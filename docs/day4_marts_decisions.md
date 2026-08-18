# Day 4 — Marts: the allocation rule and the margin gap, and why

Day 3 cleaned the data and deliberately left two decisions unmade, because both are business
judgements rather than cleaning steps. This is where they get made, and this is the reasoning.

The code is in `dbt/revenue_anomaly/models/marts/` and `models/intermediate/`. This document
explains the decisions the code implements and the alternatives that were rejected.

---

## What a mart layer is for, in this project

Staging answers "is this data trustworthy?". Marts answer "what does it mean?".

Concretely: staging may not contain a business assumption. Marts must contain them, clearly
labelled, because a consumer needs to know what they are agreeing to when they read a number.
Both of today's decisions are assumptions someone could reasonably disagree with, so both live
here, both are named in the model header, and both are enforced by tests.

Four objects land in `analytics` — the only schema Power BI, the Day 5 detector, and the Day 8
agent are ever pointed at:

| Model | Grain | Rows | Materialisation |
|---|---|---|---|
| `fct_daily_revenue` | date x category x channel x region | 43,860 | table |
| `fct_daily_margin` | date x category x channel x region | 43,860 | table |
| `fct_daily_stockout` | date x category x region | 14,620 | view |
| `dim_product` | sku_id | 120 | table |

Two supporting models land in a separate `intermediate` schema, so `analytics` contains only
things a consumer should touch:

| Model | Grain | Rows | Purpose |
|---|---|---|---|
| `int_marketing_spend_allocated` | date x channel x category x region | 43,860 | The allocation rule |
| `int_category_cost_basis` | category | 5 | Product dimension rolled up to category grain |

---

## Decision 1 — Marketing spend is allocated on TRAILING revenue share, never same-day

Spend arrives at date x channel x category. Revenue is at date x channel x category x region.
Each spend number has to be split four ways before the two can sit on one row.

### The rule

Each region receives the share of the cell's spend equal to **that region's share of the same
channel-and-category's revenue over the previous 28 days, excluding the current day**.

### The obvious rule, and why it is wrong here

The intuitive choice is to allocate on the *current day's* revenue share: if West produced 20%
of today's Electronics revenue, give it 20% of today's Electronics spend.

That rule quietly destroys the thing this entire project is built to do.

The data contains three injected anomalies. ANOM-02 is a West-region Electronics stockout, and
the generator gives it **no marketing-spend footprint at all** — deliberately. `config.py` says
so in as many words: *"ANOM-02 has no entry on purpose — it is the negative control for
spend-based reasoning."* Marketing spend is untouched during that week precisely so that the
spend table **cannot** explain the dip and the investigator is forced to consult inventory.

Allocate on same-day revenue and that control is destroyed. West Electronics revenue collapses
during the stockout, so West's same-day revenue share collapses, so its allocated spend
collapses with it — even though not one real dollar of spend changed. An agent reading the
result sees spend and revenue falling together and concludes that a marketing cut caused a
stockout. It would be wrong, it would be confident, and the number it was reading would have
been manufactured by my own allocation rule.

This is circular reasoning baked into a table: **the anomaly would be feeding into its own
explanation.**

### The measured difference

Same window, both rules, West-region Electronics. `spend (trailing)` is what the mart contains;
`spend (same-day)` is what the rejected rule would have produced:

| Date | Revenue | Trailing share | Same-day share | Spend (trailing) | Spend (same-day) |
|---|---|---|---|---|---|
| 2025-06-06 | 13,665 | 0.220 | 0.221 | 1,325 | 1,373 |
| 2025-06-08 | 14,851 | 0.221 | 0.238 | 1,619 | 1,728 |
| **2025-06-09** | 9,214 | 0.222 | 0.169 | **1,247** | **957** |
| **2025-06-11** | 5,118 | 0.216 | 0.100 | **1,071** | **512** |
| **2025-06-12** | 4,380 | 0.213 | 0.085 | **1,127** | **468** |
| **2025-06-13** | 4,440 | 0.209 | 0.084 | **1,197** | **487** |
| **2025-06-15** | 9,440 | 0.201 | 0.164 | **1,303** | **1,052** |
| 2025-06-16 | 13,495 | 0.199 | 0.239 | 1,214 | 1,479 |

Bold rows are the stockout, 9-15 June.

Under the rejected rule, allocated spend on 12 June reads **468 against a pre-stockout 1,700 —
a 72% collapse** that never happened. Under the rule actually used, spend holds near 1,100-1,300
throughout while revenue falls 70%. That contrast is the correct signal: *revenue fell, spend
did not, therefore this is not a marketing problem.* Which is exactly the conclusion the data
was constructed to support.

### Why this rule is also defensible on its own terms

Setting the negative control aside, a trailing window is simply a better model of reality.
Media budgets are **planned**, not reactive — a regional allocation is set from recent
performance and then held for a period. A rule where today's regional split responds
instantaneously to today's sales describes no real marketing organisation.

