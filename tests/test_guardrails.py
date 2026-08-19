# Unit tests for the two guardrails that need no database: the SQL validator and the call budget.
# Exists so the walls can be changed with confidence - the attack harness proves they work today,
# these prove a future edit did not quietly open one of them.
# Plain asserts and a tiny runner rather than pytest, which is not a dependency of this project.
#
#   python -m tests.test_guardrails

from __future__ import annotations

import traceback

from agent.guardrails import config as cfg
from agent.guardrails.call_budget import (
    BudgetedInvestigation, CallBudget, ToolCallBudgetExceeded,
)
from agent.guardrails.sql_validator import SqlValidationError, validate

CASES = []


def case(function):
    CASES.append(function)
    return function


def rejects(sql, expected_code):
    try:
        result = validate(sql)
    except SqlValidationError as error:
        assert error.code == expected_code, (
            f"{sql!r}: expected {expected_code}, got {error.code} ({error.message})"
        )
        return
    raise AssertionError(f"{sql!r}: expected {expected_code}, but it was ALLOWED as {result.sql!r}")


# --- Statement shape --------------------------------------------------------------------

@case
def test_only_select_survives():
    for sql in (
        "DROP TABLE analytics.dim_product",
        "DELETE FROM analytics.dim_product",
        "UPDATE analytics.dim_product SET unit_cost = 0",
        "INSERT INTO analytics.dim_product (sku_id) VALUES ('x')",
        "TRUNCATE analytics.dim_product",
        "ALTER TABLE analytics.dim_product ADD COLUMN x text",
        "GRANT SELECT ON analytics.dim_product TO public",
        "CREATE TABLE analytics.x AS SELECT 1",
    ):
        rejects(sql, "NOT_A_SELECT")


@case
def test_unparseable_and_unmodelled_statements_are_refused():
    rejects("EXPLAIN SELECT * FROM analytics.dim_product", "UNSUPPORTED_STATEMENT")
    rejects("VACUUM analytics.dim_product", "UNSUPPORTED_STATEMENT")
    # sqlglot models SET as its own node rather than degrading it to a Command, so it is refused
    # a step earlier. Both paths refuse it; the assertion records which one actually fires.
    rejects("SET search_path = raw", "NOT_A_SELECT")
    rejects("", "EMPTY_QUERY")
    rejects("   ", "EMPTY_QUERY")


@case
def test_multi_statement_is_refused_by_count_not_by_semicolon():
    rejects("SELECT 1 FROM analytics.dim_product; DROP TABLE analytics.dim_product",
            "MULTI_STATEMENT")
    rejects("SELECT * FROM analytics.dim_product; SELECT * FROM analytics.dim_product",
            "MULTI_STATEMENT")
    # A trailing semicolon is one statement, and a semicolon inside a literal is not a statement
    # boundary at all - which is exactly what a text scan would get wrong.
    assert validate("SELECT * FROM analytics.dim_product;").sql
    assert validate("SELECT * FROM analytics.dim_product WHERE sku_id = 'a;b'").sql


@case
def test_a_keyword_in_a_column_or_literal_is_not_an_attack():
    for sql in (
        "SELECT sku_id AS drop_reason FROM analytics.dim_product",
        "SELECT * FROM analytics.dim_product WHERE product_name = 'DELETE FROM x'",
        "SELECT count(*) AS update_count FROM analytics.detected_anomalies",
    ):
        assert validate(sql).sql, f"{sql!r} should be allowed"


# --- Writes that look like reads --------------------------------------------------------

@case
def test_select_into_and_locking_reads_are_refused():
    rejects("SELECT * INTO evil FROM analytics.dim_product", "SELECT_INTO")
    rejects("SELECT * FROM analytics.dim_product FOR UPDATE", "ROW_LOCK")
    rejects("SELECT * FROM analytics.dim_product FOR SHARE", "ROW_LOCK")


@case
def test_known_dangerous_functions_keep_their_specific_message():
    # These are on DENIED_FUNCTIONS as well as off the allowlist. The allowlist alone would
    # refuse them; the denylist survives so the audit trail records which one it was.
    for sql in (
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_ls_dir('/')",
        "SELECT pg_sleep(600) FROM analytics.dim_product",
        "SELECT dblink('host=x', 'select 1')",
        "SELECT query_to_xml('select * from raw.daily_revenue', true, false, '')",
    ):
        rejects(sql, "DENIED_FUNCTION")


