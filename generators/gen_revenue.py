# Generates two years of daily revenue at the category x channel x region grain and
# injects the three anomalies defined in config.py. This is the fact table the whole
# project detects on, so it carries seasonality and noise but no hint of the answer.
# Writes the blind series to data/raw/ and the counterfactual answer key to docs/.

import numpy as np
import pandas as pd

from config import (
    ANOMALIES,
    CATEGORY_AOV,
    CATEGORY_BASE_REVENUE,
    CELL_LEVEL_SIGMA,
    CHANNEL_SHARE,
    CHANNELS,
    CATEGORIES,
    DOCS_DIR,
    RAW_DIR,
    REGION_SHARE,
    REGIONS,
    REVENUE_NOISE_SIGMA,
    SEED,
)
from curves import baseline_signal, date_range

RNG = np.random.default_rng(SEED)
PROMO_AOV_DISCOUNT = 0.80


def anomaly_multipliers(days, category, channel, region):
    """Per-day revenue multiplier for one cell, from any anomaly whose filters match it.
    Returns the multiplier array plus the id of the anomaly responsible for each day, so
    the answer key can be assembled without re-deriving the matching logic later."""
    dimensions = {"category": category, "channel": channel, "region": region}
    multipliers = np.ones(len(days))
    owners = [None] * len(days)
    index = {day: position for position, day in enumerate(days)}

    for anomaly in ANOMALIES:
        if any(dimensions[key] != value for key, value in anomaly.filters.items()):
            continue
        window = [d for d in days if anomaly.start <= d <= anomaly.end]
        for offset, day in enumerate(window):
            position = index[day]
            multipliers[position] = anomaly.daily_multipliers[offset]
            owners[position] = anomaly.anomaly_id

    return multipliers, owners


def build_revenue():
    """Builds every cell as counterfactual baseline x injected multiplier, keeping both.
    Baseline and actual share the same noise draw, so the difference between them is
    purely the injected event and the answer key is exact rather than approximate."""
    days = date_range()
    day_count = len(days)
    frames = []

    for category in CATEGORIES:
        for channel in CHANNELS:
            for region in REGIONS:
                cell_level = RNG.lognormal(0.0, CELL_LEVEL_SIGMA)
                scale = (
                    CATEGORY_BASE_REVENUE[category]
                    * CHANNEL_SHARE[channel]
                    * REGION_SHARE[region]
                    * cell_level
                )
                signal = np.array(
                    [baseline_signal(day, category, channel, region) for day in days]
                )
                noise = RNG.lognormal(
                    -0.5 * REVENUE_NOISE_SIGMA**2, REVENUE_NOISE_SIGMA, day_count
                )
                baseline = scale * signal * noise
                multipliers, owners = anomaly_multipliers(days, category, channel, region)

                aov = CATEGORY_AOV[category] * RNG.lognormal(-0.00125, 0.05, day_count)
                promo_days = np.array(
                    [owner == "ANOM-01" for owner in owners], dtype=bool
                )
                aov = np.where(promo_days, aov * PROMO_AOV_DISCOUNT, aov)

                actual = baseline * multipliers
                orders = np.maximum(np.round(actual / aov), 1).astype(int)
                units = np.round(orders * RNG.uniform(1.05, 1.90, day_count)).astype(int)

                frames.append(
                    pd.DataFrame(
                        {
                            "order_date": days,
                            "category": category,
                            "channel": channel,
                            "region": region,
                            "orders": orders,
                            "units": units,
                            "gross_revenue": np.round(actual, 2),
                            "_baseline_revenue": baseline,
                            "_anomaly_id": owners,
                            "_multiplier": multipliers,
                        }
                    )
                )

    return pd.concat(frames, ignore_index=True)


def build_ground_truth(revenue):
    """Collapses the injected events into a per-day answer key: counterfactual revenue,
    realised revenue, and the exact dollar and percentage gap between them.
    Day 5's detector is scored against this, so it is written to docs/, never to Postgres."""
    injected = revenue[revenue["_anomaly_id"].notna()]
    daily = (
        injected.groupby(["_anomaly_id", "order_date"], as_index=False)
        .agg(
            injected_multiplier=("_multiplier", "first"),
            affected_cells=("gross_revenue", "size"),
            baseline_revenue_usd=("_baseline_revenue", "sum"),
            actual_revenue_usd=("gross_revenue", "sum"),
        )
        .rename(columns={"_anomaly_id": "anomaly_id"})
    )
    daily["delta_usd"] = daily["actual_revenue_usd"] - daily["baseline_revenue_usd"]
    daily["delta_pct"] = 100 * daily["delta_usd"] / daily["baseline_revenue_usd"]
    daily["baseline_revenue_usd"] = daily["baseline_revenue_usd"].round(2)
    daily["delta_usd"] = daily["delta_usd"].round(2)
    daily["delta_pct"] = daily["delta_pct"].round(2)
    return daily.sort_values(["anomaly_id", "order_date"]).reset_index(drop=True)


def main():
    revenue = build_revenue()
    ground_truth = build_ground_truth(revenue)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    public_columns = [
        "order_date",
        "category",
        "channel",
        "region",
        "orders",
        "units",
        "gross_revenue",
    ]
    revenue[public_columns].to_csv(RAW_DIR / "daily_revenue.csv", index=False)
    ground_truth.to_csv(DOCS_DIR / "ground_truth_anomalies.csv", index=False)

    print(f"daily_revenue.csv          {len(revenue):>7,} rows")
    print(f"ground_truth_anomalies.csv {len(ground_truth):>7,} rows")
    for anomaly in ANOMALIES:
        rows = ground_truth[ground_truth["anomaly_id"] == anomaly.anomaly_id]
        baseline = rows["baseline_revenue_usd"].sum()
        actual = rows["actual_revenue_usd"].sum()
        delta = actual - baseline
        worst = rows.loc[rows["delta_pct"].abs().idxmax()]
        print(
            f"  {anomaly.anomaly_id} {anomaly.start}..{anomaly.end} "
            f"cells={int(rows['affected_cells'].iloc[0])} "
            f"baseline={baseline:,.2f} actual={actual:,.2f} "
            f"delta={delta:,.2f} ({100 * delta / baseline:+.2f}%) "
            f"peak={worst['order_date']} ({worst['delta_pct']:+.2f}%)"
        )


if __name__ == "__main__":
    main()
