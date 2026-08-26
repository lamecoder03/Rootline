# KPI Semantic Contract

The governed definition of every KPI this engine reports on: what it means, how it is
calculated, what can move it, when a movement counts as material, where the number came from,
and who is allowed to read it.

**This contract is compiled from what is already enforced in code**, not written alongside it.
Every calculation below is the actual SQL in `dbt/revenue_anomaly/models/marts/`; every
threshold is the live constant in `detection/config.py`. Where the two could drift, the code is
authoritative and this document is wrong.

---

## 1. KPI definitions and calculations

Three connected KPIs, all at **date × category × channel × region** unless stated otherwise.

### Revenue — `analytics.fct_daily_revenue.gross_revenue`

| Property | Value |
|---|---|
| Type | Currency, `numeric(14,2)` — never float |
| Grain | date × category × channel × region (60 cells/day) |
| Calculation | Passed through from `stg_daily_revenue`; **no transformation** |
| Coverage | 731 days × 60 established cells = 43,860 rows, **+21** for the newly launched cell = **43,881**. Total $101,750,105.26 |
| Reconciliation | Row count and revenue total identical to staging, tested |

The 60-cell / 43,860-row figures quoted elsewhere in this repo describe the established series;
the sparse-history scenario adds a 61st cell with 21 days
(`docs/aic_sparse_history_scenario.md`). Remove it with
`python -m generators.gen_sparse_category --remove` to return to 43,860.

The primary KPI. It is deliberately *not* derived — anything computed from it (margin, ROAS)
is a separate, differently-named column so an estimate can never be mistaken for a measurement.

**Two supporting measures on the same row**, both used as drivers rather than reported KPIs:

- `marketing_spend_usd` — spend allocated to region (see §2, allocation is an assumption)
- `return_on_ad_spend` — `gross_revenue / marketing_spend_usd`, NULL when spend is 0

### Estimated gross margin — `analytics.fct_daily_margin`

| Property | Value |
|---|---|
| Type | Currency + percentage, both nullable |
| Grain | Same as revenue |
| Calculation | `estimated_cogs = units × avg_unit_cost` (category average)<br>`estimated_gross_margin = gross_revenue − estimated_cogs`<br>`estimated_gross_margin_pct = 100 × margin / gross_revenue` |
| Cost basis | `avg(unit_cost) FILTER (WHERE is_margin_calculable)` at category grain |

**The `estimated_` prefix is part of the contract, not decoration.** Revenue is per category;
cost is per SKU. Units are costed at the category average over costed SKUs only, so this is an
approximation and its name says so.

**Known incompleteness, disclosed on every row:** 8 of 120 SKUs have no `unit_cost`. They are
**excluded from the cost basis and never imputed** — an imputed cost becomes a fabricated number
inside a financial metric nobody questions again. Every margin row carries
`skus_excluded_from_cost_basis`, `cost_basis_coverage_pct` (88.9% Beauty → 96.9% Electronics)
and `excluded_sku_ids`.

> **Trap, measured on the live warehouse:** `is_margin_estimable` is `TRUE` on all 43,860
> established rows (and `FALSE` only on the 21 newly launched ones), while
> `cost_basis_is_complete` is `FALSE` on every row. For the established series the flag
> therefore **excludes nothing** — filtering on it looks careful and gives false comfort. The
> correct treatment is **disclosure beside the number** (`cost_basis_coverage_pct`), not a
> filter. The flag earns its keep only for a category with no cost basis at all.

### Stockout rate — `analytics.fct_daily_stockout.stockout_rate_pct`

| Property | Value |
|---|---|
| Type | Percentage, `numeric(5,1)` |
| Grain | **date × category × region — no channel** |
| Calculation | `100.0 × count(*) FILTER (WHERE units_on_hand = 0) / count(*)` over SKUs |
| Coverage | 14,620 rows |

**The missing channel grain is a deliberate modelling decision, not an omission.** Stock is
physical; it is not held per sales channel. Any consumer applying a channel filter to this KPI
is filtering nothing — see §5.

