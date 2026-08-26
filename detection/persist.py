# Writes detector output to Postgres so the Airflow DAG and the agent read a table, not a
# printed report.
# Exists because detection output is an input to two later stages: the agent must be able to ask
# "what fired, where, how bad" in SQL, under the read-only grant on the analytics schema.
# Two tables: one row per incident for investigation, one row per scored day as the evidence.

import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Date, Numeric, create_engine, text
from sqlalchemy.engine import URL

from . import config as cfg

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Only the flagged points are persisted, not all 40,000 scored days. The unflagged scores are a
# diagnostic the validation harness recomputes on demand; storing them would bloat the table the
# agent queries without telling it anything it needs.
POINT_COLUMNS = [
    "order_date", "cell_key", "category", "channel", "region",
    "gross_revenue", "expected_revenue", "delta_pct",
    "z_score", "z_confirmed", "z_threshold_applied", "confirmation_window_days",
    "p_value", "q_value", "direction", "is_holiday", "retail_significance",
    "baseline_n", "scale_n",
]


def build_engine():
    """Same connection contract as the ingest loader: credentials from .env, never from source,
    and URL.create escapes them so a password containing @ or : cannot corrupt the DSN."""
    load_dotenv(os.path.join(REPO_ROOT, ".env"))
    missing = [
        key for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
        if not os.getenv(key)
    ]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")

    url = URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB"),
    )
    return create_engine(url, future=True)


def persist_coverage(engine, coverage, run_id, detected_at):
    """Writes the per-cell confidence report the detector produced alongside its findings.
    Separate from persist() because it answers a different question: not "what fired" but
    "which cells could be judged at all" - the two must not be inferred from each other."""
    if coverage is None or coverage.empty:
        return 0

    coverage = coverage.copy()
    coverage["detection_run_id"] = run_id
    coverage["detected_at"] = detected_at

    coverage.to_sql(
        cfg.COVERAGE_TABLE, engine, schema=cfg.OUTPUT_SCHEMA, if_exists="replace", index=False,
        dtype={"first_observed": Date(), "last_observed": Date(),
               "pct_scored": Numeric(5, 1)},
    )

    with engine.begin() as connection:
        connection.execute(text(
            f"COMMENT ON TABLE {cfg.OUTPUT_SCHEMA}.{cfg.COVERAGE_TABLE} IS "
            "'One row per revenue cell: how many of its days the detector could score, and the "
            "confidence that supports. confidence=none means no anomaly can be ruled in OR out "
            "for that cell - absence of a detected anomaly is not evidence of normality.'"
        ))
    return len(coverage)


def persist(engine, episodes, points, run_id, detected_at):
    """Replaces both tables wholesale on every run, exactly like the ingest loader.
    Chosen over append-with-history because the detector is deterministic over a fixed window:
    re-running must converge on one answer rather than accumulate duplicates each time the DAG
    fires. run_id and detected_at record which run produced the rows that are there."""
    flagged = points[points.is_anomaly][POINT_COLUMNS].copy()

    episodes = episodes.copy()
    episodes.insert(0, "anomaly_key", [f"DET-{i + 1:04d}" for i in range(len(episodes))])
    for frame in (episodes, flagged):
        frame["detection_run_id"] = run_id
        frame["detected_at"] = detected_at

    flagged = flagged.merge(
        episodes[["anomaly_key", "cell_key", "start_date", "end_date"]], on="cell_key", how="left"
    )
    flagged = flagged[
        (flagged.order_date >= flagged.start_date) & (flagged.order_date <= flagged.end_date)
    ].drop(columns=["start_date", "end_date"])

    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {cfg.OUTPUT_SCHEMA}"))

    episodes.to_sql(
        cfg.EPISODES_TABLE, engine, schema=cfg.OUTPUT_SCHEMA, if_exists="replace", index=False,
        dtype={"start_date": Date(), "end_date": Date(), "peak_date": Date(),
               "peak_z_score": Numeric(10, 3), "peak_delta_pct": Numeric(10, 2),
               "total_revenue_delta_usd": Numeric(14, 2)},
    )
    flagged.to_sql(
        cfg.POINTS_TABLE, engine, schema=cfg.OUTPUT_SCHEMA, if_exists="replace", index=False,
        dtype={"order_date": Date(), "gross_revenue": Numeric(14, 2),
               "expected_revenue": Numeric(14, 2), "delta_pct": Numeric(10, 2),
               "z_score": Numeric(10, 3), "z_confirmed": Numeric(10, 3)},
    )

    with engine.begin() as connection:
        connection.execute(text(
            f"COMMENT ON TABLE {cfg.OUTPUT_SCHEMA}.{cfg.EPISODES_TABLE} IS "
            "'One row per detected incident. Consecutive flagged days in one cell are one row. "
            "Replaced in full on every detector run.'"
        ))
        connection.execute(text(
            f"COMMENT ON TABLE {cfg.OUTPUT_SCHEMA}.{cfg.POINTS_TABLE} IS "
            "'One row per flagged day, the per-day evidence behind each row in "
            "detected_anomalies. Joined on anomaly_key.'"
        ))

    return len(episodes), len(flagged)
