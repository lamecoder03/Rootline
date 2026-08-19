# Turns a free-text brief into a structured verdict, then compares that verdict to the answer key.
# Exists in two halves on purpose: extraction is a judgement call and is done by a model, but the
# comparison is mechanical, so nothing about pass/fail depends on a model's opinion of "close enough".
# The extracted fields are saved with each result so a disputed grade can be checked against the brief.

from __future__ import annotations

from dataclasses import dataclass, field

from .. import config as cfg
from ..llm import ToolSpec, UserTurn
from .answer_key import CAUSE_FAMILIES

# What a missing field means when the extractor omits it. Every default is the reading LEAST
# favourable to the agent, so a sloppy extraction can cost a brief a check but can never gift it
# one. Smaller models drop optional-looking fields even when the schema marks them required.
_DEFAULTS = {
    "secondary_causes": [],
    "explicitly_ruled_out": [],
    "confidence": "High",
    "states_cause_preceded_effect": False,
    "cites_specific_figures": False,
    "declares_itself_partial": False,
    "evidence_quotes": [],
}

_EXTRACTION_SCHEMA = {
        "type": "object",
        "properties": {
            "primary_cause": {
                "type": "string", "enum": list(CAUSE_FAMILIES),
                "description": (
                    "The single cause family the brief settles on. Use 'none_identifiable' if "
                    "the brief concludes no cause is supported by the data, or leaves the "
                    "question genuinely open rather than naming a leading hypothesis."
                ),
            },
            "secondary_causes": {
                "type": "array", "items": {"type": "string", "enum": list(CAUSE_FAMILIES)},
                "description": "Other causes the brief offers as jointly plausible. Often empty.",
            },
            "explicitly_ruled_out": {
                "type": "array", "items": {"type": "string", "enum": list(CAUSE_FAMILIES)},
                "description": (
                    "Cause families the brief states it ELIMINATED and supports with a figure. "
                    "A cause merely not mentioned is NOT ruled out. Saying 'marketing spend was "
                    "flat at $X/day across the window' counts; 'marketing was considered' does not."
                ),
            },
            "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
            "states_cause_preceded_effect": {
                "type": "boolean",
                "description": (
                    "True only if the brief explicitly says the cause moved BEFORE revenue did, "
                    "with dates - e.g. spend changed on the 17th, revenue on the 22nd. A brief "
                    "that merely notes both changed in the same window is false."
                ),
            },
            "cites_specific_figures": {
                "type": "boolean",
                "description": "True if the cause section quotes real dated numbers, not vague claims.",
            },
            "declares_itself_partial": {
                "type": "boolean",
                "description": "True if the brief says it was cut short or is incomplete.",
            },
            "evidence_quotes": {
                "type": "array", "items": {"type": "string"},
                "description": "Up to 3 short verbatim quotes carrying the brief's key numeric claims.",
            },
        },
        "required": ["primary_cause", "secondary_causes", "explicitly_ruled_out", "confidence",
                     "states_cause_preceded_effect", "cites_specific_figures",
                     "declares_itself_partial", "evidence_quotes"],
    "additionalProperties": False,
}

EXTRACTION_TOOL = ToolSpec(
    name="record_brief_claims",
    description="Record what the brief actually claims. Report only what is written.",
    input_schema=_EXTRACTION_SCHEMA,
)

EXTRACTION_SYSTEM = """\
You are grading infrastructure, not an analyst. Read the brief and record exactly what it \
claims, using the tool. Do not evaluate whether the brief is correct, do not use any knowledge \
of what actually happened, and do not fill gaps with inference. If the brief does not say \
something, it does not claim it.
"""

EXTRACTION_REQUEST = """The brief:

---
{brief}
---"""

CONFIDENCE_RANK = {"Low": 0, "Medium": 1, "High": 2}


@dataclass
class Grade:
    anomaly_key: str
    label: str
    expected_cause: str
    claimed_cause: str
    cause_correct: bool
    checks: dict = field(default_factory=dict)
    passed: bool = False
    extraction: dict = field(default_factory=dict)
    failure_notes: list = field(default_factory=list)