Companion columns: `skus_out_of_stock`, `skus_tracked`, `skus_below_reorder_point`,
`has_stockout`, `is_total_stockout`, and `stocked_out_sku_ids` (the actual SKU list, because
"6 of 8 West Electronics SKUs were out, including ELEC-0004" is actionable where "75%" is not).

---

## 2. Known drivers per KPI

What the engine is permitted to consider as an explanation. Anything outside this list is not
in the warehouse and must be reported as *unexplained*, never inferred.

| KPI | Driver | Where it lives | Caveat the engine must respect |
|---|---|---|---|
| Revenue | Marketing spend | `fct_daily_revenue.marketing_spend_usd` | **Region-allocated, not measured** — see below |
| Revenue | Inventory / stockout | `fct_daily_stockout` | No channel grain |
| Revenue | Calendar / holiday | `is_holiday`, `is_retail_event`, `retail_significance` | 30 holidays incl. Black Friday / Cyber Monday |
| Revenue | Day-of-week seasonality | `day_of_week`, `is_weekend` | Removed by the detector's baseline, not a cause |
| Revenue | Price vs volume | `units` alongside `gross_revenue` | Separates a discount-driven lift from real demand |
| Margin | Unit cost | `cost_basis_avg_unit_cost` | Category average over costed SKUs only |
| Margin | Revenue mix | Inherited from revenue | Margin % moves when mix moves, with no cost change |
| Stockout | Units on hand | `total_units_on_hand`, `reorder_point` | Physical, category × region only |

**Marketing spend carries the contract's most important assumption.** Source spend has **no
region** — it arrives at date × channel × category, one grain coarser than revenue. Each region
receives its share by **trailing 28-day revenue share, excluding the current day**, recorded per
row in `spend_allocation_basis` and `spend_region_share`.

Same-day allocation was tested and **rejected**: when West Electronics revenue collapses during a
stockout, same-day allocation drags allocated spend down with it and *manufactures* a marketing
cause for an inventory problem. Measured, it would have shown a 72% spend collapse on 12 June
that never happened. Every cell reconciles to the cent ($9,779,277.61 in / out) by largest-
remainder rounding. Day 1 has no history and falls back to an even split, flagged
`even_split_no_history`.

---

## 3. Detection thresholds — when a movement is material

Materiality is **statistical significance**, computed per cell, never on a total. A cell-level
event vanishes in an aggregate: the stockout scenario touches 3 of 60 cells.

All constants live in `detection/config.py`. All arithmetic is in log space — the generating
process is multiplicative and its noise lognormal.

| Stage | Constant | Value | Why this number |
|---|---|---|---|
| 1. Baseline | `BASELINE_WEEKS` | 8 | Same-weekday median; weekday factor swings 0.92–1.15 |
| 1. Baseline | `BASELINE_GAP_WEEKS` | 1 | Nearest reference is 14 days back, so a 14-day event cannot enter its own baseline |
| 1. Baseline | `MIN_BASELINE_OBSERVATIONS` | 6 | **Below this the day is not scored** — see §6 |
| 2. Common factor | `MIN_CELLS_FOR_COMMON_FACTOR` | 30 | Cross-sectional median; a shared move is the calendar, not an incident |
| 2. Common factor | `MIN_CELLS_FOR_CATEGORY_FACTOR` | 8 | On holidays the peer group narrows to the cell's own category |
| 3. Dispersion | `RESIDUAL_WINDOW_DAYS` | 56 | Scale from recent **forecast errors**, not baseline spread |
| 3. Dispersion | `MIN_RESIDUAL_OBSERVATIONS` | 30 | **Below this the day is not scored** — see §6 |
| 4. Control limit | `Z_THRESHOLD` | **3.0** | Conventional control-chart limit, on the calibrated null |
| 4. Control limit | `HOLIDAY_THRESHOLD_MULTIPLIER` | **1.6** (→ \|z\| ≥ 4.8) | Holiday \|z\| p95 is 3.45 vs 2.11 ordinary |
| 5. Confirmation | `CONFIRMATION_WINDOWS` | (1, 2, 3) | Pooled; sustained shift gains √k, a spike does not. Bonferroni ×3 |
| 5. Confirmation | `FDR_Q` | **0.01** | Benjamini–Hochberg across all 38,460 tests |

