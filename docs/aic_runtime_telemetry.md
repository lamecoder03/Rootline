# Runtime telemetry

Compiled from data this project already recorded — `docs/sample_briefs/eval_results.json`,
`audit.agent_tool_calls`, and the persona render records. **No new instrumentation was added and
no runs were repeated to produce it.** Every figure below is measured; the two derived figures are
labelled as estimates and show their arithmetic.

Population: the **5 scored investigations** completed so far (of 10 eval scenarios), plus the
**2 persona renders**. Small sample, stated rather than smoothed over.

---

## Per investigation

| Metric | Mean | Range |
|---|---|---|
| Wall-clock latency | **533.6 s** | 485.7 – 545.7 s |
| Tool calls (warehouse queries) | **7.8** | 7 – 8 |
| Model calls | **~9** | 8 tool-calling turns + 1 closing turn |
| Input tokens | **42,906** | 38,547 – 47,296 |
| Output tokens | **4,157** | 3,079 – 4,787 |
| Total tokens | **47,063** | 43,176 – 51,040 |

| Key | Calls | Latency | Input | Output | Stop reason |
|---|---|---|---|---|---|
| DET-0008 | 8 | 545.5 s | 45,290 | 3,079 | `budget_exceeded` |
| DET-0023 | 8 | 545.6 s | 43,567 | 4,529 | `budget_exceeded` |
| DET-0029 | 8 | 545.5 s | 47,296 | 3,744 | `budget_exceeded` |
| DET-0005 | 8 | 545.7 s | 39,829 | 4,647 | `budget_exceeded` |
| DET-0018 | 7 | 485.7 s | 38,547 | 4,787 | `length_retry` |
| **Total** | **39** | **2,668 s** | **214,529** | **20,786** | |

---

## The latency is rate-limit-bound, not work-bound

The four 8-call investigations took **545.5, 545.6, 545.5 and 545.7 seconds**. Four different
incidents, different queries, token counts spread over 7,467 — and they finish within **0.2 s of
each other**. Runtime that insensitive to the work being done is not measuring the work.

The confirmation is DET-0018: 7 calls instead of 8, and **485.7 s — 59.9 s faster**. One fewer
model call costs almost exactly one minute, which is the free tier's rate-limit window.

**Latency here is a queue, not a computation.** `agent/llm/pacing.py` waits for quota *before*
spending it, because Groq's free tier meters 8,000 tokens/minute and bills prompt + `max_tokens`
up front. So each model call reserves roughly a minute of budget, and ~9 calls costs ~9 minutes.

*Estimate, arithmetic shown:* mean billed tokens ≈ 42,906 input + (1,200 `max_tokens` × ~9
requests) ≈ 53,700. At 8,000 tokens/minute that is **≈ 403 s of mandatory waiting**, or **~76 %**
of the observed 533.6 s. The request count is inferred from the tool-call count, so treat this as
an estimate; the 0.2 s clustering above is the direct evidence.

**The warehouse is not the bottleneck by three orders of magnitude.** From
`audit.agent_tool_calls`, across the 39 tool calls in these 5 investigations:

| Database metric | Value |
|---|---|
| Total query time | **1,095 ms** |
| Mean per query | **28 ms** (median 19 ms, max 260 ms) |
| Share of the 2,668 s total runtime | **0.041 %** |

Every one of the 122 recorded investigation tool calls is in the audit log: 119 `pass`, 2 `error`,
1 `reject`.

---

## Persona rendering

3 model calls per incident — one action extraction, two narratives — with **zero** warehouse
queries.

| Incident | Input | Output | Warehouse queries |
|---|---|---|---|
| DET-0023 | 4,155 | 3,100 | 0 |
| DET-0018 | 4,176 | 2,461 | 0 |
| **Mean** | **4,165** | **2,780** | **0** |

Rendering both audiences costs **~6,950 tokens**, about **15 %** of one investigation. Serving a
second persona is cheap precisely because it re-reads a finished brief instead of re-investigating.

---

## Cost

**Actual spend to date: $0.** Everything runs on Groq's free tier, on `openai/gpt-oss-120b`. That
is the honest headline, and it is also why the interesting cost metric is quota, not dollars.

**The binding constraint is the 200,000 tokens/day budget**, against ~47,000 tokens per
investigation:

| | Value |
|---|---|
| Investigations per day within quota | **~4** |
| Episodes produced by one detection run | **44** |
| Days to investigate one run's backlog | **~11** |

That gap is the real operational limitation of the free tier, and no amount of prompt tuning
closes it — the fix is a paid tier or a smaller triage set. It is also why the eval runs
scenario-by-scenario with incremental writes rather than as one batch.

**Dollar cost, if this moved to a paid endpoint.** Illustrative only — substitute the current
published rate for whichever provider is in use rather than trusting a figure written here:

| Blended rate | Per investigation (47k tokens) | Per persona render (7k) | All 44 episodes |
|---|---|---|---|
| $0.10 / M | $0.005 | $0.001 | $0.21 |
| $0.50 / M | $0.024 | $0.003 | $1.04 |
| $1.00 / M | $0.047 | $0.007 | $2.07 |

The shape is what matters: at any plausible rate, **investigating every anomaly a full detection
run produces costs single-digit dollars.** The cost that dominates this project is not inference —
it is the free tier's throughput ceiling, and the analyst hours the briefs replace.

---

## Caveats

- **n = 5.** Half the eval scenarios have not run. The 545 s clustering is consistent, but four
  points is four points.
- **Grader calls are not counted** in the per-investigation figures. Each scored scenario also
  costs one forced-tool extraction call, which `eval_results.json` does not record separately.
- **Token counts are the provider's**, taken from response usage fields, not estimated locally.
  `agent/context_budget.py`'s 3.3 chars/token figure is a *sizing* heuristic used before a request
  is sent, and is deliberately pessimistic; it is not the source of any number here.
- **Latency is specific to the free tier.** On a paid tier with a higher rate limit, the ~76 %
  pacing component largely disappears and these investigations would run in roughly the time the
  model actually takes to think — a different system's telemetry, not a faster version of this one.
