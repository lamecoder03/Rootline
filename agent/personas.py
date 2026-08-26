# Who the brief is being written for, as data rather than as code paths.
# Exists because the same investigation has two audiences with opposite needs - an analyst wants
# the query that proves it, a VP wants the number and the decision - and forking the pipeline to
# serve both would let their facts drift apart. One investigation, one action set, two renderings.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Persona:
    """One audience. Everything here is presentation: which sections the narrative carries, which
    action fields survive the render, and whether the evidence trail is shown. Nothing in this
    object can change what was found - only how much of it is put on the page."""

    key: str
    title: str
    audience: str
    # Fields of a RecommendedAction this persona sees. The action content is identical for every
    # persona; personas differ only in how many of its columns are rendered.
    action_fields: tuple
    show_evidence_trail: bool
    show_monitoring_sql: bool
    max_narrative_tokens: int
    style: str


ANALYST = Persona(
    key="analyst",
    title="Revenue Ops Analyst",
    audience=(
        "A revenue operations analyst who will re-run these queries themselves and needs to know "
        "exactly what was checked, what it returned, and what to check next."
    ),
    action_fields=("driver", "driver_evidence", "controllable_lever", "action", "expected_impact",
                   "expected_impact_basis", "owner", "confidence", "monitoring_plan"),
    show_evidence_trail=True,
    show_monitoring_sql=True,
    max_narrative_tokens=1150,
    style="""\
Write for a revenue operations analyst. They are technical, they have warehouse access, and they \
will check your work.

- Lead with the mechanism, not the headline. What moved, in which cell, on which dates.
- Every claim carries its figure and its date. Name the table the figure came from.
- State what was ruled out and the number that ruled it out. An eliminated hypothesis is a result.
- Be explicit about lead and lag: if a cause moved before the effect, give both dates.
- End with concrete next checks - the specific query or comparison you would run next, and what \
each one would settle. Not "investigate further".
- Do not soften uncertainty. If the evidence is partial, say which part is missing.""",
)


EXECUTIVE = Persona(
    key="executive",
    title="VP / Business Leader",
    audience=(
        "A VP who has ninety seconds, owns the P&L, and needs to know the size of the problem, "
        "what is being done, and who owns it."
    ),
    action_fields=("action", "expected_impact", "owner", "confidence"),
    show_evidence_trail=False,
    show_monitoring_sql=False,
    max_narrative_tokens=900,
    style="""\
Write for a VP who owns the P&L and has ninety seconds.

- Bottom line first. The opening sentence states the dollar impact and whether it is contained.
- Frame everything as business impact, not statistics. No z-scores, no p-values, no table names, \
no SQL, no column names.
- One short paragraph of context, maximum. Then the decision.
- Money in dollars, movement in plain percentages, dates as plain dates.
- Say plainly whether this needs a decision now or is being handled.
- If the cause is not known, say that in one sentence and say what it would take to find out. \
Do not manufacture a reassuring narrative.
- No hedging clauses stacked on each other. One confidence statement, once.""",
)


PERSONAS = {p.key: p for p in (ANALYST, EXECUTIVE)}

DEFAULT_PERSONAS = ("analyst", "executive")