**A threshold crossing is a candidate, not a finding.** Nothing is reported until it survives
window pooling, Bonferroni correction, t-distribution p-values (with ~8 baseline observations
the normal approximation understates the tails sevenfold) and BH-FDR at q < 0.01.

**Holidays are expected-variance days, not skipped days** — excluded from baselines, still
scored, bar raised. Before the category-narrowing fix Christmas produced 30 false flags at peak
|z| 17.99; after it, 1 at 5.08.

**Validated performance:** all 3 injected anomalies detected at 0 / 1 / 4-day lag. False-positive
rate outside injected windows **0.09%**; episode precision 70.5%. Decoys (Black Friday, Cyber
Monday, Christmas Eve) fired **zero**.

### What the detector emits — and what it does *not*

`analytics.detected_anomalies` (44 episodes) carries: `anomaly_key`, the cell, `start_date`,
`end_date`, `day_count`, `direction`, `peak_date`, `peak_z_score`, `peak_delta_pct`,
`total_revenue_delta_usd`, `min_q_value`.

**There is no `severity` column, deliberately.** z measures distance from normal, not money. Any
severity banding is a **presentation-layer** decision, defined by whatever consumes the table —
never in the warehouse, so that two consumers cannot disagree about what the warehouse "said".

The banding this project uses is anchored to the detector's own control limits (|z| ≥ 3 ordinary,
≥ 4.8 holiday), and its measured distribution across the 44 episodes is the reason it must never
be the only lens:

| Band | Threshold | Episodes | Absolute revenue delta |
|---|---|---|---|
| 1 — Critical | \|z\| ≥ 8 | 8 | $83,719 |
| 2 — High | 4.8 ≤ \|z\| < 8 | 16 | $85,189 |
| 3 — Moderate | 3 ≤ \|z\| < 4.8 | 17 | $31,669 |
| 4 — Low | \|z\| < 3 | 3 | $1,003 |

**Critical and High carry near-identical dollar impact — $83.7k against $85.2k — despite a
twofold difference in z.** A tiny cell can post a huge z on a trivial dollar move, so any ranking
built on z alone must be shown beside `total_revenue_delta_usd`, not instead of it. Statistical
confidence (`min_q_value`) is a third, separate axis.

---

## 4. Lineage

```
raw (5 tables, 142,733 rows)          Source landing. Deliberately messy.
  ├─ daily_revenue        43,860      Blind — carries anomalies, no flags
  ├─ marketing_spend      10,965      Coarser grain: no region
  ├─ product_master          158      2 source systems, 24 category spellings, nulls
  ├─ inventory_snapshot   87,720
  └─ holiday_calendar         30
        │  dbt · 7 staging models · 81 tests
        ▼
staging                                Conform grain, dedupe, standardise. Views.
  ├─ stg_daily_revenue    43,860      md5 revenue_key declares the grain
  ├─ stg_product_master      120      158 rows merged field-by-field
  ├─ stg_product_master_dedup_audit   158 — which field each losing row donated
  └─ …
        │  dbt · 2 intermediate models
        ▼
intermediate                           Assumptions isolated and tested. NOT consumer-readable.
  ├─ int_marketing_spend_allocated    The allocation rule
  └─ int_category_cost_basis          Rolled to category grain so the join cannot fan out
        │  dbt · 4 marts · 191 tests total
        ▼
analytics                              The ONLY consumer-readable schema.
  ├─ fct_daily_revenue    43,881      43,860 established + 21 newly launched
  ├─ fct_daily_margin     43,881      same grain; margin NULL for the uncosted new cell
  ├─ fct_daily_stockout   14,620
  ├─ dim_product             120
  ├─ detected_anomalies       44      ← detector output
  ├─ detected_anomaly_points 166
  └─ detection_coverage       61      ← per-cell confidence: 60 normal, 1 none
```

**The schema split is the security boundary, not a naming convention.**
`macros/generate_schema_name.sql` overrides dbt's default prefixing so these names are used
verbatim — which is what makes "the agent can read `analytics` and nothing else" enforceable.

