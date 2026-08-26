# Feedback loop

**Scope, stated up front: this is a captured mechanism, not a learning system.** It records what
a human thought of a generated brief, ties that verdict to the investigation that produced it,
and aggregates the result for review. **Nothing in this pipeline reads the feedback table to
alter its own behaviour.** Closing the loop — changing a prompt, moving a threshold, retraining
anything — is a human decision, deliberately left outside a two-day build. What production would
extend is described at the end.

```
python -m agent.feedback --anomaly-key DET-0023 --verdict accurate --persona analyst --by "ops.analyst"
python -m agent.feedback --anomaly-key DET-0008 --verdict inaccurate --aspect cause --note "..."
python -m agent.feedback --summary
```

Implementation: [`agent/feedback.py`](../agent/feedback.py). Storage: `feedback.brief_feedback`
in the same Postgres warehouse, created idempotently on first use.

---

## What gets captured

| Field | Why it exists |
|---|---|
| `anomaly_key` | Ties the verdict to the brief. Without it feedback is an opinion about the product in general |
| `investigation_id` | Ties it to the **audit trail** — the exact queries that produced the claim being disputed |
| `verdict` | `accurate` / `partially_accurate` / `inaccurate` |
| `disputed_aspect` | `cause` / `magnitude` / `action` / `confidence` / `not_an_anomaly` / `other` |
| `correction_note` | Free text: what the human knows that the warehouse did not |
| `persona` | Which render they read — an executive and an analyst can disagree about the same brief |
| `submitted_by` | Who said it |

**Three verdicts, not a five-point scale.** A 1–5 scale produces a lot of 3s, and a 3 is not
actionable. These three force the only question that matters operationally: was this brief safe
to act on?

**A non-`accurate` verdict must name the disputed aspect.** Enforced twice — in Python before the
insert and by a `CHECK` constraint in the table — because "this is wrong" with no aspect cannot be
routed. The three aspects live in three different layers of this pipeline, and knowing which one
was wrong is the difference between tuning the detector and tuning the prompt:

| Disputed aspect | Which layer owns the fix |
|---|---|
| `not_an_anomaly` | Detection — control limits, `detection/config.py` |
| `magnitude` | Marts — the fact table or the allocation rule |
| `cause` | The agent's investigation prompt and its tool-call budget |
| `action` | The action extraction schema in `agent/actions.py` |
| `confidence` | The confidence cap in `agent/render_brief.py` |

## The agent cannot grade its own work

Feedback is written with the **owner** engine, never the agent's. `revenue_agent` has no grant on
the `feedback` schema at all, and this is verified rather than assumed:

```
INSERT INTO feedback.brief_feedback ... as revenue_agent
-> permission denied for schema feedback
```

The table is also provisioned by `agent/feedback.py` rather than by
`agent/guardrails/provision.py`, on purpose: that module exists to provision what the agent *may*
touch, and adding a table there that the agent must never touch would blur exactly the boundary it
is there to hold.

## Captured so far

Three real verdicts, submitted against briefs this project actually produced:

| Brief | Verdict | Aspect | Note |
|---|---|---|---|
| DET-0023 | accurate | — | — |
| DET-0008 | inaccurate | `cause` | Apparel promo ran 03-14 to 03-17; brief abstained because it queried spend only up to 03-13 |
| DET-0018 | partially_accurate | `confidence` | Abstention correct, but High confidence overstates a brief whose body is truncated |

The DET-0008 note is the useful shape: it does not say the brief was stupid, it says *which query
window was wrong*. That routes to the investigation prompt, and it matches what the eval already
found independently — DET-0008 queried spend and stockouts only for dates before the anomaly
window, so it abstained with `none_identifiable` against a real promotion.

## What production would extend

Everything below is **not built** here, and is listed so the gap is explicit rather than implied:

1. **Aspect-routed prompt tuning.** A run of `cause` disputes concentrated on one failure mode —
   as with DET-0008's query windows — is a prompt change, and would be A/B'd against the eval
   harness that already exists rather than applied on a hunch.
2. **Threshold tuning from `not_an_anomaly` verdicts.** Those are labelled false positives. The
   detector's control limits are constants in `detection/config.py`; enough labels would let them
   be fitted per cell instead of set globally.
3. **Feedback as an eval source.** A corrected brief is a new answer-key row. The eval currently
   scores against 10 hand-built scenarios; captured corrections would grow that set from real
   operator disagreement instead of from the author's imagination.
4. **Loop closure requires volume this project does not have.** Three verdicts cannot tune
   anything. The mechanism is built and proven to capture; acting on it needs months of real
   operator use, and pretending otherwise would be the dishonest version of this document.
