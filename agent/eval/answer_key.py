# The eval answer key: what a correct brief has to conclude for each scenario.
# Exists as a separate file from the grader so the standard is fixed before any agent output is
# seen - a key edited after reading the briefs is not a key, it is a rationalisation.
# The three ANOM-* entries are transcribed from docs/ground_truth_anomalies.md, not re-derived.

from __future__ import annotations

from dataclasses import dataclass, field

# The cause families a brief can land on. `none_identifiable` is a first-class correct answer,
# not a failure mode: 13 of the detector's 44 episodes correspond to no injected event, and the
# right conclusion for those is that the data does not support a cause.
CAUSE_FAMILIES = (
    "promotion",         # a marketing/promotional surge lifting revenue
    "marketing_spend",   # a spend cut or increase driving revenue down or up
    "inventory",         # stockout / availability
    "pricing",           # discounting or price change, visible in units vs revenue and margin
    "calendar",          # holiday or retail event; a legitimate seasonal move
    "none_identifiable", # real statistical signal, no cause supported by the six tables
)


@dataclass
class Scenario:
    """One eval case. `source` records where the expected answer came from, so the provenance of
    every grade is visible: `ground_truth` entries are transcribed, `operator` entries were
    decided by the project owner from the raw evidence dossier before any brief was generated."""

    anomaly_key: str
    label: str
    source: str                      # "ground_truth" | "operator"
    expected_cause: str
    ground_truth_id: str = None
    # Scenario-specific checks beyond naming the right cause family. These are what separate a
    # brief that got the answer right from one that got it right for the wrong reason.
    must_rule_out: tuple = ()        # cause families the brief must explicitly eliminate
    must_identify_lag: bool = False  # must state the cause preceded the effect
    acceptable_alternatives: tuple = ()   # also-correct families, for genuinely ambiguous cases
    max_confidence: str = None       # brief must not claim more certainty than this
    notes: str = ""
    # The operator's own wording for the `operator` scenarios, kept unedited. The machine-readable
    # fields above are a transcription of this sentence, and this is what they are checkable against.
    operator_verdict: str = ""


# --- Scenarios explained by an injected event -------------------------------------------
# One representative cell per injected anomaly. The full slice is 12 / 3 / 16 detected rows
# respectively; investigating every one would test the same reasoning repeatedly at 16x the cost.
GROUND_TRUTH_SCENARIOS = [
    Scenario(
        anomaly_key="DET-0008",
        label="ANOM-01 · Apparel promotion · Marketplace/West",
        source="ground_truth",
        ground_truth_id="ANOM-01",
        expected_cause="promotion",
        acceptable_alternatives=("marketing_spend", "pricing"),
        must_rule_out=("inventory",),
        must_identify_lag=True,
        notes=(
            "Apparel spend x2.60 from 2025-03-12, two days BEFORE revenue lifts on 03-14. "
            "AOV drops ~20% so units rise faster than revenue - the discount fingerprint. "
            "Promotion, marketing_spend and pricing are all defensible readings of the same "
            "evidence, so all three count as correct; inventory does not."
        ),
    ),
    Scenario(
        anomaly_key="DET-0023",
        label="ANOM-02 · Electronics West stockout · THE NEGATIVE CONTROL",
        source="ground_truth",
        ground_truth_id="ANOM-02",
        expected_cause="inventory",
        must_rule_out=("marketing_spend",),
        notes=(
            "6 Electronics SKUs out of stock in West for exactly the 7 event days. Marketing "
            "spend is deliberately UNTOUCHED across this window. A brief that attributes this "
            "to spend is the specific failure this case exists to catch, and a brief that "
            "concludes inventory without explicitly clearing spend has got it right by luck."
        ),
    ),
    Scenario(
        anomaly_key="DET-0029",
        label="ANOM-03 · Mobile App budget cut · Home & Garden/North",
        source="ground_truth",
        ground_truth_id="ANOM-03",
        expected_cause="marketing_spend",
        must_rule_out=("inventory",),
        must_identify_lag=True,
        notes=(
            "Mobile App spend x0.30 from 2025-09-17; revenue does not break trend until 09-22. "
            "The 5-day lead is the point of the case - a same-day comparison understates the "
            "link. Gradual decay, not a step change."
        ),
    ),
]

