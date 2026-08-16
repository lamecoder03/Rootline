# Loads every generator CSV into the Postgres `raw` schema — the ingest step the Day 6
# Airflow DAG will call. Credentials come from .env, never from source.
# Each file lands in its own table, replaced in full on every run, so the load is idempotent.
# Date columns are typed as DATE; deliberately messy columns stay TEXT for dbt to clean.

import os
import sys

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Date, create_engine, text
from sqlalchemy.engine import URL

from config import RAW_DIR, REPO_ROOT

RAW_SCHEMA = "raw"
CHUNK_SIZE = 10_000

# Source file -> target table, with the columns that should land as real DATEs.
# product_master's launch_date_raw is missing on purpose: it arrives in two different
# string formats and reconciling it is Day 3's staging work, not the loader's.
LOAD_PLAN = (
    ("daily_revenue.csv", "daily_revenue", ("order_date",)),
    ("marketing_spend.csv", "marketing_spend", ("spend_date",)),
    ("product_master.csv", "product_master", ()),
    ("inventory_snapshot.csv", "inventory_snapshot", ("snapshot_date",)),
    ("holiday_calendar.csv", "holiday_calendar", ("holiday_date",)),
)


def build_engine():
    """Reads .env and builds the connection. URL.create escapes the password, so
    credentials containing @ or : do not corrupt the DSN."""
    load_dotenv(REPO_ROOT / ".env")
    missing = [
        key
        for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
        if not os.getenv(key)
    ]
    if missing:
        raise SystemExit(
            f"Missing in .env: {', '.join(missing)}. Copy .env.example to .env first."
        )

    url = URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB"),
    )
    return create_engine(url, future=True)


def load_table(engine, filename, table_name, date_columns):
    """Reads one generator CSV and replaces its target table wholesale.
    Returns the row count so the caller can report what actually landed."""
    path = RAW_DIR / filename
    if not path.exists():
        raise SystemExit(f"{path} not found. Run the generators before loading.")

    frame = pd.read_csv(path, parse_dates=list(date_columns))
    for column in date_columns:
        frame[column] = frame[column].dt.date

    frame.to_sql(
        table_name,
        engine,
        schema=RAW_SCHEMA,
        if_exists="replace",
        index=False,
        chunksize=CHUNK_SIZE,
        method="multi",
        dtype={column: Date() for column in date_columns},
    )
    return len(frame)


def main():
    engine = build_engine()

    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {RAW_SCHEMA}"))

    print(f"Loading into schema '{RAW_SCHEMA}'")
    for filename, table_name, date_columns in LOAD_PLAN:
        rows = load_table(engine, filename, table_name, date_columns)
        print(f"  {RAW_SCHEMA}.{table_name:<20} {rows:>7,} rows")

    with engine.connect() as connection:
        total = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"
            ),
            {"s": RAW_SCHEMA},
        ).scalar_one()
    print(f"Done. {total} tables in {RAW_SCHEMA}.")


if __name__ == "__main__":
    sys.exit(main())
