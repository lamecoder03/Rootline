# Generates the product dimension and the daily inventory snapshot behind it.
# The product master is deliberately dirty — two source systems, duplicate SKUs, clashing
# category names, missing values — because cleaning it is Day 3's dbt staging exercise.
# The snapshot simulates real stock depletion and carries the ANOM-02 stockout as evidence.

import numpy as np
import pandas as pd
from faker import Faker

from config import (
    N_PRODUCTS,
    RAW_DIR,
    REGIONS,
    SEED,
    STOCKOUT_EVENT,
)
from curves import date_range

RNG = np.random.default_rng(SEED + 2)
FAKE = Faker()
Faker.seed(SEED + 2)

# SKU counts per category, weighted toward the categories that carry the most revenue.
CATEGORY_PRODUCT_COUNT = {
    "Electronics": 32,
    "Apparel": 28,
    "Home & Garden": 22,
    "Sports": 20,
    "Beauty": 18,
}
CATEGORY_SKU_PREFIX = {
    "Electronics": "ELEC",
    "Apparel": "APRL",
    "Home & Garden": "HOME",
    "Sports": "SPRT",
    "Beauty": "BEAU",
}
CATEGORY_NOUNS = {
    "Electronics": ["Wireless Earbuds", "4K Monitor", "Bluetooth Speaker", "Laptop Stand",
                    "Smart Watch", "Power Bank", "Mechanical Keyboard", "HD Webcam"],
    "Apparel": ["Denim Jacket", "Running Tee", "Wool Sweater", "Chino Trousers",
                "Rain Shell", "Knit Scarf", "Oxford Shirt"],
    "Home & Garden": ["Ceramic Planter", "Cast Iron Pan", "Throw Blanket",
                      "Garden Shears", "Table Lamp", "Storage Basket"],
    "Sports": ["Yoga Mat", "Resistance Bands", "Trail Backpack", "Insulated Bottle",
               "Cycling Gloves", "Foam Roller"],
    "Beauty": ["Vitamin C Serum", "Matte Lipstick", "Clay Mask", "Beard Oil",
               "Hand Cream", "Setting Powder"],
}
CATEGORY_UNIT_COST = {
    "Electronics": (35.0, 210.0),
    "Apparel": (9.0, 62.0),
    "Home & Garden": (12.0, 88.0),
    "Sports": (8.0, 70.0),
    "Beauty": (4.0, 34.0),
}

# The same category under two source systems. Reconciling these to one canonical label is
# the single most important thing Day 3's staging layer has to get right.
CATEGORY_ALIASES = {
    "Electronics": {
        "erp_prod": ["Electronics", "ELECTRONICS", "electronics"],
        "shopify_export": ["Consumer Electronics", "consumer electronics", "Electronics "],
    },
    "Apparel": {
        "erp_prod": ["Apparel", "APPAREL"],
        "shopify_export": ["Clothing & Apparel", "apparel", " Apparel"],
    },
    "Home & Garden": {
        "erp_prod": ["Home & Garden", "HOME & GARDEN"],
        "shopify_export": ["Home and Garden", "home_garden", "Home&Garden"],
    },
    "Sports": {
        "erp_prod": ["Sports", "SPORTS"],
        "shopify_export": ["Sports & Outdoors", "sports/outdoors"],
    },
    "Beauty": {
        "erp_prod": ["Beauty", "BEAUTY"],
        "shopify_export": ["Beauty & Personal Care", "beauty"],
    },
}

CROSS_SYSTEM_DUPLICATE_COUNT = 30
INTRA_SYSTEM_DUPLICATE_COUNT = 8
NULL_CATEGORY_RATE = 0.07
NULL_UNIT_COST_RATE = 0.10
BLANK_NAME_RATE = 0.05
NULL_SUPPLIER_RATE = 0.12
SKU_FORMATTING_NOISE_RATE = 0.15

LEAD_TIME_RANGE = (3, 8)
LATE_SHIPMENT_RATE = 0.02


