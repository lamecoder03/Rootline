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
# Filled in by the project owner from `python -m agent.eval.candidate_evidence`, BEFORE any
# brief exists. Left empty deliberately: the author of the agent does not get to invent the
# standard the agent is measured against.
OPERATOR_SCENARIOS: list = []


def all_scenarios():
    return GROUND_TRUTH_SCENARIOS + OPERATOR_SCENARIOS


def is_ready():
    """The eval refuses to run until the operator scenarios are keyed, so a partial key cannot
    quietly become a published result."""
    return bool(OPERATOR_SCENARIOS)
