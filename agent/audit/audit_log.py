# Writes one row to audit.agent_tool_calls for every attempted tool call, whatever the outcome.
# Exists so a rejection leaves a trace: without it, a blocked query and a query never asked are
# indistinguishable afterwards, and the guardrails become a claim rather than a record.
# Writes over the agent's own connection, which holds INSERT on this table and nothing else -
# so the process that is being audited cannot read or amend what was written about it.

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text

from ..guardrails import config as cfg

_INSERT = text(
    f"""
    INSERT INTO {cfg.AUDIT_QUALIFIED} (
        investigation_id, call_index, tool_name, tool_input,
        generated_sql, executed_sql, validation_outcome,
        rejection_code, rejection_reason, tables_referenced,
        row_count, duration_ms, error_message
    ) VALUES (
        :investigation_id, :call_index, :tool_name, CAST(:tool_input AS jsonb),
        :generated_sql, :executed_sql, :validation_outcome,
        :rejection_code, :rejection_reason, :tables_referenced,
        :row_count, :duration_ms, :error_message
    )
    """
)
# No RETURNING clause, and that is the grant working rather than an omission: RETURNING reads
# the row back, so it needs SELECT, which this role does not have on this table. The writer
# cannot see what it wrote - which is the property append-only is meant to give.

VALID_OUTCOMES = ("pass", "reject", "error", "budget_exceeded")


@dataclass
class AuditLog:
    """One instance per investigation. Holds the engine and the investigation id so every call
    site records the same correlation key and the trail reads as a sequence, not a pile."""

    engine: object
    investigation_id: str

    def record(
        self,
        call_index,
        tool_name,
        tool_input,
        validation_outcome,
        generated_sql=None,
        executed_sql=None,
        rejection_code=None,
        rejection_reason=None,
        tables_referenced=None,
        row_count=None,
        duration_ms=None,
        error_message=None,
    ):
        """Fails closed. If the audit row cannot be written the exception propagates rather than
        being swallowed, because an unlogged tool call is the one outcome the design does not
        permit - it is better for the investigation to stop than to run unobserved."""
        if validation_outcome not in VALID_OUTCOMES:
            raise ValueError(f"validation_outcome must be one of {VALID_OUTCOMES}")

        with self.engine.begin() as connection:
            connection.execute(
                _INSERT,
                {
                    "investigation_id": self.investigation_id,
                    "call_index": call_index,
                    "tool_name": tool_name,
                    "tool_input": json.dumps(tool_input, default=str),
                    "generated_sql": generated_sql,
                    "executed_sql": executed_sql,
                    "validation_outcome": validation_outcome,
                    "rejection_code": rejection_code,
                    "rejection_reason": rejection_reason,
                    "tables_referenced": list(tables_referenced) if tables_referenced else None,
                    "row_count": row_count,
                    "duration_ms": duration_ms,
                    "error_message": error_message,
                },
            )


def read_trail(owner_engine, investigation_id):
    """Reads the trail back. Takes an OWNER engine on purpose: the agent role has no SELECT on
    this table, so reviewing the log is an action a human takes with a different identity."""
    with owner_engine.begin() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT audit_id, call_index, tool_name, validation_outcome,
                       rejection_code, generated_sql, executed_sql, row_count,
                       db_role, occurred_at
                FROM {cfg.AUDIT_QUALIFIED}
                WHERE investigation_id = :investigation_id
                ORDER BY call_index, audit_id
                """
            ),
            {"investigation_id": investigation_id},
        )
        return [row._mapping for row in rows]