def build_canonical_products():
    """The clean product truth the messy master is derived from. Region is assigned
    cyclically within each category so every category has a predictable spread of
    warehouses, which is what makes the West-region stockout reproducible."""
    records = []
    for category, count in CATEGORY_PRODUCT_COUNT.items():
        low, high = CATEGORY_UNIT_COST[category]
        for index in range(count):
            noun = CATEGORY_NOUNS[category][index % len(CATEGORY_NOUNS[category])]
            brand = FAKE.company().split()[0].replace(",", "")
            records.append(
                {
                    "sku_id": f"{CATEGORY_SKU_PREFIX[category]}-{index + 1:04d}",
                    "product_name": f"{brand} {noun}",
                    "category": category,
                    "subcategory": noun,
                    "unit_cost": round(float(RNG.uniform(low, high)), 2),
                    "supplier": FAKE.company(),
                    "primary_region": REGIONS[index % len(REGIONS)],
                    "launch_date": FAKE.date_between_dates(
                        date_start=pd.Timestamp("2022-01-01").date(),
                        date_end=pd.Timestamp("2024-06-30").date(),
                    ),
                }
            )
    return pd.DataFrame(records)


def _scramble_sku(sku_id):
    """Whitespace padding and case drift on the join key itself. Forces staging to
    normalise before matching rather than trusting the raw string."""
    style = RNG.integers(0, 4)
    if style == 0:
        return f" {sku_id} "
    if style == 1:
        return sku_id.lower()
    if style == 2:
        return f"{sku_id}  "
    return sku_id.replace("-", "_")


def _emit_row(product, source_system):
    """One product as one source system sees it: aliased category, system-specific date
    format, and independently applied missing values."""
    category = product["category"]
    alias = CATEGORY_ALIASES[category][source_system]
    launch = product["launch_date"]

    row = {
        "sku_id": product["sku_id"],
        "product_name": product["product_name"],
        "category_raw": alias[int(RNG.integers(0, len(alias)))],
        "subcategory": product["subcategory"],
        "unit_cost": product["unit_cost"],
        "supplier": product["supplier"],
        "primary_region": product["primary_region"],
        "launch_date_raw": (
            launch.strftime("%Y-%m-%d")
            if source_system == "erp_prod"
            else launch.strftime("%m/%d/%Y")
        ),
        "source_system": source_system,
        "extracted_at": "2026-01-05" if source_system == "erp_prod" else "2026-01-06",
    }

    if RNG.random() < SKU_FORMATTING_NOISE_RATE:
        row["sku_id"] = _scramble_sku(row["sku_id"])
    if RNG.random() < NULL_CATEGORY_RATE:
        row["category_raw"] = None
    if RNG.random() < NULL_UNIT_COST_RATE:
        row["unit_cost"] = None
    if RNG.random() < BLANK_NAME_RATE:
        row["product_name"] = ""
    if RNG.random() < NULL_SUPPLIER_RATE:
        row["supplier"] = None
    return row


def build_product_master(canonical):
    """Unions the two source systems into the raw master. Roughly a quarter of SKUs are
    present in both with conflicting attributes, and a handful are double-entered inside
    the ERP, so dedupe is a real decision rather than a DISTINCT."""
    shuffled = canonical.sample(frac=1.0, random_state=SEED + 2).reset_index(drop=True)
    both = set(shuffled["sku_id"].iloc[:CROSS_SYSTEM_DUPLICATE_COUNT])

    rows = []
    for _, product in canonical.iterrows():
        if product["sku_id"] in both:
            rows.append(_emit_row(product, "erp_prod"))
            rows.append(_emit_row(product, "shopify_export"))
        else:
            system = "erp_prod" if RNG.random() < 0.55 else "shopify_export"
            rows.append(_emit_row(product, system))

    erp_rows = [row for row in rows if row["source_system"] == "erp_prod"]
    for position in RNG.choice(
        len(erp_rows), size=INTRA_SYSTEM_DUPLICATE_COUNT, replace=False
    ):
        rows.append(dict(erp_rows[int(position)]))

    master = pd.DataFrame(rows).sample(frac=1.0, random_state=SEED + 3)
    return master.reset_index(drop=True)


def _stockout_skus(canonical):
    """The six highest-cost Electronics SKUs warehoused in the West — the ones a delayed
    supplier shipment would strand, and the ones ANOM-02 empties."""
    candidates = canonical[
        (canonical["category"] == STOCKOUT_EVENT["category"])
        & (canonical["primary_region"] == STOCKOUT_EVENT["region"])
    ]
    return list(
        candidates.nlargest(STOCKOUT_EVENT["n_skus_affected"], "unit_cost")["sku_id"]
    )


