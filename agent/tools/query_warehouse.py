# The agent's one tool: run a SELECT against the six allowlisted analytics tables.
# Exists as the single hole in the wall - every other capability the model has is reasoning, so
# this file is the entire attack surface and is deliberately short enough to read in one sitting.
# Implementation is Day 7's pipeline in order: budget -> validate -> execute read-only -> audit.

from __future__ import annotations

import json
import time
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ..guardrails import config as gcfg
from ..guardrails.call_budget import ToolCallBudgetExceeded
from ..guardrails.sql_validator import SqlValidationError, validate

TOOL_NAME = "query_warehouse"

# How much of a result set is allowed back into the conversation. This is NOT a guardrail and it
# does not touch one: gcfg.MAX_ROWS still bounds what the database returns and the audit still
# records the true row count. This bounds what is fed to the MODEL, which is a different problem
# with a different limit - a free-tier context budget. Found the hard way: a validator-approved
# `SELECT * ... LIMIT 100000` came back correctly capped at 1,000 rows, and those 1,000 rows of
# JSON then made the next request too large for the provider to accept at all.
# Sized by measurement in both directions. Too big and the request stops fitting; too SMALL
# and it costs more than it saves - at 900 chars an ordinary 7-row result was truncated, and
# the model spent five consecutive tool calls re-querying the same table with fewer columns
# trying to get a complete answer. 1,800 chars (~545 tokens) holds a normal aggregate whole,
# and four of them still fit the 8,000-token ceiling - see agent/config.py.
MAX_RESULT_CHARS = 1800

# The description IS the interface. The model never sees the validator's source, so every rule it
# has to satisfy is stated here - otherwise it learns the boundary by tripping over it, and each
# rejection costs a tool call out of a budget of 20.
TOOL_DEFINITION = {
    "name": TOOL_NAME,
    "description": (
        "Run a single read-only SELECT against the analytics warehouse and get the rows back "
        "as JSON.\n\n"
        "Allowed tables (schema `analytics`, nothing else is reachable):\n"
        "  - fct_daily_revenue        date x category x channel x region. Columns include "
        "order_date, category, channel, region, orders, units, gross_revenue, "
        "marketing_spend_usd, impressions, clicks, return_on_ad_spend, is_holiday, holiday_name, "
        "is_retail_event, day_of_week, is_weekend.\n"
        "  - fct_daily_margin         same grain. units, gross_revenue, estimated_cogs, "
        "estimated_gross_margin, estimated_gross_margin_pct, cost_basis_coverage_pct.\n"
        "  - fct_daily_stockout       snapshot_date x category x region (NO channel - stock is "
        "physical). skus_tracked, skus_out_of_stock, skus_below_reorder_point, "
        "total_units_on_hand, stockout_rate_pct, has_stockout, stocked_out_sku_ids.\n"
        "  - dim_product              sku_id grain. product_name, category, subcategory, "
        "unit_cost, supplier, primary_region, is_margin_calculable.\n"
        "  - detected_anomalies       one row per detected incident. anomaly_key, cell_key, "
        "category, channel, region, start_date, end_date, day_count, direction, peak_date, "
        "peak_z_score, peak_delta_pct, total_revenue_delta_usd, min_q_value.\n"
        "  - detected_anomaly_points  one row per flagged day, joined by anomaly_key. "
        "order_date, gross_revenue, expected_revenue, delta_pct, z_score, p_value, q_value.\n\n"
        "Rules enforced before execution - a query that breaks one is rejected and still costs "
        "you a tool call:\n"
        "  - Exactly ONE statement. No semicolon-chained statements.\n"
        "  - SELECT only. No INSERT/UPDATE/DELETE/DROP/CREATE, no SELECT INTO, no FOR UPDATE, "
        "no EXPLAIN, no UNION at the top level (use a CTE or two separate calls instead).\n"
        "  - Only the six tables above. CTEs are fine; the tables inside them are still checked.\n"
        "  - Functions are allowlisted: aggregates, maths, coalesce/nullif, date_trunc, extract, "
        "to_char, lower/upper/trim/substring/split_part/length, and window functions all work. "
        "Anything else is rejected.\n"
        "  - A LIMIT of 1000 is injected if you omit one, and clamped if you ask for more. "
        "Aggregate rather than pulling raw rows when the window is wide.\n\n"
        "Money columns are numeric; they come back as strings in JSON to avoid float rounding."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The single SELECT statement to run.",
            },
            "purpose": {
                "type": "string",
                "description": (
                    "One short sentence on what this query is meant to establish or rule out. "
                    "Recorded in the audit log next to the SQL."
                ),
            },
        },
        "required": ["sql", "purpose"],
        "additionalProperties": False,
    },
}


