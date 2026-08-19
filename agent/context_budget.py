# Keeps a growing investigation inside the provider's per-request token ceiling.
# Exists because Groq's free tier meters 8,000 tokens per minute and charges prompt+max_tokens up
# front, so an investigation that gathers evidence for ten calls eventually cannot send its own
# history. Elides the OLDEST tool results first and tells the model what it lost, so the brief
# cannot silently rest on figures that are no longer in front of it.

from __future__ import annotations

from .llm import AssistantTurnRef, ToolResult, ToolResultsTurn, UserTurn

# Measured, not assumed: 8,152 characters of prompt billed as 2,042 tokens on gpt-oss-120b,
# i.e. 3.99 chars/token on prose. Set to 3.3 rather than 3.99 because the conversation is not
# prose - tool calls carry JSON punctuation and per-message framing the character count cannot
# see, and a first pass at 3.7 still overshot the ceiling by 70 tokens on the twentieth call.
# The estimate must err high; the cost of guessing high is a slightly shorter brief, and the
# cost of guessing low is a dead investigation nineteen calls in.
CHARS_PER_TOKEN = 3.3

# How many of the most recent tool results survive untouched. The last few are what the model is
# actively reasoning over; the early ones are usually orientation queries whose conclusion has
# already been absorbed. Four is enough to hold a hypothesis and its two checks.
KEEP_FULL_RESULTS = 4

# What replaces an elided result. Deliberately does NOT repeat the SQL: assistant turns are never
# elided, so the statement is still visible in the tool call directly above this message, and
# carrying it twice was paying for the same text at both ends of the window.
ELISION = (
    "[{rows} rows returned. Rows elided to fit the context window - the query above still shows "
    "what was asked. These figures are no longer in front of you: re-run the query if the brief "
    "needs them, and do NOT quote numbers you can no longer see.]"
)


class ContextTooTight(RuntimeError):
    """Raised when even a fully elided conversation cannot fit. A distinct type because the fix
    is configuration, not a retry - the caller should not confuse it with a rate limit."""


def estimate_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _turn_chars(turn):
    if isinstance(turn, UserTurn):
        return len(turn.text)
    if isinstance(turn, AssistantTurnRef):
        total = len(turn.turn.text or "")
        for call in turn.turn.tool_calls:
            total += len(str(call.arguments)) + len(call.name) + 40
        return total
    if isinstance(turn, ToolResultsTurn):
        return sum(len(r.content) + 30 for r in turn.results)
    return 0


def _elide(result):
    """Rewrites one tool result down to its shape. Parses the payload rather than slicing the
    string, so an elided result is still valid JSON-adjacent text the model can read."""
    import json

    try:
        rows = json.loads(result.content).get("row_count", "?")
    except (ValueError, AttributeError):
        return ToolResult(id=result.id, content=result.content[:200], is_error=result.is_error)
    return ToolResult(id=result.id, content=ELISION.format(rows=rows), is_error=result.is_error)


def fit(turns, fixed_tokens, limit, reserve_output, min_output, safety=250):
    """Returns (turns, output_tokens_allowed), guaranteeing the projected request fits.

    This function owns the whole decision. An earlier version let the caller floor the answer
    length at a minimum afterwards, which is what put a request 70 tokens over the ceiling on the
    nineteenth call - a floor applied after the arithmetic is not a floor, it is an overrun.

    Order of sacrifice, most expendable first:
      1. answer length, down to `min_output`
      2. old result sets, oldest first, keeping the most recent KEEP_FULL_RESULTS intact
      3. the recent result sets too, newest held longest
    """
    budget = limit - safety - fixed_tokens
    working = list(turns)

    def projected():
        return int(sum(_turn_chars(t) for t in working) / CHARS_PER_TOKEN) + 1

    def affordable():
        return max(budget - projected(), 0)

    result_turns = [i for i, t in enumerate(working) if isinstance(t, ToolResultsTurn)]

    def elide_next(candidates):
        pending = [i for i in candidates if not getattr(working[i], "_elided", False)]
        if not pending:
            return False
        elided = ToolResultsTurn([_elide(r) for r in working[pending[0]].results])
        elided._elided = True
        working[pending[0]] = elided
        return True

    old = result_turns[:-KEEP_FULL_RESULTS] if len(result_turns) > KEEP_FULL_RESULTS else []
    recent = result_turns[-KEEP_FULL_RESULTS:]

    # Phase 1: buy back a full-length answer by eliding OLD results only.
    while affordable() < reserve_output and elide_next(old):
        pass

    # Phase 2: the recent results are worth more than a long brief, so they are only sacrificed
    # to clear `min_output` - never merely to lengthen the answer. Getting this the wrong way
    # round threw away every result the model was actively reasoning over, on the twentieth call,
    # to buy 160 tokens of prose it did not need.
    while affordable() < min_output and elide_next(recent):
        pass

    allowed = min(reserve_output, affordable())
    if allowed < min_output:
        raise ContextTooTight(
            f"Even with every result elided, only {allowed} output tokens fit inside the "
            f"{limit}-token request ceiling (fixed overhead {fixed_tokens}, history "
            f"{projected()}). Raise LLM_CONTEXT_TOKEN_LIMIT, shorten the system prompt, or "
            f"lower MAX_TOOL_CALLS."
        )
    return working, allowed