### Rules considered and rejected

| Rule | Why not |
|---|---|
| **Same-day revenue share** | Destroys the ANOM-02 negative control, as above. The anomaly feeds its own explanation. |
| **Even split, 25% each** | Immune to feedback, but wrong in a way that matters: the regions genuinely produce 30/28/22/20 of revenue. An even split permanently understates North's efficiency and overstates West's, inventing a regional performance story that is purely an artefact of the rule. |
| **Fixed whole-series share** | Also immune to feedback, and better than an even split, but static — the regions drift apart over two years by design, so a single fixed share is wrong at both ends of the series. |
| **Trailing 28-day share** | **Chosen.** Independent of the current day, so no feedback. Tracks genuine regional drift. Matches how budgets are actually planned. |

### The edge case, handled and flagged

The first day of the series has no prior revenue to compute a share from. Rather than leave a
NULL or silently drop the row, those rows fall back to an even 25% split and are labelled
`allocation_basis = 'even_split_no_history'`.

That is **60 rows out of 43,860** — one date, 2024-01-01, across 3 channels x 5 categories x
4 regions. Days 2 through 28 need no special handling: the window is simply shorter, which is
correct behaviour rather than an edge case. Every row carries `allocation_basis`, so a consumer
can always tell which rule produced the number in front of them, and an `accepted_values` test
pins the column to those two permitted values.

### Not one cent is created or destroyed

Splitting money four ways and rounding to cents does not, in general, sum back to what you
started with. Four values rounded to two decimals can miss the original total by a cent or two,
and across 10,965 source cells that silently invents or destroys money.

The fix is the **largest remainder method**, standard practice in accounting: allocate the
rounded amounts, then give the leftover residue to the largest-share region. The row that
absorbs it is flagged `carries_rounding_residue`, so the adjustment is visible rather than
hidden.

`assert_spend_allocation_reconciles.sql` proves it holds, and checks four separate things:

1. Every one of the 10,965 source cells sums back to its source total **exactly** — not within
   a tolerance.
2. The grand total reconciles: **9,779,277.61 in, 9,779,277.61 out, difference 0.00**.
3. Each cell's four region shares sum to 1.
4. Each cell allocates across exactly 4 regions — catching a dropped or duplicated join.

---

## Decision 2 — The 8 uncosted SKUs are excluded from the cost basis and counted on every row

Day 3 left 8 of 120 SKUs with a NULL `unit_cost`, deliberately refusing to impute a fabricated
cost into a financial metric. That decision was correct, and it left a problem for today: what
does a margin number mean when 7% of the products behind it have no known cost?

### The failure mode being avoided

The default behaviour is the dangerous one. In SQL, `AVG` and `SUM` skip NULLs silently. Write
the obvious margin query and it will return a perfectly plausible number, computed over only
the SKUs that happened to have a cost, with nothing anywhere indicating that some were missing.
Nobody reading a dashboard sees the gap. The number is not wrong enough to look wrong.

**A silent exclusion and a silent inclusion are equally bad. What matters is that the exclusion
is stated.**

### What was built

Three layers, each with one job:

**`dim_product`** carries `is_margin_calculable` — simply whether `unit_cost` is present. The
question "can this SKU be costed?" is answered once, in one place, rather than being
rediscovered as a NULL by every downstream query.

**`int_category_cost_basis`** rolls the SKU-grain dimension up to category grain and computes
`avg_unit_cost` **over calculable SKUs only** — the uncosted ones are excluded from the average,
never imputed into it. Alongside the average it carries `skus_in_category`,
`skus_with_unit_cost`, `skus_excluded_from_cost_basis`, `cost_basis_coverage_pct`, and
`excluded_sku_ids`.

**`fct_daily_margin`** computes the margin and carries those counts through onto **every single
row**. The exclusion count is `not_null` tested. It is structurally impossible to read a margin
figure from this mart without the coverage of its cost basis sitting in the adjacent column.

### What the gap actually is

| Category | SKUs | Costed | Excluded | Coverage | Excluded SKUs |
|---|---|---|---|---|---|
| Electronics | 32 | 31 | 1 | 96.9% | ELEC-0020 |
| Apparel | 28 | 27 | 1 | 96.4% | APRL-0014 |
| Home & Garden | 22 | 20 | 2 | 90.9% | HOME-0001, HOME-0004 |
| Sports | 20 | 18 | 2 | 90.0% | SPRT-0013, SPRT-0017 |
| Beauty | 18 | 16 | 2 | 88.9% | BEAU-0006, BEAU-0012 |

Coverage never drops below 88.9%, so every category remains estimable — but Beauty's margin
rests on a visibly thinner basis than Electronics', and now anyone using it can see that.

Naming the excluded SKUs, not merely counting them, is deliberate: it turns "8 products have no
cost" from a statistic into a work item somebody can actually go and fix.

