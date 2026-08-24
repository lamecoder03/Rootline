# Appends a newly launched category with only ~3 weeks of history to raw.daily_revenue.
# Exists to prove the detector reports "not enough history to judge" instead of either
# fabricating a baseline from too little data or silently dropping the cell from its output.
# Additive by design: the 731-day SEED=42 series and its 44 episodes are left untouched.

import argparse
import os
import sys

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Date, create_engine, text
from sqlalchemy.engine import URL

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A separate seed from the main series: this data is appended, never regenerated with it, so
# sharing SEED=42 would imply a reproducibility relationship that does not exist.
SPARSE_SEED = 4242

# The launch. 21 days ending on the series' last day, so the category is still new on the day
# the detector runs - which is the whole point. 8 weeks of same-weekday history is what the
# baseline needs; three weeks cannot supply it for any weekday.
CATEGORY = "Wearables"
HISTORY_DAYS = 21
SERIES_END = pd.Timestamp("2025-12-31")

# One channel, one region: a soft launch, not a full rollout. Keeps the scenario about history
# length rather than about slice width.
CHANNEL = "Web"
REGION = "North"

BASE_DAILY_REVENUE = 4200.0
AOV = 85.0
NOISE_SIGMA = 0.18
WEEKDAY_FACTOR = {0: 0.94, 1: 0.97, 2: 1.00, 3: 1.03, 4: 1.10, 5: 1.15, 6: 0.98}


def build_engine():
    """Same credential contract as the main loader: from .env, never from source."""
    load_dotenv(os.path.join(REPO_ROOT, ".env"))
    url = URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB"),
    )
    return create_engine(url, future=True)


def build_frame():
    """Twenty-one days of an ordinary, unremarkable new category. Deliberately contains NO
    anomaly: the question being tested is whether the detector admits it cannot judge, and an
    injected event would confuse 'abstained correctly' with 'missed something'."""
    rng = np.random.default_rng(SPARSE_SEED)
    dates = pd.date_range(end=SERIES_END, periods=HISTORY_DAYS, freq="D")

    revenue = np.array([
        BASE_DAILY_REVENUE
        * WEEKDAY_FACTOR[date.weekday()]
        * float(rng.lognormal(mean=0.0, sigma=NOISE_SIGMA))
        for date in dates
    ])

    units = np.maximum(1, np.round(revenue / AOV)).astype(int)
    return pd.DataFrame({
        "order_date": [d.date() for d in dates],
        "category": CATEGORY,
        "channel": CHANNEL,
        "region": REGION,
        "orders": np.maximum(1, np.round(units * 0.72)).astype(int),
        "units": units,
        "gross_revenue": np.round(revenue, 2),
    })


def existing_columns(engine):
    """raw.daily_revenue is written by the main loader, so its column set is authoritative here
    rather than assumed - a mismatch must fail loudly instead of inserting NULLs."""
    with engine.connect() as connection:
        rows = connection.execute(text(
            "select column_name from information_schema.columns "
            "where table_schema = 'raw' and table_name = 'daily_revenue' "
            "order by ordinal_position"
        )).fetchall()
    return [row[0] for row in rows]


def main():
    parser = argparse.ArgumentParser(description="Append the sparse-history category.")
    parser.add_argument("--remove", action="store_true",
                        help="Delete the sparse category instead of adding it.")
    args = parser.parse_args()

    engine = build_engine()
    columns = existing_columns(engine)
    if not columns:
        raise SystemExit("raw.daily_revenue not found. Run the main loader first.")

    if args.remove:
        with engine.begin() as connection:
            deleted = connection.execute(
                text("delete from raw.daily_revenue where category = :c"), {"c": CATEGORY}
            ).rowcount
        print(f"Removed {deleted} {CATEGORY} rows from raw.daily_revenue.")
        return 0

    frame = build_frame()
    missing = [c for c in frame.columns if c not in columns]
    if missing:
        raise SystemExit(f"raw.daily_revenue has no column(s): {', '.join(missing)}")

    with engine.begin() as connection:
        already = connection.execute(
            text("select count(*) from raw.daily_revenue where category = :c"), {"c": CATEGORY}
        ).scalar()
        if already:
            connection.execute(
                text("delete from raw.daily_revenue where category = :c"), {"c": CATEGORY}
            )

    frame.to_sql("daily_revenue", engine, schema="raw", if_exists="append", index=False,
                 dtype={"order_date": Date()})

    print(f"Appended {len(frame)} rows: {CATEGORY} | {CHANNEL} | {REGION}")
    print(f"  window   : {frame.order_date.min()} -> {frame.order_date.max()}")
    print(f"  revenue  : ${frame.gross_revenue.sum():,.2f}")
    print(f"\n{HISTORY_DAYS} days is below every history gate the detector requires.")
    print("Rebuild and re-detect, then read analytics.detection_coverage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
