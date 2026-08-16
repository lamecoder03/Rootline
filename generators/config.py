# Single source of truth for the synthetic world: date range, dimensions, and the three
# injected anomalies. Every generator and the ground-truth doc read from here, so the
# data and the answer key can never drift apart.
# Changing SEED or any constant here changes the data; regenerate and re-derive ground truth.

from dataclasses import dataclass
from datetime import date
from pathlib import Path

SEED = 42

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
DOCS_DIR = REPO_ROOT / "docs"

START_DATE = date(2024, 1, 1)
END_DATE = date(2025, 12, 31)

# Daily revenue in USD each category produces at the start of the series, before the
# channel/region split, trend, and seasonality are applied.
CATEGORY_BASE_REVENUE = {
    "Electronics": 42_000.0,
    "Apparel": 28_000.0,
    "Home & Garden": 19_000.0,
    "Beauty": 12_000.0,
    "Sports": 15_000.0,
}

CHANNEL_SHARE = {"Web": 0.45, "Mobile App": 0.35, "Marketplace": 0.20}
REGION_SHARE = {"North": 0.30, "East": 0.28, "South": 0.22, "West": 0.20}

CATEGORIES = tuple(CATEGORY_BASE_REVENUE)
CHANNELS = tuple(CHANNEL_SHARE)
REGIONS = tuple(REGION_SHARE)

# Compound annual growth per category and a small per-region adjustment, so different
# slices drift apart over two years rather than moving as one block.
CATEGORY_ANNUAL_GROWTH = {
    "Electronics": 1.16,
    "Apparel": 1.05,
    "Home & Garden": 1.09,
    "Beauty": 1.22,
    "Sports": 1.11,
}
REGION_GROWTH_ADJUSTMENT = {"North": 1.00, "East": 0.99, "South": 1.03, "West": 1.02}

# Day-of-week shape (Monday=0) and how strongly each channel amplifies it. Mobile App
# swings harder into the weekend; Marketplace is flatter because it is browse-driven.
WEEKDAY_FACTOR = {0: 0.95, 1: 0.92, 2: 0.94, 3: 1.00, 4: 1.08, 5: 1.15, 6: 1.10}
CHANNEL_WEEKLY_AMPLITUDE = {"Web": 1.00, "Mobile App": 1.15, "Marketplace": 0.60}

# How hard each category reacts to a holiday, applied to the calendar effect in curves.py.
CATEGORY_HOLIDAY_SENSITIVITY = {
    "Electronics": 1.35,
    "Apparel": 1.10,
    "Home & Garden": 0.75,
    "Beauty": 1.00,
    "Sports": 0.85,
}

# Average order value per category, used to derive orders and units from revenue.
CATEGORY_AOV = {
    "Electronics": 245.0,
    "Apparel": 88.0,
    "Home & Garden": 132.0,
    "Beauty": 54.0,
    "Sports": 97.0,
}

REVENUE_NOISE_SIGMA = 0.09
CELL_LEVEL_SIGMA = 0.12

# Marketing spend as a share of the revenue it supports. Mobile App is acquisition-heavy;
# Marketplace is low because its fees are not booked as marketing spend.
CHANNEL_SPEND_RATIO = {"Web": 0.080, "Mobile App": 0.140, "Marketplace": 0.045}
SPEND_NOISE_SIGMA = 0.16

N_PRODUCTS = 120
SOURCE_SYSTEMS = ("erp_prod", "shopify_export")


@dataclass(frozen=True)
class Anomaly:
    """One injected event: which slice it hits, which days, and the per-day revenue
    multiplier applied to the counterfactual baseline. `filters` is matched against the
    revenue grain columns; an absent key means the event hits every value of that column."""

    anomaly_id: str
    label: str
    cause_family: str
    start: date
    end: date
    filters: dict
    daily_multipliers: tuple
    narrative: str


# The three injected anomalies. Deliberately different in shape and scope: a short sharp
# spike, a narrow deep dip, and a slow two-week decay — so Day 5 detection is tested
# against easy, medium, and hard cases rather than three of the same thing.
ANOMALIES = (
    Anomaly(
        anomaly_id="ANOM-01",
        label="Spring Style Event promotion",
        cause_family="promotion",
        start=date(2025, 3, 14),
        end=date(2025, 3, 17),
        filters={"category": "Apparel"},
        daily_multipliers=(1.95, 2.15, 2.05, 1.70),
        narrative=(
            "A four-day sitewide Apparel promotion over a Friday-to-Monday weekend, "
            "supported by a paid-media surge that starts two days before the event."
        ),
    ),
    Anomaly(
        anomaly_id="ANOM-02",
        label="West-region Electronics stockout",
        cause_family="inventory",
        start=date(2025, 6, 9),
        end=date(2025, 6, 15),
        filters={"category": "Electronics", "region": "West"},
        daily_multipliers=(0.72, 0.48, 0.38, 0.35, 0.35, 0.41, 0.66),
        narrative=(
            "A delayed supplier shipment empties the West distribution centre of its "
            "top Electronics SKUs. Marketing spend is deliberately left untouched, so "
            "the spend table cannot explain this dip and inventory must be consulted."
        ),
    ),
    Anomaly(
        anomaly_id="ANOM-03",
        label="Mobile App acquisition budget cut",
        cause_family="marketing_spend",
        start=date(2025, 9, 22),
        end=date(2025, 10, 5),
        filters={"channel": "Mobile App"},
        daily_multipliers=(
            0.94, 0.89, 0.84, 0.80, 0.77, 0.74, 0.72,
            0.70, 0.70, 0.71, 0.72, 0.74, 0.76, 0.79,
        ),
        narrative=(
            "Paid user-acquisition spend on the Mobile App channel is cut to 30% of "
            "plan five days before revenue reacts. Revenue decays gradually rather "
            "than stepping down, which is the hardest of the three to detect."
        ),
    ),
)

# Spend-side overrides. These are the marketing footprint of the events above: the promo
# is preceded by a media surge, the budget cut leads its revenue impact by five days.
# ANOM-02 has no entry on purpose — it is the negative control for spend-based reasoning.
SPEND_OVERRIDES = (
    {
        "anomaly_id": "ANOM-01",
        "start": date(2025, 3, 12),
        "end": date(2025, 3, 17),
        "filters": {"category": "Apparel"},
        "multiplier": 2.60,
    },
    {
        "anomaly_id": "ANOM-03",
        "start": date(2025, 9, 17),
        "end": date(2025, 10, 5),
        "filters": {"channel": "Mobile App"},
        "multiplier": 0.30,
    },
)

# The inventory footprint of ANOM-02: stock in the West DC depletes from 6 June and is
# fully out from 9-15 June, three days before revenue visibly bottoms out.
STOCKOUT_EVENT = {
    "anomaly_id": "ANOM-02",
    "category": "Electronics",
    "region": "West",
    "depletion_start": date(2025, 6, 6),
    "stockout_start": date(2025, 6, 9),
    "stockout_end": date(2025, 6, 15),
    "recovery_end": date(2025, 6, 19),
    "n_skus_affected": 6,
}
