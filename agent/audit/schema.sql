-- Append-only record of every attempted agent tool call, rejected ones included.
-- Exists because a guardrail that blocks silently leaves no evidence it ever fired; the audit
-- trail is what turns "the agent cannot read raw" into something provable after the fact.
-- Append-only is enforced twice: the agent role is granted INSERT and nothing else, and a
-- trigger refuses UPDATE and DELETE from any role, including the owner.
--
-- Applied by agent/guardrails/provision.py, which substitutes :agent_role safely. Idempotent.

CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS audit.agent_tool_calls (
    audit_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at         timestamptz  NOT NULL DEFAULT now(),
    investigation_id    text         NOT NULL,
    call_index          integer      NOT NULL,
    tool_name           text         NOT NULL,
    tool_input          jsonb        NOT NULL,
    generated_sql       text,
    executed_sql        text,
    validation_outcome  text         NOT NULL,
    rejection_code      text,
    rejection_reason    text,
    tables_referenced   text[],
    row_count           integer,
    duration_ms         integer,
    db_role             text         NOT NULL DEFAULT current_user,
    error_message       text,
    CONSTRAINT agent_tool_calls_outcome_ck
        CHECK (validation_outcome IN ('pass', 'reject', 'error', 'budget_exceeded')),
    CONSTRAINT agent_tool_calls_reject_has_reason_ck
        CHECK (validation_outcome <> 'reject' OR rejection_code IS NOT NULL)
);

COMMENT ON TABLE audit.agent_tool_calls IS
    'Append-only audit trail of every attempted agent tool call. INSERT only for the agent role; '
    'UPDATE and DELETE are refused by trigger for every role. One row per attempt, including '
    'attempts refused by the SQL validator before they reached the database.';

CREATE INDEX IF NOT EXISTS agent_tool_calls_investigation_idx
    ON audit.agent_tool_calls (investigation_id, call_index);
CREATE INDEX IF NOT EXISTS agent_tool_calls_outcome_idx
    ON audit.agent_tool_calls (validation_outcome, occurred_at DESC);

-- Second layer of append-only. Grants stop the agent role from rewriting history; this stops
-- anyone from doing it, which is the property an audit trail actually needs. It is not
-- tamper-proof against the table owner, who can drop the trigger - that would take a separate
-- privileged action, which is the point: quiet edits become loud ones.
CREATE OR REPLACE FUNCTION audit.refuse_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'audit.agent_tool_calls is append-only; % is refused', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS agent_tool_calls_append_only ON audit.agent_tool_calls;
CREATE TRIGGER agent_tool_calls_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.agent_tool_calls
    FOR EACH STATEMENT EXECUTE FUNCTION audit.refuse_mutation();
