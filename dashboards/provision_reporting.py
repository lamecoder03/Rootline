# Creates `revenue_reporting`, the read-only login Power BI Desktop connects as.
# Exists because a human's BI tool and an autonomous agent are two different consumers of the
# same marts, and giving them one shared login makes "who read this?" unanswerable in the logs.
# Same least-privilege shape as the guardrail layer's revenue_agent, deliberately NOT the same role.
# Run as the owner: python -m dashboards.provision_reporting [--verify]

from __future__ import annotations

import os
import sys

from psycopg2 import sql
from sqlalchemy import text

# Imported rather than re-implemented. The project's "one credential source" principle applies here
# exactly as it does to dbt and the loader: these helpers read POSTGRES_* from the environment and
# escape the DSN, and a second copy of that logic is a second thing to get wrong. Nothing in the
# agent package is modified by this module - it only borrows the owner connection builder.
from agent.guardrails.config import ANALYTICS_SCHEMA, ALLOWED_TABLES, AUDIT_QUALIFIED
from agent.guardrails.config import AUDIT_SCHEMA, FORBIDDEN_SCHEMAS
from agent.guardrails.db import _require, _url, build_owner_engine

# The reporting role's credentials live under their own env keys, so a leaked BI connection
# string cannot be pasted into the agent and vice versa.
ROLE_ENV_USER = "REPORTING_DB_USER"
ROLE_ENV_PASSWORD = "REPORTING_DB_PASSWORD"

# Longer than the agent's 30s. An agent query is interactive and a slow one means a bad query;
# a Power BI import legitimately scans a whole 43,860-row fact table, and six of them in a
# refresh. Still bounded, because an unbounded statement is how a BI tool takes a warehouse down.
STATEMENT_TIMEOUT = "120s"


def reporting_credentials():
    values = _require(ROLE_ENV_USER, ROLE_ENV_PASSWORD, "POSTGRES_DB")
    return values[ROLE_ENV_USER], values[ROLE_ENV_PASSWORD]


def build_reporting_engine(**kwargs):
    """The reporting identity. Used only to prove the grants are what they claim to be - Power BI
    opens its own connection with these same credentials."""
    from sqlalchemy import create_engine

    user, password = reporting_credentials()
    return create_engine(_url(user, password), future=True, **kwargs)


def provision(verbose=True):
    """Idempotent. Re-run after any dbt build that adds a mart, and after changing the password."""
    role, password = reporting_credentials()
    database = os.getenv("POSTGRES_DB")
    owner = os.getenv("POSTGRES_USER")

    role_id = sql.Identifier(role)
    db_id = sql.Identifier(database)
    owner_id = sql.Identifier(owner)
    analytics_id = sql.Identifier(ANALYTICS_SCHEMA)

    engine = build_owner_engine(isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        cursor = connection.connection.driver_connection.cursor()

        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        ).scalar()

        # LOGIN and nothing else, identical to the agent role. A BI tool needs no more authority
        # than an agent does, and reporting roles are the ones that quietly accumulate it.
        verb = "ALTER" if exists else "CREATE"
        cursor.execute(
            sql.SQL(
                verb + " ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(role_id, sql.Literal(password))
        )

        cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db_id, role_id))

        # Read analytics, and only analytics. USAGE makes the objects addressable; SELECT makes
        # them readable; neither implies the other.
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(analytics_id, role_id))
        cursor.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(analytics_id, role_id)
        )

        # The line that keeps the grant alive across dbt rebuilds - the same one that matters for
        # the agent. Without it the dashboard works until the next DAG run, then breaks with a
        # permission error that looks like a Power BI problem and is not.
        cursor.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
            ).format(owner_id, analytics_id, role_id)
        )

        # Explicit denials. No-ops on a clean database; written so the intent lives in code and a
        # hand-issued grant is undone by the next run.
        for schema in FORBIDDEN_SCHEMAS:
            schema_id = sql.Identifier(schema)
            cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(schema_id, role_id))
            cursor.execute(
                sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(schema_id, role_id)
            )

        # The one place this role is TIGHTER than the agent: no audit access whatsoever. The agent
        # holds INSERT because it must record its own tool calls; a dashboard has nothing to
        # record, and read access would expose every query the agent ever ran to a BI user.
        audit_id = sql.Identifier(AUDIT_SCHEMA)
        cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(audit_id, role_id))
        cursor.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(audit_id, role_id)
        )

        cursor.execute(
            sql.SQL("ALTER ROLE {} SET search_path = {}, pg_catalog").format(role_id, analytics_id)
        )
        cursor.execute(
            sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                role_id, sql.Literal(STATEMENT_TIMEOUT)
            )
        )

    if verbose:
        print(f"Provisioned read-only reporting role '{role}':")
        print(f"  CONNECT   {database}")
        print(f"  USAGE     {ANALYTICS_SCHEMA}")
        print(f"  SELECT    {ANALYTICS_SCHEMA}.* ({len(ALLOWED_TABLES)} objects)")
        print(f"  denied    {', '.join(FORBIDDEN_SCHEMAS)}, {AUDIT_SCHEMA} (no USAGE, no SELECT)")
        print(f"  session   search_path={ANALYTICS_SCHEMA}, {STATEMENT_TIMEOUT} statement timeout")
    return role