def build_inventory_snapshot(canonical):
    """Simulates daily stock per SKU as demand draw-down plus lead-time replenishment.
    Late shipments create incidental stockouts elsewhere in the series, so ANOM-02 cannot
    be found by simply grepping for any zero-stock day."""
    days = date_range()
    affected = set(_stockout_skus(canonical))
    frames = []

    for _, product in canonical.iterrows():
        sku_id = product["sku_id"]
        mean_demand = float(RNG.uniform(6, 45))
        reorder_point = int(mean_demand * RNG.uniform(4, 7))
        order_quantity = int(mean_demand * RNG.uniform(18, 30))
        on_hand = float(order_quantity + reorder_point)
        pending = None

        levels = np.empty(len(days))
        for position, day in enumerate(days):
            if pending is not None and pending[0] == position:
                on_hand += pending[1]
                pending = None

            on_hand = max(0.0, on_hand - RNG.poisson(mean_demand))

            if on_hand <= reorder_point and pending is None:
                lead_time = int(RNG.integers(*LEAD_TIME_RANGE))
                if RNG.random() < LATE_SHIPMENT_RATE:
                    lead_time += int(RNG.integers(4, 9))
                pending = (position + lead_time, order_quantity)

            levels[position] = on_hand

        if sku_id in affected:
            levels = _apply_stockout(days, levels, order_quantity)

        frames.append(
            pd.DataFrame(
                {
                    "snapshot_date": days,
                    "sku_id": sku_id,
                    "region": product["primary_region"],
                    "units_on_hand": levels.astype(int),
                    "reorder_point": reorder_point,
                }
            )
        )

    return pd.concat(frames, ignore_index=True)


def _apply_stockout(days, levels, order_quantity):
    """Overwrites the simulated curve with the ANOM-02 event: a three-day slide into zero,
    a week fully out of stock, then a ramp back as the delayed shipment lands."""
    event = STOCKOUT_EVENT
    for position, day in enumerate(days):
        if event["depletion_start"] <= day < event["stockout_start"]:
            remaining = (event["stockout_start"] - day).days
            span = (event["stockout_start"] - event["depletion_start"]).days
            levels[position] = levels[position] * (remaining / (span + 1))
        elif event["stockout_start"] <= day <= event["stockout_end"]:
            levels[position] = 0.0
        elif event["stockout_end"] < day <= event["recovery_end"]:
            elapsed = (day - event["stockout_end"]).days
            span = (event["recovery_end"] - event["stockout_end"]).days
            levels[position] = order_quantity * (elapsed / span) * 0.9
    return levels


def main():
    canonical = build_canonical_products()
    master = build_product_master(canonical)
    snapshot = build_inventory_snapshot(canonical)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    master.to_csv(RAW_DIR / "product_master.csv", index=False)
    snapshot.to_csv(RAW_DIR / "inventory_snapshot.csv", index=False)

    stockout_skus = _stockout_skus(canonical)
    zero_days = snapshot[snapshot["units_on_hand"] == 0]
    event_zero_days = zero_days[
        (zero_days["sku_id"].isin(stockout_skus))
        & (zero_days["snapshot_date"] >= STOCKOUT_EVENT["stockout_start"])
        & (zero_days["snapshot_date"] <= STOCKOUT_EVENT["stockout_end"])
    ]

    print(f"product_master.csv     {len(master):>7,} rows for {len(canonical)} real SKUs")
    print(f"  duplicate sku_id values (post-normalisation): "
          f"{len(master) - master['sku_id'].str.strip().str.upper().str.replace('_', '-').nunique()}")
    print(f"  null category_raw: {master['category_raw'].isna().sum()}  "
          f"null unit_cost: {master['unit_cost'].isna().sum()}  "
          f"blank product_name: {(master['product_name'] == '').sum()}  "
          f"null supplier: {master['supplier'].isna().sum()}")
    print(f"  distinct category_raw values: {master['category_raw'].nunique()}")
    print(f"inventory_snapshot.csv {len(snapshot):>7,} rows")
    print(f"  ANOM-02 SKUs: {', '.join(stockout_skus)}")
    print(f"  zero-stock days in event window: {len(event_zero_days)} "
          f"(expected {len(stockout_skus) * 7})")
    print(f"  incidental zero-stock days elsewhere: {len(zero_days) - len(event_zero_days)}")


if __name__ == "__main__":
    main()
