# Turns a finished brief into the action chain the objective names:
#   driver -> controllable lever -> action -> expected impact -> owner -> confidence -> monitoring.
# Exists because a diagnosis nobody can act on is not intelligence-to-action; extraction is one
# forced-tool model call over the brief ALONE, so no action can rest on a figure never gathered.

from __future__ import annotations

from . import config as cfg
from .eval.answer_key import CAUSE_FAMILIES
from .llm import ToolSpec, UserTurn

# Which functions can own a remediation. An open enum would let the model invent a plausible-
# sounding department that does not exist, and an action with no real owner is a wish.
OWNERS = (
    "Marketing",
    "Merchandising / Category Management",
    "Supply Chain / Inventory Planning",
    "Pricing",
    "Revenue Operations",
    "Finance",
    "No owner - not actionable",
)

# The distinction that makes this an action schema rather than a summary: a driver can be real,
# well-evidenced and still carry no lever anyone controls. Christmas is the canonical case - the
# correct response is to model it, not to fix it.
LEVER_STATUS = ("controllable", "not_controllable", "unknown")

STANCE = ("actionable", "partial", "abstain")

_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "stance": {
            "type": "string", "enum": list(STANCE),
            "description": (
                "'actionable' if at least one concrete action is supported by evidence in the "
                "brief. 'partial' if an action is supported but the brief is incomplete or its "
                "confidence is Low. 'abstain' if the brief identifies no cause, or identifies "
                "one that nobody controls - in that case recommended_actions MUST be empty."
            ),
        },
        "recommended_actions": {
            "type": "array",
            "description": (
                "Actions supported by evidence stated IN THE BRIEF. Empty is a valid and often "
                "correct answer. Never add an action to avoid returning an empty list."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "driver": {
                        "type": "string", "enum": list(CAUSE_FAMILIES),
                        "description": "The cause family the brief actually settles on.",
                    },
                    "driver_evidence": {
                        "type": "string",
                        "description": (
                            "A short VERBATIM quote from the brief carrying the figure that "
                            "establishes this driver. Must be text that appears in the brief. "
                            "If no quote in the brief establishes it, do not emit this action."
                        ),
                    },
                    "lever_status": {"type": "string", "enum": list(LEVER_STATUS)},
                    "controllable_lever": {
                        "type": "string",
                        "description": (
                            "The specific thing the business can change - a replenishment "
                            "trigger, a budget line, a price. If lever_status is "
                            "'not_controllable', state what is outside control instead."
                        ),
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "One concrete step, specific enough to assign. 'Raise the safety "
                            "stock floor for Electronics/West' is an action; 'improve inventory "
                            "management' is not."
                        ),
                    },
                    "expected_impact": {
                        "type": "string",
                        "description": (
                            "What the action is expected to recover or prevent, quantified in "
                            "dollars or percent WHERE THE BRIEF SUPPLIES THE FIGURE. If the "
                            "brief gives no figure to anchor it, say 'not quantifiable from "
                            "this investigation' rather than estimating one."
                        ),
                    },
                    "expected_impact_basis": {
                        "type": "string",
                        "description": (
                            "Which figure in the brief the expected impact is derived from, and "
                            "how. This field is what makes the impact checkable; if it would be "
                            "empty, the impact is invented and the action must be dropped."
                        ),
                    },
                    "owner": {"type": "string", "enum": list(OWNERS)},
                    "confidence": {
                        "type": "string", "enum": ["High", "Medium", "Low"],
                        "description": (
                            "Confidence in THIS ACTION. It can never exceed the brief's own "
                            "confidence in the diagnosis, and should be lower if the brief is "
                            "marked partial."
                        ),
                    },
                    "monitoring_metric": {
                        "type": "string",
                        "description": "The one number that shows whether the action worked.",
                    },
                    "monitoring_threshold": {
                        "type": "string",
                        "description": "The value or movement that means it worked, or did not.",
                    },
                    "monitoring_horizon": {
                        "type": "string",
                        "description": "When to check - a concrete interval, e.g. 'daily for 14 days'.",
                    },
                },
                "required": ["driver", "driver_evidence", "lever_status", "controllable_lever",
                             "action", "expected_impact", "expected_impact_basis", "owner",
                             "confidence", "monitoring_metric", "monitoring_threshold",
                             "monitoring_horizon"],
                "additionalProperties": False,
            },
        },
        "abstentions": {
            "type": "array",
            "description": (
                "Every place the brief stops short of supporting an action. This is the honest "
                "half of the output and is expected to be non-empty on most investigations."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "What no action could be recommended about.",
                    },
                    "why_no_action": {
                        "type": "string",
                        "description": (
                            "Why the brief does not support one - cause not identified, cause "
                            "not controllable, or investigation cut short before it was settled."
                        ),
                    },
                    "what_would_be_needed": {
                        "type": "string",
                        "description": "The specific evidence that would unlock an action here.",
                    },
                },
                "required": ["topic", "why_no_action", "what_would_be_needed"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["stance", "recommended_actions", "abstentions"],
    "additionalProperties": False,
}

