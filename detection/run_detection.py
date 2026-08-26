# Entry point for the detector - the `detect` step the Airflow DAG will call.
# Exists so the whole method runs from one command, reproducibly, and reports what it wrote.
# Scores every cell-day, groups flagged days into incidents, and replaces the two output tables.

import sys
import uuid
from datetime import datetime, timezone

from detection import config as cfg
from detection.detector import detect, to_episodes
from detection.persist import build_engine, persist, persist_coverage


def main():
    engine = build_engine()
    run_id = str(uuid.uuid4())
    detected_at = datetime.now(timezone.utc)

    print(f"Reading {cfg.SOURCE_TABLE}")
    points = detect(engine)
    episodes = to_episodes(points)

    flagged = int(points.is_anomaly.sum())
    print(f"  scored {len(points):,} cell-days across {points.cell_key.nunique()} cells")
    print(f"  empirical null: centre {points.attrs['null_centre']:+.3f}, "
          f"spread {points.attrs['null_spread']:.3f}")
    print(f"  flagged {flagged:,} cell-days "
          f"({100.0 * flagged / len(points):.2f}%) in {len(episodes)} episodes")

    coverage = points.attrs.get("cell_coverage")
    if coverage is not None and not coverage.empty:
        unjudgeable = coverage[coverage.confidence != "normal"]
        if len(unjudgeable):
            print(f"  {len(unjudgeable)} cell(s) below the history gates and NOT judgeable:")
            for row in unjudgeable.itertuples():
                print(f"    {row.cell_key:<38} {row.days_observed:>4} days observed, "
                      f"{row.pct_scored:>5.1f}% scored, confidence={row.confidence}")

    episode_count, point_count = persist(engine, episodes, points, run_id, detected_at)
    coverage_count = persist_coverage(engine, coverage, run_id, detected_at)
    print(f"Wrote {cfg.OUTPUT_SCHEMA}.{cfg.EPISODES_TABLE:<24} {episode_count:>6,} rows")
    print(f"Wrote {cfg.OUTPUT_SCHEMA}.{cfg.POINTS_TABLE:<24} {point_count:>6,} rows")
    print(f"Wrote {cfg.OUTPUT_SCHEMA}.{cfg.COVERAGE_TABLE:<24} {coverage_count:>6,} rows")
    print(f"run_id {run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
