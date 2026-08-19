# The tool-use loop: send the anomaly, run whatever queries the model asks for, return the brief.
# Exists as a hand-written loop rather than an SDK helper because budget exhaustion has to break
# the loop and still produce output - the model gets one final turn with no tools, which a runner
# that stops on its own terms cannot express. Provider-agnostic: it talks only to agent/llm.

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from . import config as cfg
from . import context_budget
from . import prompts
from .audit.audit_log import AuditLog
from .guardrails.call_budget import CallBudget, ToolCallBudgetExceeded
from .guardrails.db import build_agent_engine
from .llm import AssistantTurnRef, ToolResult, ToolResultsTurn, ToolSpec, UserTurn
from .tools.query_warehouse import TOOL_DEFINITION, TOOL_NAME, WarehouseTool

# The Day 7 tool definition, restated in the neutral shape. Same name, same description, same
# schema - only the wrapper changes, so nothing about what the model may ask for is re-specified.
WAREHOUSE_TOOL = ToolSpec(
    name=TOOL_DEFINITION["name"],
    description=TOOL_DEFINITION["description"],
    input_schema=TOOL_DEFINITION["input_schema"],
)


@dataclass
class Investigation:
    """Everything one run produced: the brief plus the evidence trail behind it, so a reader can
    check the brief against the queries rather than taking it on trust."""

    anomaly_key: str
    investigation_id: str
    brief: str
    truncated: bool
    tool_calls: list = field(default_factory=list)
    stop_reason: str = None
    elapsed_s: float = 0.0
    # How many earlier result sets had to be dropped from the model's view to fit the
    # provider's request ceiling. Non-zero means the brief was written with less in front
    # of it than the agent actually gathered, which the eval reports rather than hides.
    results_elided: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    provider: str = ""

    @property
    def calls_used(self):
        return len(self.tool_calls)

    @property
    def rejected_calls(self):
        return sum(1 for call in self.tool_calls if call["outcome"] != "pass")