def _jsonable(value):
    """Decimal and date do not survive json.dumps. Money is stringified rather than floated,
    because the whole warehouse casts money to numeric(14,2) precisely to avoid binary float."""
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


class WarehouseTool:
    """Holds the three things one investigation needs to keep consistent: the read-only engine,
    the call budget, and the audit log. One instance per investigation."""

    def __init__(self, engine, budget, audit):
        self.engine = engine
        self.budget = budget
        self.audit = audit
        self.calls = []

    def run(self, tool_input):
        """Returns (result_text, is_error). Never raises for a rejected query - a refusal is
        information the model needs in order to retry, so it goes back as a tool_result.
        The one exception is budget exhaustion, which must stop the loop rather than inform it."""
        sql = (tool_input or {}).get("sql", "")
        purpose = (tool_input or {}).get("purpose", "")
        started = time.perf_counter()

        index = self.budget.spend(TOOL_NAME)

        outcome = "error"
        fields = {"rejection_code": None, "rejection_reason": None, "executed_sql": None,
                  "tables_referenced": None, "row_count": None, "error_message": None}
        try:
            checked = validate(sql)
            fields["executed_sql"] = checked.sql
            fields["tables_referenced"] = checked.tables

            with self.engine.begin() as connection:
                result = connection.execute(text(checked.sql))
                columns = list(result.keys())
                rows = [
                    {column: _jsonable(value) for column, value in zip(columns, row)}
                    for row in result.fetchall()
                ]

            fields["row_count"] = len(rows)
            outcome = "pass"
            payload = {
                "row_count": len(rows),
                "executed_sql": checked.sql,
                "rows": rows,
            }
            if checked.notes:
                payload["notes"] = checked.notes
            if not rows:
                payload["hint"] = (
                    "Zero rows. Check the date range, and remember fct_daily_stockout has no "
                    "channel column while the other facts do."
                )

            body = json.dumps(payload, default=str)
            if len(body) > MAX_RESULT_CHARS and rows:
                kept = rows
                while len(kept) > 1 and len(json.dumps(
                        {**payload, "rows": kept}, default=str)) > MAX_RESULT_CHARS:
                    kept = kept[: max(1, len(kept) * 2 // 3)]
                payload["rows"] = kept
                payload["truncated"] = True
                payload["hint"] = (
                    f"Result had {len(rows)} rows; only the first {len(kept)} are shown because "
                    "the full set does not fit in context. Re-run with an aggregate "
                    "(SUM/AVG/GROUP BY) or a narrower date range rather than asking for raw rows."
                )
                body = json.dumps(payload, default=str)
            return body, False

        except SqlValidationError as error:
            outcome = "reject"
            fields["rejection_code"] = error.code
            fields["rejection_reason"] = error.message
            return (
                f"QUERY REJECTED BY THE SQL VALIDATOR ({error.code}): {error.message} "
                "Rewrite the query to satisfy the rules in the tool description and try again.",
                True,
            )

        except DBAPIError as error:
            message = str(getattr(error, "orig", error)).strip().splitlines()[0]
            fields["error_message"] = message
            return (
                f"DATABASE ERROR: {message} The SQL was syntactically acceptable but the "
                "warehouse refused it. Check column names against the tool description.",
                True,
            )

        finally:
            duration = int((time.perf_counter() - started) * 1000)
            self.calls.append({
                "index": index, "purpose": purpose, "sql": sql,
                "outcome": outcome, "row_count": fields["row_count"],
                "rejection_code": fields["rejection_code"], "duration_ms": duration,
            })
            self.audit.record(
                call_index=index,
                tool_name=TOOL_NAME,
                tool_input={"sql": sql, "purpose": purpose},
                generated_sql=sql,
                validation_outcome=outcome,
                duration_ms=duration,
                **fields,
            )


__all__ = ["TOOL_DEFINITION", "TOOL_NAME", "WarehouseTool", "ToolCallBudgetExceeded", "gcfg"]