@case
def test_functions_are_an_allowlist_not_a_denylist():
    """The six that a denylist let through, measured live against the read-only role before the
    allowlist replaced it: pg_get_viewdef leaked the stockout view body (which names staging
    tables), txid_current forced a WAL write, repeat() materialised 64MB in one row."""
    for sql in (
        "SELECT pg_get_viewdef('analytics.fct_daily_stockout'::regclass) FROM analytics.dim_product",
        "SELECT txid_current() FROM analytics.dim_product",
        "SELECT current_setting('listen_addresses') FROM analytics.dim_product",
        "SELECT version() FROM analytics.dim_product",
        "SELECT pg_backend_pid() FROM analytics.dim_product",
        "SELECT has_table_privilege('raw.daily_revenue', 'SELECT') FROM analytics.dim_product",
        "SELECT repeat(md5(sku_id), 2000000) FROM analytics.dim_product",
        "SELECT lpad(sku_id, 1000000, 'a') FROM analytics.dim_product",
        "SELECT regexp_replace(sku_id, '(a+)+b', 'x') FROM analytics.dim_product",
        "SELECT pg_get_functiondef(1) FROM analytics.dim_product",
        "SELECT inet_server_addr() FROM analytics.dim_product",
    ):
        rejects(sql, "FUNCTION_NOT_ALLOWED")


@case
def test_cast_and_case_are_syntax_not_functions():
    # sqlglot models both as Func subclasses. Refusing them would refuse every money cast.
    assert validate("SELECT round(gross_revenue::numeric, 2) FROM analytics.fct_daily_revenue").sql
    assert validate(
        "SELECT CASE WHEN delta_pct < 0 THEN 'drop' ELSE 'spike' END FROM analytics.detected_anomalies"
    ).sql


@case
def test_legitimate_analyst_queries_still_pass():
    """The regression set. An over-tight function allowlist fails silently at Day 8 rather than
    loudly here, so every shape the agent is expected to write is asserted up front.
    Note the sqlglot renames: to_char parses as TimeToStr, date_trunc as TimestampTrunc,
    string_agg as GroupConcat - listing the Postgres spelling in config would not permit them."""
    T = "analytics.fct_daily_revenue"
    A = "analytics.detected_anomalies"
    P = "analytics.detected_anomaly_points"
    D = "analytics.dim_product"
    for sql in (
        f"SELECT category, sum(gross_revenue) FROM {T} GROUP BY 1",
        f"SELECT date_trunc('week', order_date) AS wk, sum(gross_revenue) FROM {T} GROUP BY 1",
        f"SELECT to_char(order_date, 'YYYY-MM') AS m, sum(gross_revenue) FROM {T} GROUP BY 1",
        f"SELECT extract(dow FROM order_date) AS d, avg(gross_revenue) FROM {T} GROUP BY 1",
        f"SELECT sum(gross_revenue) / nullif(sum(units), 0) AS aov FROM {T}",
        f"SELECT category, count(*) FILTER (WHERE is_margin_calculable) FROM {D} GROUP BY 1",
        f"SELECT coalesce(supplier, 'unknown') AS s, count(*) FROM {D} GROUP BY 1",
        f"SELECT order_date, sum(gross_revenue) OVER (ORDER BY order_date "
        f"ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) FROM {T}",
        f"SELECT cell_key, row_number() OVER (ORDER BY abs(peak_z_score) DESC) FROM {A}",
        f"SELECT order_date, lag(gross_revenue) OVER (ORDER BY order_date) FROM {T}",
        f"SELECT stddev(z_score), variance(z_score), corr(z_score, delta_pct) FROM {P}",
        f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY z_score) FROM {P}",
        f"SELECT split_part(cell_key, '|', 1) AS cat, count(*) FROM {A} GROUP BY 1",
        f"SELECT lower(trim(category)), upper(channel), length(sku_id) FROM {D}",
        f"SELECT greatest(z_score, 0), least(z_score, 0) FROM {P}",
        f"SELECT category, string_agg(DISTINCT sku_id, ',') FROM {D} GROUP BY 1",
        f"SELECT count(*) FROM {T} WHERE order_date > current_date - 30",
        f"WITH w AS (SELECT cell_key FROM {A} ORDER BY abs(peak_z_score) DESC LIMIT 3) "
        f"SELECT p.order_date, p.z_score FROM {P} p JOIN w ON p.cell_key = w.cell_key",
    ):
        try:
            validate(sql)
        except SqlValidationError as error:
            raise AssertionError(
                f"legitimate query refused as {error.code}: {error.message}\n  {sql}"
            ) from error


# --- The allowlist ----------------------------------------------------------------------

@case
def test_every_allowlisted_table_is_readable():
    for name in cfg.ALLOWED_TABLES:
        result = validate(f"SELECT * FROM {cfg.ANALYTICS_SCHEMA}.{name}")
        assert result.tables == (f"{cfg.ANALYTICS_SCHEMA}.{name}",)