ACTION_TOOL = ToolSpec(
    name="record_recommended_actions",
    description=(
        "Record the actions the brief's own evidence supports, and every place it supports none."
    ),
    input_schema=_ACTION_SCHEMA,
)

ACTION_SYSTEM = """\
You convert a finished investigation brief into recommended actions. You are not an analyst and \
you are not re-investigating: the brief in front of you is the ONLY evidence that exists. You \
have no warehouse access and no knowledge of what really happened.

The chain every action must complete:

  driver -> controllable lever -> action -> expected impact -> owner -> confidence -> monitoring

Three rules decide whether an action may exist at all:

1. **Grounded.** Every action's driver must be established by a figure written in the brief, and \
you must quote that figure verbatim. If you cannot quote it, the action does not exist.
2. **Controllable.** A real cause with no lever anyone owns produces no action. A holiday is not \
a failure to fix; a stockout is. When the driver is real but uncontrollable, record it as an \
abstention with lever_status reasoning, not as an action.
3. **Abstain rather than invent.** If the brief concludes no cause is identifiable, return an \
empty recommended_actions list and explain in abstentions. An empty list is a correct answer. A \
fabricated action is worse than no action, because someone will spend money on it.

Never estimate a dollar figure the brief does not contain. If the brief gives you no basis to \
quantify an impact, say so in that field - "not quantifiable from this investigation" is an \
acceptable and often correct expected_impact.

Confidence in an action can never exceed the brief's confidence in its diagnosis. If the brief \
declares itself PARTIAL, no action may be High confidence.
"""

ACTION_REQUEST = """The brief, in full:

---
{brief}
---

The queries this brief rests on, in the order they were run:

{trail}

Record the actions this evidence supports, and every place it supports none."""


def extract_actions(brief, trail, provider=None):
    """One forced-tool call over the brief text alone. Forced because prose about what one might
    do is exactly the failure mode - the chain is only auditable if it comes back as fields."""
    provider = provider or cfg.build_provider()
    reply = provider.chat(
        system=ACTION_SYSTEM,
        turns=[UserTurn(ACTION_REQUEST.format(brief=brief.strip(), trail=trail.strip()))],
        tools=[ACTION_TOOL],
        require_tool=ACTION_TOOL.name,
        max_tokens=cfg.MAX_TOKENS,
    )
    for call in reply.tool_calls:
        if call.name == ACTION_TOOL.name:
            return _normalise(call.arguments), reply
    raise RuntimeError(f"action extraction returned no tool call (stop={reply.stop!r})")


