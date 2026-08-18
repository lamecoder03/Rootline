# The one DAG: ingest -> transform -> detect, the whole pipeline in dependency order.
# Exists so the warehouse rebuilds and re-scores itself unattended, instead of three commands
# being run by hand in the right order and the wrong one being forgotten.
# Every task is a BashOperator invoking a Linux venv built into the image: the project code
# never enters Airflow's own interpreter, so dbt's pins and Airflow's pins cannot collide.

from __future__ import annotations

from datetime import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

# Paths and the warehouse address arrive as container environment variables from
# docker-compose.yml, which fills them from the same .env the host tooling reads. Referencing
# them here rather than hardcoding is what keeps the containerised and local paths in step.
PROJECT_ROOT = "/opt/project"
PROJECT_PYTHON = "/opt/venvs/project/bin/python"
DBT = "/opt/venvs/dbt/bin/dbt"

# set -euo pipefail is the whole failure-propagation story. Without it a failing command in the
# middle of a chain is swallowed and the task reports success - which is worse than having no
# orchestration at all, because it looks like the pipeline ran. BashOperator then turns any
# non-zero exit into a failed task, exactly as run_dbt.bat propagates dbt's exit code on Windows.
STRICT = "set -euo pipefail"

with DAG(
    dag_id="revenue_anomaly_pipeline",
    description="Load raw data, rebuild the dbt warehouse, then re-score anomalies.",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["revenue-anomaly"],
    doc_md=__doc__,
) as dag:

    ingest = BashOperator(
        task_id="ingest",
        cwd=PROJECT_ROOT,
        bash_command=(
            f"{STRICT}\n"
            f"{PROJECT_PYTHON} generators/load_to_postgres.py"
        ),
        doc_md=(
            "Replaces all five `raw` tables from the generator CSVs. Idempotent: each table is "
            "dropped and rewritten, so a re-run converges rather than duplicating rows."
        ),
    )

    transform = BashOperator(
        task_id="transform",
        cwd=PROJECT_ROOT,
        bash_command=(
            f"{STRICT}\n"
            f'{DBT} build --project-dir "$DBT_PROJECT_DIR" --profiles-dir "$DBT_PROJECT_DIR"'
        ),
        doc_md=(
            "`dbt build` runs seeds, staging, intermediate and marts models and all 191 tests "
            "in dependency order. A failing test fails the task, so bad data stops the pipeline "
            "here instead of reaching the detector."
        ),
    )

    detect = BashOperator(
        task_id="detect",
        cwd=PROJECT_ROOT,
        bash_command=(
            f"{STRICT}\n"
            f"{PROJECT_PYTHON} -m detection.run_detection"
        ),
        doc_md=(
            "Scores all 60 cells and replaces `analytics.detected_anomalies` and "
            "`analytics.detected_anomaly_points`. Runs last because it reads the marts that "
            "`transform` rebuilds."
        ),
    )

    ingest >> transform >> detect