Full per-layer reasoning: `docs/data_reconciliation.md` (staging),
`docs/marts_and_allocation.md` (marts), `docs/detection_and_prioritisation.md` (detection).

**Three integrity tests carry most of the weight** (191 total, all passing):
`assert_spend_allocation_reconciles` (no money created or destroyed),
`assert_marts_do_not_inflate_revenue` (a SKU-grain dimension must not fan a category-grain fact
out 24×), `assert_margin_exclusions_are_accounted`.

---

## 5. Access restrictions

Three roles, one database. Enforced by Postgres grants — not by application code, and not by
convention. Full evidence in `docs/aic_rbac_scenario.md`.

| Role | raw | staging | intermediate | analytics | audit |
|---|---|---|---|---|---|
| `revenue_ops` (owner: dbt, loader, detector) | ALL | ALL | ALL | ALL | ALL |
| `revenue_agent` (the LLM investigator) | ✗ | ✗ | ✗ | **SELECT** | **INSERT only** |
| `revenue_reporting` (BI client) | ✗ | ✗ | ✗ | **SELECT** | ✗ |

Verified live: objects outside `analytics` lack schema `USAGE`, so they are **not addressable**,
not merely unreadable.

**Two independent limits, deliberately not the same list.** The Postgres grant is the floor —
both consumer roles hold `SELECT` on all 7 `analytics` objects, and
`ALTER DEFAULT PRIVILEGES` extends that to anything dbt or the detector creates later. The
agent's *query allowlist* (`agent/guardrails/config.py`, 6 tables) is the ceiling, and is
narrower on purpose: a new table becomes readable by the dashboard immediately, but the agent
cannot query it until someone adds it to the allowlist consciously.

`revenue_reporting` is **strictly tighter** than `revenue_agent`: the agent holds `INSERT` on
`audit.agent_tool_calls` because it must record its own tool calls; the dashboard role has no
audit access at all, because read access there would expose every query the agent ever ran to
anyone holding the reporting credentials.

**Column- and domain-level protection.** The `analytics` schema contains no PII: the grain is
category × channel × region, and the only identifiers are SKU codes. Supplier names are the one
sensitive field, and they are **kept NULL rather than imputed** precisely because the agent uses
supplier in narrative — imputing would put a real company's name into a brief a human acts on.

**Every agent query is logged before it is trusted.** `audit.agent_tool_calls` is append-only,
enforced twice (grant + trigger refusing `UPDATE`/`DELETE`/`TRUNCATE` from every role including
the owner), and records inputs, generated SQL, tables referenced, row count, duration, and
validation outcome — **including refusals**.

---

## 6. Confidence and abstention rules

The contract states when the engine must **decline to report** rather than report weakly.

| Condition | Constant | Behaviour |
|---|---|---|
| Fewer than 6 usable baseline days | `MIN_BASELINE_OBSERVATIONS = 6` | Day is not scored |
| Fewer than 30 residual observations | `MIN_RESIDUAL_OBSERVATIONS = 30` | Day is not scored |
| Fewer than 30 peer cells | `MIN_CELLS_FOR_COMMON_FACTOR = 30` | Common factor not removed |
| Scale below `MIN_SCALE` | `1e-6` | Day is not scored (degenerate series) |

A newly launched category has none of the history these rules require. Rather than
fabricating a baseline from too little data, the detector reports **insufficient history with an
explicit reason** — the sparse-history scenario in `docs/aic_sparse_history_scenario.md`.

Two further limits apply to the investigation agent rather than the detector: a hard **8 tool
calls** per investigation (measured, not estimated) and a **1,000-row** cap per query. On
exhaustion the agent must ship a brief marked **partial** rather than silently truncating.

---

## Contract change control

| To change | Edit | Then |
|---|---|---|
| A KPI calculation | `dbt/revenue_anomaly/models/marts/*.sql` | `run_dbt.bat build` — 191 tests must pass |
| A detection threshold | `detection/config.py` | `python -m detection.validate` — re-score vs answer key |
| An access grant | `agent/guardrails/provision.py` / `dashboards/provision_reporting.py` | `python -m tests.attack_attempts` |
| This document | Only after the above | Code is authoritative; this compiles it |