def verify():
    """Proves the boundary by using it, not by re-reading the grants that created it.

    The guardrail lesson applied to the second consumer: `GRANT` succeeding says nothing about what was
    left reachable. Every line below is a live attempt over the reporting role's own connection.
    """
    role, _ = reporting_credentials()
    engine = build_reporting_engine()
    ok = True

    print(f"\nVERIFYING '{role}' against the live warehouse")
    print("=" * 78)

    print(f"\n  Should ALLOW - the six {ANALYTICS_SCHEMA} objects Power BI imports:")
    for table in ALLOWED_TABLES:
        try:
            with engine.begin() as connection:
                n = connection.execute(
                    text(f"SELECT count(*) FROM {ANALYTICS_SCHEMA}.{table}")
                ).scalar()
            print(f"    ok      {ANALYTICS_SCHEMA}.{table:<24} {n:>7,} rows")
        except Exception as error:
            ok = False
            print(f"    FAILED  {ANALYTICS_SCHEMA}.{table:<24} {str(error).splitlines()[0][:60]}")

    print("\n  Should DENY - schemas a reporting tool has no business reading:")
    probes = [("raw", "daily_revenue"), ("staging", "stg_daily_revenue"),
              ("intermediate", "int_marketing_spend_allocated"), (AUDIT_SCHEMA, "agent_tool_calls")]
    for schema, table in probes:
        try:
            with engine.begin() as connection:
                connection.execute(text(f"SELECT 1 FROM {schema}.{table} LIMIT 1"))
            ok = False
            print(f"    LEAK    {schema}.{table} was readable")
        except Exception as error:
            reason = str(getattr(error, "orig", error)).splitlines()[0][:56]
            print(f"    denied  {schema + '.' + table:<44} {reason}")

    print("\n  Should DENY - writes to analytics, which this role must never perform:")
    writes = [
        ("INSERT", f"INSERT INTO {ANALYTICS_SCHEMA}.dim_product (sku_id) VALUES ('X')"),
        ("UPDATE", f"UPDATE {ANALYTICS_SCHEMA}.dim_product SET supplier = 'X'"),
        ("DELETE", f"DELETE FROM {ANALYTICS_SCHEMA}.dim_product"),
        ("DROP", f"DROP TABLE {ANALYTICS_SCHEMA}.dim_product"),
        ("INSERT-audit", f"INSERT INTO {AUDIT_QUALIFIED} (investigation_id) VALUES ('X')"),
    ]
    for label, statement in writes:
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
            ok = False
            print(f"    LEAK    {label} succeeded")
        except Exception as error:
            reason = str(getattr(error, "orig", error)).splitlines()[0][:56]
            print(f"    denied  {label:<44} {reason}")

    print("\n" + "=" * 78)
    print("  BOUNDARY HOLDS" if ok else "  CHECK FAILED - see LEAK lines above")
    return 0 if ok else 1


if __name__ == "__main__":
    provision()
    raise SystemExit(verify() if "--verify" in sys.argv else 0)
