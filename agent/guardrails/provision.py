# Creates the read-only agent role, grants it exactly SELECT on analytics plus INSERT on the
# audit table, and applies the audit DDL. Idempotent - safe to re-run after every dbt build.
# Exists because dbt drops and recreates its tables, and a GRANT dies with the object it was on;
# ALTER DEFAULT PRIVILEGES is what makes the grant survive, and that is easy to get wrong by hand.
# Run as the owner: python -m agent.guardrails.provision

from __future__ import annotations

import os

from psycopg2 import sql
from sqlalchemy import text

from . import config as cfg
from .db import REPO_ROOT, agent_credentials, build_owner_engine

AUDIT_DDL_PATH = os.path.join(REPO_ROOT, "agent", "audit", "schema.sql")


def _statements(connection):
    """psycopg2.sql composes identifiers and literals with correct quoting, so the role name and
    password reach the server as data rather than as string-formatted SQL - the same class of bug
    the validator exists to stop, applied to our own provisioning code."""
    return connection.connection.driver_connection.cursor()


def provision(verbose=True):
    role, password = agent_credentials()
    database = os.getenv("POSTGRES_DB")
    owner = os.getenv("POSTGRES_USER")
    engine = build_owner_engine(isolation_level="AUTOCOMMIT")

    role_id = sql.Identifier(role)
    db_id = sql.Identifier(database)
    owner_id = sql.Identifier(owner)
    analytics_id = sql.Identifier(cfg.ANALYTICS_SCHEMA)
    audit_id = sql.Identifier(cfg.AUDIT_SCHEMA)
    audit_table_id = sql.Identifier(cfg.AUDIT_SCHEMA, cfg.AUDIT_TABLE)

    with engine.connect() as connection:
        cursor = _statements(connection)

        with open(AUDIT_DDL_PATH, encoding="utf-8") as handle:
            cursor.execute(handle.read())

        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        ).scalar()

        # LOGIN and nothing else. No CREATEDB, no CREATEROLE, no BYPASSRLS, no inherited
        # membership - the role starts with the empty privilege set and is granted upward.
        if exists:
            cursor.execute(
                sql.SQL(
                    "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(role_id, sql.Literal(password))
            )
        else:
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(role_id, sql.Literal(password))
            )

        # PUBLIC is a role every login inherits, and it holds CREATE and TEMPORARY on the
        # database by default. Left alone, the agent could create temp tables and scratch
        # objects; revoking from PUBLIC is the only way to take them away, since they were
        # never granted to the agent role directly.
        cursor.execute(sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(db_id))
        cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
        cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db_id, role_id))

        # Explicit revokes on the three schemas the agent must never reach. These are no-ops on a
        # clean database - nothing was ever granted. They are written anyway so the intent is in
        # the code and re-running after someone grants by hand undoes it.
        for schema in cfg.FORBIDDEN_SCHEMAS:
            schema_id = sql.Identifier(schema)
            cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(schema_id, role_id))
            cursor.execute(
                sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(schema_id, role_id)
            )

        # The read grant. USAGE on the schema is what makes the objects addressable at all;
        # SELECT on the tables is what makes them readable. Both are needed, and neither implies
        # the other.
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(analytics_id, role_id))
        cursor.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(analytics_id, role_id)
        )

        # The line that keeps the grant alive. dbt materialises marts with CREATE TABLE AS, so
        # every build produces new objects that carry no grants; default privileges attach SELECT
        # to whatever the owner creates in analytics from now on. Without this the agent works
        # until the next DAG run and then silently loses access.
        cursor.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
            ).format(owner_id, analytics_id, role_id)
        )

        # Audit: USAGE on the schema and INSERT on the one table. No SELECT, so the agent cannot
        # read its own trail; no UPDATE or DELETE, so it cannot rewrite it. Append-only as a
        # privilege set, not as an application convention.
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(audit_id, role_id))
        cursor.execute(
            sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(audit_table_id, role_id)
        )
        cursor.execute(
            sql.SQL("GRANT INSERT ON TABLE {} TO {}").format(audit_table_id, role_id)
        )

        # search_path is resolved at execution time and is attacker-influenceable, so it is
        # pinned on the role. Belt and braces with the validator, which schema-qualifies every
        # table reference before the query is allowed to run.
        cursor.execute(
            sql.SQL("ALTER ROLE {} SET search_path = {}, pg_catalog").format(
                role_id, analytics_id
            )
        )
        # A resource guard, not a security boundary - the role can raise it with SET, so it is
        # written to stop a runaway scan, not a determined caller. `default_transaction_read_only`
        # was considered here and deliberately not set: it is equally overridable, and it would
        # block the audit INSERT the same connection has to make.
        cursor.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '30s'").format(role_id))

    if verbose:
        print(f"Provisioned read-only role '{role}':")
        print(f"  CONNECT   {database}")
        print(f"  USAGE     {cfg.ANALYTICS_SCHEMA}, {cfg.AUDIT_SCHEMA}")
        print(f"  SELECT    {cfg.ANALYTICS_SCHEMA}.* ({len(cfg.ALLOWED_TABLES)} objects)")
        print(f"  INSERT    {cfg.AUDIT_QUALIFIED}")
        print(f"  denied    {', '.join(cfg.FORBIDDEN_SCHEMAS)} (no USAGE, no SELECT)")
        print(f"  session   search_path=analytics, 30s statement timeout")
    return role


if __name__ == "__main__":
    provision()
