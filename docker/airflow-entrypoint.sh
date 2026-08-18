#!/usr/bin/env bash
# Starts Airflow after making sure its metadata database exists.
# Exists because the Postgres volume predates Airflow, so docker-entrypoint-initdb.d never
# fires for it and `airflow standalone` would abort on a missing database.
# Creates the database if absent (idempotent), then hands off to standalone via exec so
# signals and the exit code reach Airflow rather than this script.

set -euo pipefail

echo "[entrypoint] ensuring Airflow metadata database '${AIRFLOW_METADATA_DB}' exists"
/opt/venvs/project/bin/python - <<'PY'
import os
import psycopg2

target = os.environ["AIRFLOW_METADATA_DB"]
connection = psycopg2.connect(
    host=os.environ["POSTGRES_HOST"],
    port=int(os.environ["POSTGRES_PORT"]),
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    dbname=os.environ["POSTGRES_DB"],
)
connection.autocommit = True
with connection.cursor() as cursor:
    cursor.execute("select 1 from pg_database where datname = %s", (target,))
    if cursor.fetchone():
        print(f"[entrypoint] database '{target}' already present")
    else:
        cursor.execute(f'create database "{target}"')
        print(f"[entrypoint] created database '{target}'")
connection.close()
PY

echo "[entrypoint] starting airflow standalone"
exec airflow standalone
