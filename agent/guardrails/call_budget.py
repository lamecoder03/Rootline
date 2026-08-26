# Hard ceiling on tool calls in one investigation, enforced by refusing the call rather than by
# counting it and hoping the caller checks.
# Exists because an agent loop has no natural end: a model that keeps re-running a failing query
# will do so until something stops it, and "something" has to be code, not a token limit.
# The ceiling is a termination guarantee - at exhaustion the loop stops and a partial brief ships.

from __future__ import annotations

from dataclasses import dataclass, field

from . import config as cfg


class ToolCallBudgetExceeded(Exception):
    """Raised by spend() once the ceiling is reached. Deliberately an exception and not a return
    value: a caller that ignores a boolean keeps going, a caller that ignores this does not."""


@dataclass
class CallBudget:
    """Counts calls against a fixed ceiling and refuses the one that would cross it. Records what
    was spent on what, so the partial brief can say which questions were answered and which the
    ceiling cut off - a truncated investigation that does not say so is worse than none."""

    max_calls: int = cfg.MAX_TOOL_CALLS
    spent: int = 0
    calls: list = field(default_factory=list)
    stopped_reason: str = None

    def __post_init__(self):
        if self.max_calls < 1:
            raise ValueError("max_calls must be at least 1.")

    @property
    def remaining(self):
        return max(0, self.max_calls - self.spent)

    @property
    def exhausted(self):
        return self.spent >= self.max_calls

    def spend(self, tool_name="query_warehouse"):
        """Consumes one unit and returns the call's 1-based index, or raises if the ceiling is
        already reached. Charged on attempt, never on success: a call that fails validation still
        cost a model turn, and not charging for failures is exactly how a loop runs forever."""
        if self.exhausted:
            self.stopped_reason = (
                f"Tool-call ceiling of {self.max_calls} reached; "
                f"aborting the investigation and returning a partial brief."
            )
            raise ToolCallBudgetExceeded(self.stopped_reason)

        self.spent += 1
        self.calls.append(tool_name)
        return self.spent

    def summary(self):
        """One line for the audit trail and the head of the brief."""
        state = "EXHAUSTED" if self.exhausted else "ok"
        return f"{self.spent}/{self.max_calls} tool calls used ({state}); {self.remaining} remaining."


class BudgetedInvestigation:
    """Context-manager form for the agent loop: opens a budget, lets the body run, and converts
    exhaustion into a clean early exit instead of a crash - which is what "returns a partial
    brief rather than running unbounded" has to mean in code."""

    def __init__(self, max_calls=None):
        self.budget = CallBudget(max_calls=cfg.MAX_TOOL_CALLS if max_calls is None else max_calls)
        self.aborted = False

    def __enter__(self):
        return self.budget

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is ToolCallBudgetExceeded:
            self.aborted = True
            return True
        return False