def _normalise(claims):
    """Fills anything the extractor omitted with the least generous reading, and drops values
    outside the enum. A grade must never depend on the extractor being tidy."""
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in (claims or {}).items() if v is not None})

    families = set(CAUSE_FAMILIES)
    if out.get("primary_cause") not in families:
        raise RuntimeError(f"extraction returned no valid primary_cause: {claims!r}")
    for key in ("secondary_causes", "explicitly_ruled_out"):
        value = out.get(key) or []
        if isinstance(value, str):
            value = [value]
        out[key] = [v for v in value if v in families]
    if out.get("confidence") not in CONFIDENCE_RANK:
        out["confidence"] = "High"
    for key in ("states_cause_preceded_effect", "cites_specific_figures",
                "declares_itself_partial"):
        out[key] = bool(out.get(key))
    return out


def extract_claims(brief, provider=None):
    """A separate model call with the tool FORCED, so the reply is always the structured record
    and never prose about the brief. Goes through the neutral provider like everything else -
    the grader is not allowed its own vendor client, or the pivot would only be half-done."""
    provider = provider or cfg.build_provider()
    reply = provider.chat(
        system=EXTRACTION_SYSTEM,
        turns=[UserTurn(EXTRACTION_REQUEST.format(brief=brief))],
        tools=[EXTRACTION_TOOL],
        max_tokens=cfg.MAX_TOKENS,
        require_tool=EXTRACTION_TOOL.name,
    )
    for call in reply.tool_calls:
        if call.name == EXTRACTION_TOOL.name:
            return _normalise(call.arguments)
    raise RuntimeError(f"extraction produced no tool call (stop={reply.stop})")


def grade(scenario, brief, provider=None):
    """Mechanical comparison against the key. Every check is a boolean with a stated reason, so
    a failing scenario says which requirement it missed rather than just scoring low."""
    # An empty or stub brief is a failure to produce output, NOT a claim of "no cause found".
    # The first eval run graded a zero-byte brief as `none_identifiable`, which is a verdict
    # invented by the defaulting logic rather than read from a document. Caught before the
    # extractor runs, so it also costs no quota.
    body = (brief or "").strip()
    if len(body) < 200 or body.startswith("**No brief was produced."):
        return Grade(
            anomaly_key=scenario.anomaly_key, label=scenario.label,
            expected_cause=scenario.expected_cause, claimed_cause="none_produced",
            cause_correct=False, checks={"produced a brief at all": False}, passed=False,
            extraction={}, failure_notes=["no brief was produced - nothing to grade"],
        )

    claims = extract_claims(brief, provider=provider)
    claimed = claims["primary_cause"]
    accepted = {scenario.expected_cause, *scenario.acceptable_alternatives}

    checks, notes = {}, []

    cause_correct = claimed in accepted
    checks["names the expected cause"] = cause_correct
    if not cause_correct:
        notes.append(f"concluded '{claimed}', expected one of {sorted(accepted)}")

    for family in scenario.must_rule_out:
        ok = family in claims["explicitly_ruled_out"]
        checks[f"explicitly rules out {family}"] = ok
        if not ok:
            notes.append(f"did not explicitly rule out '{family}'")

    if scenario.must_identify_lag:
        ok = claims["states_cause_preceded_effect"]
        checks["identifies the cause-before-effect lag"] = ok
        if not ok:
            notes.append("did not state that the cause preceded the effect")

    checks["cites specific figures"] = claims["cites_specific_figures"]
    if not claims["cites_specific_figures"]:
        notes.append("cause section lacks dated, specific figures")

    if scenario.max_confidence:
        ok = CONFIDENCE_RANK[claims["confidence"]] <= CONFIDENCE_RANK[scenario.max_confidence]
        checks[f"confidence at most {scenario.max_confidence}"] = ok
        if not ok:
            notes.append(f"claimed {claims['confidence']} confidence, "
                         f"key allows at most {scenario.max_confidence}")

    return Grade(
        anomaly_key=scenario.anomaly_key, label=scenario.label,
        expected_cause=scenario.expected_cause, claimed_cause=claimed,
        cause_correct=cause_correct, checks=checks,
        passed=all(checks.values()), extraction=claims, failure_notes=notes,
    )