### Margin here is an estimate, and the name says so

Every margin column is prefixed `estimated_`. This is not hedging — it reflects a real
structural limitation. **Revenue is recorded per category; cost is known per SKU.** The data
never says which SKUs made up a given day's Electronics sales, so units are costed at the
category's average unit cost.

That embeds an assumption: that the day's product mix resembles the category average, and that
the uncosted SKUs are not systematically cheaper or dearer than the costed ones. Both are
plausible and neither is verifiable from this data. Calling the output `estimated_gross_margin`
rather than `gross_margin` is what keeps that assumption from being forgotten by the third
person to use the column.

`is_margin_estimable` guards the degenerate case: if a category ever had zero costed SKUs, the
margin columns are NULL **by design and by flag**, not by accident. All five categories are
currently estimable; the flag exists so that a future load which breaks that fails loudly.

---

## Decision 3 — The product dimension is aggregated before it is joined

This one is not a business judgement, but it is the subtlest bug avoided today and worth
recording.

The task was to join the cleaned product dimension into `fct_daily_revenue`. The obvious
implementation joins `dim_product` to the revenue fact on `category` — and it is catastrophic.
Revenue is one row per category; `dim_product` is one row per SKU. Electronics has 32 SKUs, so
that join turns **one revenue row into 32**, and `SUM(gross_revenue)` reports roughly 24 times
the real figure.

Nothing about the result looks broken. Every individual row is plausible. Only the totals are
wrong, and they are wrong by a factor large enough to invalidate everything downstream.

So the dimension is rolled up to category grain in `int_category_cost_basis` **first**, and the
fact joins that. Every join in the mart layer is then one-to-one or many-to-one, and the row
count is preserved end to end.

`assert_marts_do_not_inflate_revenue.sql` is the guard. It asserts that `fct_daily_revenue` has
exactly the row count *and* exactly the revenue total of `stg_daily_revenue`
(**43,860 rows / 101,656,971.77 both sides**), that `fct_daily_margin` matches the fact, and
that `fct_daily_stockout` has no duplicate grain keys. A fan-out bug cannot survive a build.

---

## Decision 4 — The stockout view is aggregated to the grain the fact joins on

`fct_daily_stockout` is the evidence table for exactly the case where marketing spend has
nothing to say — which, by construction, is ANOM-02.

Inventory is recorded per SKU per day. The revenue fact is per category, channel and region.
So the view aggregates stock to **date x category x region** — the three columns the two share.
Channel is deliberately absent: stock is physical, held in a regional distribution centre, and
is not channel-specific. Joining on those three columns is many-to-one, which is correct — all
three channels in a region see the same shelf.

It is a **view** rather than a table because it is an aggregation over 87,720 rows that Postgres
computes in milliseconds, and freshness matters more than a saved fraction of a second.

Beyond counting stockouts it carries `stocked_out_sku_ids` — the actual SKU list. The Day 8
deliverable is a written brief for a human, and "6 of 8 West Electronics SKUs were out of stock,
including ELEC-0004 and ELEC-0008" is an actionable sentence in a way that "stockout rate 75%"
is not.

Verified against the injected event: zero stockouts on 6-8 June, then exactly 6 of 8 West
Electronics SKUs at zero every day from 9 June to 15 June, then back to zero stockouts on
16 June. The mart isolates the event cleanly, without the flag having been told about it.

---

## What the tests protect against

191 tests pass, up from 81. The Day 4 additions that carry real weight:

**`assert_spend_allocation_reconciles`** — the allocation neither creates nor destroys money, to
the cent, per cell and in total. This is the test that makes the allocation rule safe to build on.

**`assert_marts_do_not_inflate_revenue`** — the dimensional joins enrich the fact without
multiplying it. Guards the single most damaging bug available in this layer.

**`assert_margin_exclusions_are_accounted`** — cross-checks `dim_product` against
`int_category_cost_basis` against `fct_daily_margin`, so the exclusion count a margin row
advertises has to match the dimension it came from. An exclusion that is not counted is the same
as a silent drop, and this is what stops the two drifting apart.

**`accepted_values` on `spend_allocation_basis`** — pins the allocation to its two permitted
rules, so a third one cannot appear unannounced.

**`relationships` from every mart key back to staging** — `fct_daily_revenue.revenue_key` to
`stg_daily_revenue`, `dim_product.sku_id` to `stg_product_master`, and each fact's category to
`dim_product`. The mart layer cannot invent a key that staging never had.

---

## Running it

Unchanged from Day 3 — always through the wrapper, which loads `.env` into the environment:

```
run_dbt.bat build
```

Marts land in `analytics`, intermediates in `intermediate`, staging in `staging`. That
separation is what makes the Day 7 guardrail enforceable: the agent's read-only role gets
granted on `analytics` alone, and `analytics` contains only the four objects a consumer should
ever read.