# --- Scenarios with no injected event ---------------------------------------------------
# Decided by the project owner from `python -m agent.eval.candidate_evidence`, BEFORE any brief
# existed, and transcribed here verbatim - the `operator_verdict` field on each entry is the
# owner's own wording, unedited, so the grade and the standard it was graded against can be read
# side by side. The author of the agent does not get to invent the standard the agent is
# measured against.
#
# One transcription rule, stated so it is auditable: `must_rule_out` is set ONLY where the owner
# wrote "must rule out" - that check demands the brief explicitly eliminate a family with a
# figure. Where the owner wrote "must not" (must not conflate / must not default to blaming /
# must NOT attribute), that is a constraint on the CONCLUSION, and naming the forbidden family
# already fails the cause check. Promoting those to must_rule_out would grade the agent against
# a stricter key than the one it was given.
OPERATOR_SCENARIOS: list = [
    Scenario(
        anomaly_key="DET-0005",
        label="Beauty · Mobile App · North — Christmas Day",
        source="operator",
        expected_cause="calendar",
        operator_verdict="calendar",
        notes="2024-12-25, peak z -5.08, revenue -167, spend -65.9%, 0/5 SKUs out of stock.",
    ),
    Scenario(
        anomaly_key="DET-0018",
        label="Home & Garden · Web · West — concurrent with the Apparel promotion",
        source="operator",
        expected_cause="none_identifiable",
        operator_verdict=(
            "none_identifiable (must not conflate with the concurrent Apparel promotion in a "
            "different category moving the opposite direction)"
        ),
        notes="2025-03-16, peak z -2.34, revenue -316, spend +20.4%, 0/5 out of stock, 12 peers.",
    ),
    Scenario(
        anomaly_key="DET-0019",
        label="Apparel · Mobile App · West — a real spend-driven decline",
        source="operator",
        expected_cause="marketing_spend",
        operator_verdict=(
            "marketing_spend (spend down 21%, revenue down, directionally consistent, "
            "no stockout - a real, non-trap case, just with a 2-day window)"
        ),
        notes="2025-05-13..14, peak z -5.76, revenue -683, spend -21.0%, 0/7 out of stock.",
    ),
    Scenario(
        anomaly_key="DET-0021",
        label="Electronics · Web · North — THE SPEND TRAP (spend is UP)",
        source="operator",
        expected_cause="none_identifiable",
        operator_verdict=(
            "none_identifiable (must not default to blaming reduced marketing spend - spend is "
            "actually up 26% here, that's the trap)"
        ),
        notes="2025-05-18..19, peak z -4.71, revenue -2,543, spend +26.4%, 0/8 out of stock.",
    ),
    Scenario(
        anomaly_key="DET-0022",
        label="Electronics · Marketplace · West — the neighbouring-stockout trap",
        source="operator",
        expected_cause="marketing_spend",
        operator_verdict=(
            "marketing_spend (spend up 22.7%, revenue up, no stockout in this specific cell - "
            "must NOT attribute this to the neighboring West Electronics stockout that starts "
            "the next day in a different channel)"
        ),
        notes="2025-06-08, peak z +3.83, revenue +586, spend +22.7%, 0/8 out of stock.",
    ),
    Scenario(
        anomaly_key="DET-0026",
        label="Apparel · Web · East — inventory, with spend to be cleared",
        source="operator",
        expected_cause="inventory",
        operator_verdict="inventory (must rule out marketing spend, which moved only slightly)",
        must_rule_out=("marketing_spend",),
        notes="2025-08-04, peak z -4.80, revenue -790, spend +6.4%, 1/7 SKUs out of stock.",
    ),
    Scenario(
        anomaly_key="DET-0028",
        label="Electronics · Web · West — mirror of the spend trap (spend DOWN, revenue UP)",
        source="operator",
        expected_cause="none_identifiable",
        operator_verdict=(
            "none_identifiable (mirror of DET-0021 - spend down 8.4% while revenue spiked up, "
            "wrong direction to be the cause)"
        ),
        notes="2025-09-04, peak z +5.39, revenue +2,446, spend -8.4%, 0/8 out of stock.",
    ),
]


def all_scenarios():
    return GROUND_TRUTH_SCENARIOS + OPERATOR_SCENARIOS


def is_ready():
    """The eval refuses to run until the operator scenarios are keyed, so a partial key cannot
    quietly become a published result."""
    return bool(OPERATOR_SCENARIOS)
