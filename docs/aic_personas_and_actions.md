# Persona-specific narratives and action recommendations

Two capabilities that share one guarantee: **the investigation happens once, and everything
downstream is presentation.** A Revenue Ops analyst and a VP read different documents built from
the same evidence, the same diagnosis and the same action record — so they can never be told
different facts about the same incident.

Both examples below are produced by the pipeline, not written by hand. Regenerate them with:

```
python -m agent.render_brief --anomaly-key DET-0023 --anomaly-key DET-0018 --subdir eval
```

That writes three files per incident into `docs/sample_briefs/personas/`:

| Incident | Analyst render | Executive render | Structured actions |
|---|---|---|---|
| DET-0023 — Electronics/Marketplace/West stockout | `DET-0023-analyst.md` | `DET-0023-executive.md` | `DET-0023-actions.json` |
| DET-0018 — Home & Garden/Web/West, no cause found | `DET-0018-analyst.md` | `DET-0018-executive.md` | `DET-0018-actions.json` |

---

## One investigation, many audiences

The naive way to serve two audiences is two pipelines. That fails for a reason that has nothing
to do with cost: two investigations produce two sets of numbers, and the first time a VP quotes a
figure the analyst cannot reproduce, the system stops being trusted. So the split is placed after
the evidence, not before it:

```
investigate once  ->  brief + evidence trail          (agent/investigator.py, 8 tool calls, audited)
                        |
                        +-- extract action chain ONCE (agent/actions.py, 1 call, forced tool)
                        |
                        +-- render per persona        (agent/render_brief.py, 1 call each)
```

`agent/personas.py` holds the personas as **data**, not code paths — audience, prose style, which
action fields survive the render, whether the evidence trail is shown. Adding a third audience is
a new `Persona` object; it cannot introduce a new code path that reaches the warehouse, because
the render stage has no warehouse access to reach with.

**What the two renders actually differ on:**

| | Revenue Ops Analyst | VP / Business Leader |
|---|---|---|
| Opens with | the mechanism — which cell moved, on which dates | the dollar figure and whether it is contained |
| Statistics | z-scores, expected-vs-actual, source table names | none |
| Action fields shown | all 9, including monitoring plan and evidence quote | 4 — action, impact, owner, confidence |
| Evidence trail | full query list | omitted |
| Abstentions | topic, why, what would be needed | open question, what it would take |

**The action content is identical by construction, not by agreement.** Actions are extracted once
into JSON and rendered from that JSON by ordinary code. The analyst's dollar figure and the VP's
are the same string in the same file. Verified mechanically on the shipped examples: of the action
fields both personas render, **4 of 4 are byte-identical** on DET-0023.

Only the narrative prose is model-generated per persona, and it is generated from the brief text
alone with one rule that overrides the style guidance: *no fact, figure, date or cause that is not
already in the brief.*

---

## The action chain

The structure the objective names, with one addition:

```
driver -> controllable lever -> action -> expected impact -> owner -> confidence -> monitoring plan
```

The addition is **`expected_impact_basis`** — which figure in the brief the impact was derived
from, and how. It exists because "expected impact" is exactly the field a language model will
happily invent a number for. Requiring the derivation to be stated means an unfounded impact has
nowhere to hide: if the basis would be empty, the action is dropped instead.

Three constraints decide whether an action may exist at all:

1. **Grounded.** The driver must be established by a figure written in the brief, quoted verbatim
   in `driver_evidence`. On DET-0023 that quote is `5.33 %` — and `5.33` and `7,190` both appear
   in the source brief, checked rather than assumed.
2. **Controllable.** A real, well-evidenced driver with no lever anyone owns produces no action.
   `lever_status` carries `controllable` / `not_controllable` / `unknown` for exactly this: a
   Christmas trough is not a failure to fix, and the correct response is to model it, not to
   assign it to someone. `OWNERS` is a closed enum ending in `No owner - not actionable`, so the
   model cannot invent a department to make an action look assignable.
3. **Bounded by the evidence's own confidence.** An action can never be more confident than the
   brief it rests on. This is enforced in code, not asked for in the prompt — `cap_confidence()`
   demotes any High-confidence action whose source brief is incomplete, and stamps the render
   with why.

### Incompleteness has two forms, and only one of them is declared

A brief marked `complete` can still stop mid-sentence: `complete` means the investigation never
hit the tool-call ceiling, not that the write-up finished. Both DET-0023 and DET-0018 have bodies
that end mid-table because the closing turn ran out of output tokens.

So the parser checks both, and either one caps confidence:

| Brief | Declared status | Body ends mid-sentence | Treated as partial |
|---|---|---|---|
| DET-0023 | PARTIAL (tool-call ceiling) | yes | yes |
| DET-0018 | complete | **yes** | **yes** |

Trusting the declared status alone would have let DET-0018 support a High-confidence action off a
truncated evidence section.

---

## Abstention is a first-class output

The two shipped examples were chosen because they land on opposite sides of this line, on real
evidence rather than a constructed demonstration.

**DET-0023 — stockout, action recommended.** Stockout rate more than doubled (2.80% → 5.33%)
across the same window revenue fell $7,190, while marketing spend stayed flat ($108.35 → $107.77
per day). Driver `inventory`, lever `safety stock floor`, owner Supply Chain / Inventory Planning,
confidence **Medium** — capped from the model's own reading because the source brief is partial.
Monitoring: daily stockout rate, threshold ≤3%, daily for 14 days.

**DET-0018 — no cause identified, no action recommended.** The investigation ruled out all four
tracked causes: spend *rose* ($129.56 → $186.17), stockout rate was 0%, margin held near 39%, and
the date carried no holiday or retail event. The correct output is nothing:

> **No action is recommended from this investigation.**
>
> This is a deliberate abstention, not an omission: the evidence gathered does not support a
> specific intervention, and an invented one would send someone to spend money against a cause
> that was never established.

`stance: "abstain"`, `recommended_actions: []`, and five abstentions each naming the specific
evidence that would unlock an action. The VP render says the same thing in its own register —
"No immediate P&L action is required" — without manufacturing a reassuring cause.

**Both incidents produced 5 abstentions.** A confident diagnosis does not empty that section: the
stockout was diagnosed and still left the *reason for the stockout* unanswered, because supplier
lead times are not in the six tables the agent can read.

---

## What this stage costs, and what it cannot do

**It runs no queries.** `agent/render_brief.py` and `agent/actions.py` import no engine, no tool
and no SQL — the only thing taken from the guardrail package is `REPO_ROOT`, a path constant. The
audit log confirms it from the other direction: rendering both incidents added **zero** rows to
`audit.agent_tool_calls`. The guardrail chain is not re-implemented here because there is nothing
to guard; the evidence was already gathered, under the cap, through the validator, on the
read-only role.

Cost is 3 model calls per incident — one action extraction, one narrative per persona — against
Groq's 8,000 tokens/minute free tier, which the pacer spaces automatically.

**Two honest limitations:**

1. **The narrative can drift in framing from the action table.** The narrative is rendered without
   sight of the extracted actions, deliberately — that is what stops it contradicting the figures.
   The cost shows on DET-0023, where the VP narrative says ownership "has not yet been assigned"
   while the action table assigns it to Supply Chain / Inventory Planning. The facts agree; the
   framing does not. Passing the action record into the narrative call would fix the framing at
   the price of letting the narrative re-argue the numbers.
2. **The narratives inherit their source's truncation.** The analyst render of DET-0023 ends
   mid-table for the same output-token reason the source brief does. The action chain is unaffected
   — it is extracted as structured fields, which the model completes before the prose budget runs
   out — but the prose is visibly cut.