@case
def test_tables_outside_the_allowlist_are_refused_wherever_they_hide():
    for sql in (
        "SELECT * FROM raw.daily_revenue",
        "SELECT * FROM staging.stg_product_master",
        "SELECT * FROM intermediate.int_category_cost_basis",
        "SELECT * FROM audit.agent_tool_calls",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM analytics.some_new_table",
        "WITH c AS (SELECT * FROM raw.daily_revenue) SELECT * FROM c",
        "SELECT (SELECT 1 FROM raw.product_master) FROM analytics.dim_product",
        "SELECT a.* FROM analytics.dim_product a JOIN raw.product_master b ON a.sku_id = b.sku_id",
        "SELECT * FROM analytics.dim_product WHERE sku_id IN (SELECT sku_id FROM raw.product_master)",
    ):
        rejects(sql, "TABLE_NOT_ALLOWED")


@case
def test_unqualified_names_are_rewritten_to_the_analytics_schema():
    result = validate("SELECT category FROM fct_daily_revenue")
    assert "analytics.fct_daily_revenue" in result.sql, result.sql
    assert result.tables == ("analytics.fct_daily_revenue",)
    rejects("SELECT * FROM daily_revenue", "TABLE_NOT_ALLOWED")


@case
def test_cte_aliases_are_not_mistaken_for_tables():
    result = validate(
        "WITH worst AS (SELECT cell_key FROM analytics.detected_anomalies) "
        "SELECT * FROM worst"
    )
    assert result.tables == ("analytics.detected_anomalies",)


@case
def test_a_query_reading_nothing_is_refused():
    rejects("SELECT 1", "NO_TABLE")
    rejects("SELECT now()", "NO_TABLE")


@case
def test_cross_database_references_are_refused():
    rejects("SELECT * FROM other_db.analytics.dim_product", "CROSS_DATABASE")


# --- The row cap ------------------------------------------------------------------------

@case
def test_limit_is_injected_clamped_or_left_alone():
    injected = validate("SELECT * FROM analytics.dim_product")
    assert injected.limit_was_injected and injected.limit_applied == cfg.MAX_ROWS
    assert f"LIMIT {cfg.MAX_ROWS}" in injected.sql

    clamped = validate("SELECT * FROM analytics.dim_product LIMIT 999999")
    assert not clamped.limit_was_injected and clamped.limit_applied == cfg.MAX_ROWS
    assert f"LIMIT {cfg.MAX_ROWS}" in clamped.sql

    kept = validate("SELECT * FROM analytics.dim_product LIMIT 5")
    assert kept.limit_applied == 5 and "LIMIT 5" in kept.sql

    tight = validate("SELECT * FROM analytics.dim_product", max_rows=10)
    assert tight.limit_applied == 10 and "LIMIT 10" in tight.sql


# --- The call budget --------------------------------------------------------------------

@case
def test_budget_refuses_the_call_that_crosses_the_ceiling():
    budget = CallBudget(max_calls=3)
    assert [budget.spend() for _ in range(3)] == [1, 2, 3]
    assert budget.exhausted and budget.remaining == 0
    for _ in range(4):
        try:
            budget.spend()
        except ToolCallBudgetExceeded:
            continue
        raise AssertionError("budget allowed a call past the ceiling")
    assert budget.spent == 3, "a refused call must not be counted as spent"
    assert "partial brief" in budget.stopped_reason


@case
def test_budget_charges_on_attempt_not_on_success():
    budget = CallBudget(max_calls=2)
    budget.spend("query_warehouse")
    try:
        raise RuntimeError("the tool itself failed after the budget was charged")
    except RuntimeError:
        pass
    assert budget.remaining == 1, "a failed call must still consume budget"


@case
def test_budget_rejects_a_nonsense_ceiling():
    for bad in (0, -1):
        try:
            CallBudget(max_calls=bad)
        except ValueError:
            continue
        raise AssertionError(f"max_calls={bad} should not be accepted")


@case
def test_context_manager_converts_exhaustion_into_a_clean_exit():
    investigation = BudgetedInvestigation(max_calls=2)
    executed = 0
    with investigation as budget:
        for _ in range(5):
            budget.spend()
            executed += 1
    assert executed == 2, executed
    assert investigation.aborted
    assert investigation.budget.exhausted


@case
def test_default_ceiling_comes_from_config():
    assert CallBudget().max_calls == cfg.MAX_TOOL_CALLS
    assert BudgetedInvestigation().budget.max_calls == cfg.MAX_TOOL_CALLS


def main():
    passed, failed = 0, []
    for function in CASES:
        try:
            function()
            passed += 1
            print(f"  PASS  {function.__name__}")
        except Exception:
            failed.append(function.__name__)
            print(f"  FAIL  {function.__name__}")
            print(traceback.format_exc())
    print(f"\n{passed} passed, {len(failed)} failed out of {len(CASES)}")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