def _normalise(payload):
    """Fills anything the extractor dropped with the least actionable reading. A missing field
    must never be able to promote an action to something more confident than it earned."""
    out = {
        "stance": (payload or {}).get("stance"),
        "recommended_actions": list((payload or {}).get("recommended_actions") or []),
        "abstentions": list((payload or {}).get("abstentions") or []),
    }
    if out["stance"] not in STANCE:
        out["stance"] = "abstain" if not out["recommended_actions"] else "partial"

    cleaned = []
    for raw in out["recommended_actions"]:
        if not isinstance(raw, dict):
            continue
        action = dict(raw)
        action.setdefault("confidence", "Low")
        if action["confidence"] not in ("High", "Medium", "Low"):
            action["confidence"] = "Low"
        if action.get("owner") not in OWNERS:
            action["owner"] = "No owner - not actionable"
        if action.get("lever_status") not in LEVER_STATUS:
            action["lever_status"] = "unknown"
        for key in ("driver", "driver_evidence", "controllable_lever", "action",
                    "expected_impact", "expected_impact_basis", "monitoring_metric",
                    "monitoring_threshold", "monitoring_horizon"):
            action.setdefault(key, "")
        cleaned.append(action)
    out["recommended_actions"] = cleaned

    if not cleaned and out["stance"] == "actionable":
        out["stance"] = "abstain"
    return out


def cap_confidence(actions, brief_is_partial):
    """A partial investigation cannot support a High-confidence action, whatever the extractor
    said. Enforced in code rather than trusted to the prompt, because this is the guarantee the
    abstention discipline actually rests on."""
    if not brief_is_partial:
        return actions, 0
    lowered = 0
    for action in actions["recommended_actions"]:
        if action["confidence"] == "High":
            action["confidence"] = "Medium"
            action["capped_by_partial_brief"] = True
            lowered += 1
    return actions, lowered


def render_actions(actions, persona):
    """Renders ONE extracted action set for one persona. Deterministic on purpose: every persona
    reads the same JSON, so the analyst's dollar figure and the VP's are the same figure by
    construction rather than by two models happening to agree."""
    fields = persona.action_fields
    lines = []

    if not actions["recommended_actions"]:
        lines += [
            "**No action is recommended from this investigation.**", "",
            "This is a deliberate abstention, not an omission: the evidence gathered does not "
            "support a specific intervention, and an invented one would send someone to spend "
            "money against a cause that was never established.", "",
        ]
    else:
        for i, action in enumerate(actions["recommended_actions"], 1):
            lines.append(f"### Action {i} — {action['action']}")
            lines.append("")
            lines.append("| Step | Value |")
            lines.append("|---|---|")
            for field in fields:
                if field == "monitoring_plan":
                    value = (f"{action['monitoring_metric']} — {action['monitoring_threshold']}, "
                             f"checked {action['monitoring_horizon']}")
                    label = "Monitoring plan"
                else:
                    value = action.get(field, "")
                    label = field.replace("_", " ").capitalize()
                if not value:
                    continue
                lines.append(f"| **{label}** | {str(value).replace('|', chr(92) + '|')} |")
            if action.get("capped_by_partial_brief"):
                lines.append("| **Confidence note** | Capped below High: the underlying "
                             "investigation is partial |")
            lines.append("")

    if actions["abstentions"]:
        lines += ["### Where no action is recommended", ""]
        if persona.key == "executive":
            lines.append("| Open question | What it would take |")
            lines.append("|---|---|")
            for item in actions["abstentions"]:
                topic = str(item.get("topic", "")).replace("|", "\\|")
                need = str(item.get("what_would_be_needed", "")).replace("|", "\\|")
                lines.append(f"| {topic} | {need} |")
        else:
            lines.append("| Topic | Why no action | What would be needed |")
            lines.append("|---|---|---|")
            for item in actions["abstentions"]:
                cells = [str(item.get(k, "")).replace("|", "\\|")
                         for k in ("topic", "why_no_action", "what_would_be_needed")]
                lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    return "\n".join(lines)
