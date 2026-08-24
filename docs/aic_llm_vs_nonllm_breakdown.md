# LLM vs Non-LLM: What Computes What

**The LLM is never the source of quantitative truth.** Every number in every brief is computed
by SQL or by statistics. The model reads results, forms a hypothesis, and writes prose — it does
not calculate, and it cannot reach data the guardrails have not allowed.

---

## The 30-second version

| Stage | Method | LLM involved? |
|---|---|---|
| **Ingest** | Python + pandas → Postgres | ❌ No |
| **Transform** | dbt / SQL — 13 models, 191 tests | ❌ No |
| **KPI calculation** | SQL in the marts (revenue, margin, stockout) | ❌ No |
| **Anomaly detection** | Rolling z-score, MAD, t-test, Benjamini–Hochberg FDR | ❌ No |
| **Prioritisation** | `peak_z_score`, `min_q_value`, `total_revenue_delta_usd` | ❌ No |
| **Query validation** | `sqlglot` AST parse against an allowlist | ❌ No |
| **Access control** | Postgres roles + grants | ❌ No |
| **Audit logging** | Append-only table + trigger | ❌ No |
| **Which evidence to fetch next** | LLM chooses from one allowlisted tool | ✅ **Yes** |
| **Root-cause hypothesis** | LLM reasons over returned rows | ✅ **Yes** |
| **Narrative brief** | LLM writes prose | ✅ **Yes** |

**Eight of eleven stages are deterministic.** The LLM occupies exactly one band: *interpretation
and explanation*, downstream of every number it cites.

---

## The boundary is mechanically enforced, not a convention

Three checks anyone can run:

```bash
# 1. No LLM anywhere in the pipeline, detection, or reporting layers
grep -rl "groq\|anthropic\|openai\|LLMProvider" detection/ generators/ dbt/ dashboards/ dags/
#    → no matches

# 2. No vendor SDK outside the adapter directory
grep -rl "import groq\|import anthropic\|import openai" agent/
#    → agent/llm/ only (3 files)

# 3. The statistics are real libraries, not prompts
grep "^import\|^from" detection/detector.py
#    → numpy, pandas, scipy.stats
```

`agent/llm/` defines one neutral `LLMProvider.chat()` contract. The investigation loop, the
grader and the eval talk only to that interface. Swapping providers is a `.env` change.

---

## What the deterministic half actually computes

**Detection — pure statistics, five stages, no model:**

1. **Baseline** — median of the same weekday over 8 trailing weeks, skipping the most recent
   week so an event cannot enter its own baseline.
2. **Common-factor removal** — subtract the cross-sectional median across all 60 cells. A move
   the whole business made together is the calendar, not an incident.
3. **Robust scale** — MAD over 56 days of recent *forecast errors* (× 1.4826), not baseline spread.
4. **Empirical null calibration** — Efron, measured factor 1.047.
5. **Confirmation** — pool over 1/2/3-day windows, Bonferroni ×3, **t-distribution** p-values,
   then **Benjamini–Hochberg FDR at q < 0.01** across 38,460 tests.

Result: all 3 injected anomalies detected (0/1/4-day lag), false-positive rate **0.09%**,
holiday decoys **zero flags**. Reproducible at `SEED = 42` — the same input always gives the
same episodes, which is not a property an LLM can offer.

**Materiality is a statistical decision, not a model opinion.** A movement is reported only if
it survives FDR correction. No prompt participates in that judgement.

---

## What the LLM is allowed to do — and how it is bounded

The agent has **exactly one tool**, `query_warehouse`. Every call passes through the same
pipeline in order:

```
budget check → sqlglot validation → read-only execution → audit write
```

| Bound | Value | Enforced by |
|---|---|---|
| Tool calls per investigation | **8** (measured, not estimated) | `CallBudget` — raises, so an ignoring caller cannot loop past it |
| Rows per query | **1,000** | `LIMIT` injected or clamped by the validator |
| Readable tables | **6**, in `analytics` only | Table allowlist + Postgres grants |
| SQL functions | ~70 allowlisted | AST node inspection |
| Statement types | `SELECT` only, single statement | AST parse, not string matching |
| Query timeout | 30s | Session `statement_timeout` |

**The model cannot escalate.** Even if it emits hostile SQL, the validator rejects it; even if the
validator were removed, the `revenue_agent` role cannot write, drop, or reach `raw`. Both halves
were tested independently — 83 attack attempts, 62 blocked, **0 unexpected outcomes**.

**Every attempt is recorded before it is trusted.** The audit log captures inputs, generated SQL,
tables referenced, row count, duration and outcome — *including refusals* — in an append-only
table enforced by grant and trigger.

---

## Why this split, and not more LLM

| Concern | Deterministic answer |
|---|---|
| **Reproducibility** | Same seed → same 44 episodes, every run. A model sampled twice may not agree with itself |
| **Auditability** | A z-score has a formula a statistician can check. "The model thought it looked unusual" does not |
| **Cost** | Detection over 43,860 rows costs zero tokens |
| **Correctness under scale** | 38,460 hypothesis tests need FDR control, not judgement |
| **Trust** | If the model is wrong about a *cause*, the brief is wrong. If it were wrong about a *number*, the whole system would be untrustworthy |

The honest converse: **root-cause attribution is genuinely hard to specify as a rule.** Deciding
that a revenue drop with flat marketing spend and a simultaneous stockout is an *inventory*
problem — and saying so in a sentence a Revenue Ops lead can act on — is where a language model
earns its place. That is the one job it is given.

---

## Cost and latency of the LLM portion

Measured on Groq `openai/gpt-oss-120b` (free tier):

| Metric | Value |
|---|---|
| Tool calls per investigation | ≤ 8 (hard ceiling) |
| Tokens per investigation | ~40,000 (every call re-sends the conversation → quadratic in calls) |
| Cost at 20-call ceiling | ~152,000 tokens — **rejected on measurement, not price** |

The ceiling was lowered from 20 to 8 because at 20 the agent spent every call, re-queried tables
it had already read, and produced **no brief at all** — past ~8 results the oldest are elided to
fit the context window, so the marginal call destroys more evidence than it adds. Cost fell as a
*consequence* of fixing a reasoning defect, not as the goal.

---

**Detail:** detection method in `docs/detection_and_prioritisation.md` · guardrails and the full
attack transcript in `docs/security_guardrails.md` · KPI definitions in
`docs/aic_kpi_semantic_contract.md`.
