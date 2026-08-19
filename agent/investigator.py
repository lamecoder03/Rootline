# The tool-use loop: send the anomaly, run whatever queries the model asks for, return the brief.
# Exists as a hand-written loop rather than an SDK helper because budget exhaustion has to break
# the loop and still produce output - the model gets one final turn with no tools, which a runner
# that stops on its own terms cannot express. Provider-agnostic: it talks only to agent/llm.

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from . import config as cfg
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

    def ask(with_tools):
        reply = provider.chat(
            system=prompts.SYSTEM_PROMPT,
            turns=turns,
            tools=[WAREHOUSE_TOOL] if with_tools else None,
            max_tokens=cfg.MAX_TOKENS,
        )
        usage["in"] += reply.input_tokens
        usage["out"] += reply.output_tokens
        return reply

    def finish(reply, stop):
        return Investigation(
            anomaly_key=anomaly["anomaly_key"], investigation_id=investigation_id,
            brief=reply.text, truncated=truncated, tool_calls=tool.calls, stop_reason=stop,
            elapsed_s=time.perf_counter() - started,
            input_tokens=usage["in"], output_tokens=usage["out"],
            provider=provider.describe(),
        )

    while True:
        reply = ask(with_tools=True)

        if reply.stop == "refusal":
            return finish(reply, "refusal")
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
                    detail = (f"{last['row_count']} rows" if last["row_count"] is not None
                              else last["rejection_code"])
                    print(f"    [{last['index']:>2}/{budget.max_calls}] {mark} {detail:<14} "
                          f"{(last['purpose'] or '')[:60]}")

        turns.append(ToolResultsTurn(results))

        if truncated:
            # Final turn with the tool removed, so the model cannot ask for anything else and has
            # to write from what it has. This is the graceful part of graceful degradation.
            turns.append(UserTurn(prompts.BUDGET_EXHAUSTED_NUDGE))
            return finish(ask(with_tools=False), "budget_exceeded")