def investigate(anomaly, provider=None, max_calls=None, verbose=True):
    """One anomaly in, one brief out. The budget, validator, read-only engine and audit log are
    Day 7's, imported and used as-is; this function only wires them into the model's turn cycle."""
    provider = provider or cfg.build_provider()
    investigation_id = f"INV-{anomaly['anomaly_key']}-{uuid.uuid4().hex[:6]}"

    engine = build_agent_engine()
    budget = CallBudget(max_calls=cfg.MAX_TOOL_CALLS if max_calls is None else max_calls)
    audit = AuditLog(engine=engine, investigation_id=investigation_id)
    tool = WarehouseTool(engine=engine, budget=budget, audit=audit)

    turns = [UserTurn(prompts.investigation_prompt(anomaly))]
    started = time.perf_counter()
    truncated = False
    usage = {"in": 0, "out": 0}
    stats = {"elided": 0}

    def ask(with_tools):
        # The free tier bills prompt + max_tokens against one 8,000-token-per-minute budget, so
        # the request has to be sized before it is sent, not retried after it is refused. Dropping
        # the tool schema on the final turn is worth ~600 tokens of that budget.
        fixed = context_budget.estimate_tokens(prompts.SYSTEM_PROMPT)
        if with_tools:
            fixed += context_budget.estimate_tokens(
                WAREHOUSE_TOOL.description + str(WAREHOUSE_TOOL.input_schema))
        sized, allowed = context_budget.fit(
            turns, fixed_tokens=fixed, limit=cfg.CONTEXT_TOKEN_LIMIT,
            reserve_output=cfg.MAX_TOKENS, min_output=cfg.MIN_OUTPUT_TOKENS)

        elided = sum(1 for a, b in zip(sized, turns) if a is not b)
        if verbose and elided:
            print(f"    [context] {elided} earlier result set(s) elided to fit "
                  f"{cfg.CONTEXT_TOKEN_LIMIT} tokens; answer capped at {allowed}")
        stats["elided"] += elided

        reply = provider.chat(
            system=prompts.SYSTEM_PROMPT,
            turns=sized,
            tools=[WAREHOUSE_TOOL] if with_tools else None,
            max_tokens=allowed,
        )
        usage["in"] += reply.input_tokens
        usage["out"] += reply.output_tokens
        return reply

    def write_brief(nudge):
        """The closing turn, with tools removed so the whole allowance goes to prose.

        Retries once if it comes back empty. That is not defensive padding: gpt-oss spends output
        budget on reasoning before emitting text, and the first eval run shipped a brief file
        containing nothing at all while still being graded - a scored verdict resting on an empty
        document is worse than a recorded failure, so an unrecoverable one is now labelled.
        """
        turns.append(UserTurn(nudge))
        reply = ask(with_tools=False)
        if not (reply.text or "").strip():
            if verbose:
                print("    [output] closing turn produced no text - retrying once")
            turns.append(UserTurn(prompts.OUTPUT_TRUNCATED_NUDGE))
            reply = ask(with_tools=False)
        if not (reply.text or "").strip():
            reply.text = (
                "**No brief was produced.** The model exhausted its output allowance on "
                "reasoning tokens twice without emitting a written brief. The evidence trail "
                "below is real and was gathered, but no analysis of it exists. This is a "
                "recorded failure, not a finding of 'no cause'."
            )
        return reply

    def finish(reply, stop):
        return Investigation(
            anomaly_key=anomaly["anomaly_key"], investigation_id=investigation_id,
            brief=reply.text, truncated=truncated, tool_calls=tool.calls, stop_reason=stop,
            elapsed_s=time.perf_counter() - started, results_elided=stats["elided"],
            input_tokens=usage["in"], output_tokens=usage["out"],
            provider=provider.describe(),
        )

    while True:
        reply = ask(with_tools=True)

        if reply.stop == "refusal":
            return finish(reply, "refusal")

        # "length" means the answer was cut off mid-generation, not that the model finished.
        # Returning here shipped an EMPTY brief: gpt-oss spends output budget on reasoning
        # tokens before it emits any text, so a turn can hit the cap having written nothing.
        # One retry without tools, which frees the ~600 tokens of tool schema for the answer.
        if reply.stop == "length" and not reply.tool_calls:
            if verbose:
                print("    [output] turn hit the token cap before writing - retrying "
                      "without tools so the whole allowance goes to the brief")
            return finish(write_brief(prompts.OUTPUT_TRUNCATED_NUDGE), "length_retry")

        if reply.stop != "tool_calls" or not reply.tool_calls:
            return finish(reply, reply.stop)

        turns.append(AssistantTurnRef(reply))

        results = []
        for call in reply.tool_calls:
            if call.name != TOOL_NAME:
                results.append(ToolResult(
                    id=call.id, is_error=True,
                    content=f"No such tool '{call.name}'. The only tool is '{TOOL_NAME}'."))
                continue
            try:
                payload, is_error = tool.run(call.arguments)
            except ToolCallBudgetExceeded:
                truncated = True
                payload, is_error = (
                    "TOOL CALL REFUSED: the tool-call ceiling for this investigation has been "
                    "reached. No further queries are possible.", True)
            results.append(ToolResult(id=call.id, content=payload, is_error=is_error))

            if verbose:
                if truncated:
                    print(f"    [--/{budget.max_calls}] BUDGET EXHAUSTED - forcing partial brief")
                elif tool.calls:
                    last = tool.calls[-1]
                    mark = "ok " if last["outcome"] == "pass" else "REJ"
                    # A DATABASE error leaves both row_count and rejection_code null - the query
                    # passed the validator and then failed in Postgres, which is a third outcome
                    # the two-branch version did not have a label for, and it crashed the sweep.
                    detail = (f"{last['row_count']} rows" if last["row_count"] is not None
                              else last["rejection_code"] or last["outcome"].upper())
                    print(f"    [{last['index']:>2}/{budget.max_calls}] {mark} {detail:<14} "
                          f"{(last['purpose'] or '')[:60]}")

        turns.append(ToolResultsTurn(results))

        if truncated:
            # Final turn with the tool removed, so the model cannot ask for anything else and has
            # to write from what it has. This is the graceful part of graceful degradation.
            return finish(write_brief(prompts.BUDGET_EXHAUSTED_NUDGE), "budget_exceeded")
