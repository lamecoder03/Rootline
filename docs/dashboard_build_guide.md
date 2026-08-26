# Building the dashboard in Power BI Desktop

> **NOT YET BUILT — prep work only, dashboard not implemented.** This is a step-by-step guide
> to follow, not a record of a finished build. No `.pbix` exists yet.

A step-by-step build, to be followed by hand in the Power BI Desktop GUI. Connection details are
in `docs/powerbi_connection.md`; every measure referenced here is defined in
`docs/dax_measures.md`.

**What this dashboard is for.** The project's founding decision is that the deliverable is the agent's
written brief, not the dashboard — *"the dashboard exists so a human can sanity-check the
brief."* That single sentence decides every layout question below. This is not an exploration
tool and not an executive summary. It is the page a Revenue Ops lead opens **next to a brief**
to answer one question: *do I believe this?*

So each visual below earns its place by making one specific claim in a brief checkable in a few
seconds. A visual that cannot do that is decoration, however good it looks.

---

## Step 1 — Load the data

Follow `docs/powerbi_connection.md` steps 1–6. In summary: `localhost:5433`,
`revenue_anomaly`, **Import**, credentials on the Database tab as `revenue_reporting` with
*Encrypt connection* unticked, and the six `analytics` tables selected.

**Click Load, not Transform Data.** The temptation is to open Power Query and tidy things. Don't.
These marts are the output of a dbt project with 191 passing tests; any cleaning done in Power
Query is logic that lives outside version control, has no tests, and is invisible to the agent
reading the same tables. If something needs changing, it changes in dbt.

**Verify the load before building anything.** Row counts:

| Table | Expected rows |
|---|---|
| `fct_daily_revenue` | 43,860 |
| `fct_daily_margin` | 43,860 |
| `fct_daily_stockout` | 14,620 |
| `dim_product` | 120 |
| `detected_anomalies` | 44 |
| `detected_anomaly_points` | 166 |

Then check money typing in *Model view*: `gross_revenue`, `marketing_spend_usd`,
`estimated_gross_margin` and `estimated_cogs` must be **Fixed Decimal Number**. Power BI
sometimes reads Postgres `numeric` as floating-point Decimal Number, which reintroduces the
binary-float rounding the entire warehouse was built to avoid.

---

## Step 2 — Build the model

This is the step that decides whether the dashboard is trustworthy, and it is the one most
easily rushed.

### 2a. The problem you are solving

You have **three fact tables at three different grains** and no dimensions:

| Fact | Grain | Date column |
|---|---|---|
| `fct_daily_revenue` | date × category × channel × region | `order_date` |
| `fct_daily_margin` | date × category × channel × region | `order_date` |
| `fct_daily_stockout` | date × category × **region only** | `snapshot_date` |

Two facts share a grain; the third is one dimension coarser. You cannot relate facts directly to
each other — a relationship between `fct_daily_revenue` and `fct_daily_margin` on any single
column is many-to-many and will silently produce wrong totals. Facts relate to **shared
dimensions**, never to each other. That is the star schema the marts layer built, and it has to be
reconstructed here because Postgres marts do not carry the relationships with them.

### 2b. Create the date table

Exactly as in `docs/dax_measures.md` — *Modeling → New table*, paste the `Date =` definition,
then *Table tools → Mark as date table → Date*. Set `Month` to sort by `MonthStart` and
`DayOfWeek` by `DayOfWeekNo`.

**Why it earns its place:** without it, revenue (`order_date`) and stockouts (`snapshot_date`)
cannot share an axis, and the ANOM-02 visual in Step 6 is impossible. Every time-intelligence
measure also depends on a marked date table.

### 2c. Create the three shared dimensions

*Modeling → New table*, three times:

```dax
dim_category = DISTINCT ( fct_daily_revenue[category] )
dim_channel  = DISTINCT ( fct_daily_revenue[channel] )
dim_region   = DISTINCT ( fct_daily_revenue[region] )
```

`fct_daily_revenue` is the source for all three because it is the only table carrying all three
columns at full cardinality — 5 categories, 3 channels, 4 regions, the 60 cells the detector
scores.

**Why they earn their place:** a slicer bound directly to `fct_daily_revenue[category]` filters
*only that table*. Margin and stockout visuals on the same page would ignore it entirely, and
nothing on screen would say so. Conformed dimensions are what make one slicer mean one thing
across the whole page.

### 2d. Wire the relationships

In *Model view*, create these. All are **one-to-many, single direction**, from the dimension
(one side) to the fact (many side):

| From (one) | To (many) | Notes |
|---|---|---|
| `Date[Date]` | `fct_daily_revenue[order_date]` | |
| `Date[Date]` | `fct_daily_margin[order_date]` | |
| `Date[Date]` | `fct_daily_stockout[snapshot_date]` | Different column name, same meaning |
| `Date[Date]` | `detected_anomaly_points[order_date]` | |
| `Date[Date]` | `detected_anomalies[start_date]` | See the note below |
| `dim_category[category]` | `fct_daily_revenue[category]` | |
| `dim_category[category]` | `fct_daily_margin[category]` | |
| `dim_category[category]` | `fct_daily_stockout[category]` | |
| `dim_category[category]` | `detected_anomalies[category]` | |
| `dim_category[category]` | `detected_anomaly_points[category]` | |
| `dim_channel[channel]` | `fct_daily_revenue[channel]` | |
| `dim_channel[channel]` | `fct_daily_margin[channel]` | |
| `dim_channel[channel]` | `detected_anomalies[channel]` | |
| `dim_channel[channel]` | `detected_anomaly_points[channel]` | **No stockout relationship — none exists** |
| `dim_region[region]` | `fct_daily_revenue[region]` | |
| `dim_region[region]` | `fct_daily_margin[region]` | |
| `dim_region[region]` | `fct_daily_stockout[region]` | |
| `dim_region[region]` | `detected_anomalies[region]` | |
| `dim_region[region]` | `detected_anomaly_points[region]` | |
| `detected_anomalies[anomaly_key]` | `detected_anomaly_points[anomaly_key]` | Incident → its days |

**Keep every relationship single-direction.** Power BI will offer bidirectional filtering and it
will seem helpful. It creates ambiguous filter paths across three facts sharing three dimensions,
and ambiguity in a model shows up as numbers that change depending on which visual you clicked
first. If a specific visual needs reverse propagation, use `CROSSFILTER` in that one measure.

**On `detected_anomalies[start_date]`:** an incident spans a range, but a relationship can only
use one column. `start_date` is the active one, so a date-filtered anomaly count means
"incidents that *started* in this period". That is the honest reading and it is what you want on
a monitoring page. For per-day analysis use `detected_anomaly_points`, which is genuinely at
day grain.

### 2e. Do NOT relate `dim_product` to the facts

This is the single most important modelling decision on the page, and Power BI will actively
suggest getting it wrong — `dim_product[category]` and `fct_daily_revenue[category]` share a name
and a domain, so autodetect may propose joining them.

**Joining `dim_product` to a fact on `category` fans the fact out.** `dim_product` is at SKU
grain: there are 32 Electronics SKUs. Relating it to a category-grain fact repeats every
Electronics revenue row 32 times, and total revenue jumps by roughly 24× overall. The marts layer has a
dedicated dbt test for exactly this — `assert_marts_do_not_inflate_revenue` — because it is the
classic star-schema failure and it produces numbers that look plausible until someone checks a
total.

Two acceptable options:

- **Leave `dim_product` unrelated** (simplest, and enough for this dashboard). Use it only for
  standalone SKU reference visuals and for `SKUs Excluded from Cost Basis`.
- **Relate `dim_product[category]` → `dim_category[category]` as many-to-one.** Legitimate, and
  it lets a product-level slicer filter the facts *via* the category dimension. But understand
  what it means: selecting one SKU shows **its whole category's** revenue, because the facts have
  no finer grain to offer. If you take this option, label any such visual accordingly.

If Power BI has auto-created a `dim_product` → fact relationship on load, delete it. Then check
`Total Revenue` reads **$101,656,971.77**. If it reads a multiple of that, a fan-out is live.

---

## Step 3 — Create the measures

Create every measure from `docs/dax_measures.md`. Put them in one place: create an empty
table (*Enter data*, name it `_Measures`, load one dummy column, delete the column) and set each
measure's Home Table to it. Measures scattered across fact tables become unfindable at about
fifteen.

Build them in this order, checking as you go:

1. `Total Revenue` — verify **$101,656,971.77**
2. `Total Units`, `Total Orders`, `Average Order Value`, `Revenue per Unit`
3. `Marketing Spend` — verify **$9,779,277.61**, the allocation total
4. `Estimated Gross Margin`, `Estimated Gross Margin %` — verify **35.37%**
5. `Cost Basis Coverage %`, `SKUs Excluded from Cost Basis` (**8**), `Margin Caveat`
6. `Anomaly Count` (**44**), `Anomalies - Drops` (**26**), `Anomalies - Spikes` (**18**)
7. `Severity` as a **calculated column** on `detected_anomalies`, plus `Confidence Band`
8. `Revenue Impact`, `Revenue Impact (Absolute)`, `Worst Z Score`
9. `Stockout Rate %`, `Days With Stockout`, `SKUs Out of Stock`
10. `Spend Allocation Note`, `Stockout Scope Note`

Each checkpoint catches a different failure: step 1 catches fan-out, step 3 catches a broken
spend import, step 4 catches margin measured as an average instead of a ratio.

---

## Step 4 — Visual 1: daily revenue with anomalies overlaid

**The primary visual. Everything else supports it.**

**Why it earns its place.** Every brief opens with a claim of the form *"revenue in this slice
fell X% between these dates."* This visual is where a human confirms or rejects that in one
glance — the dip is either visibly there and visibly unusual against its own history, or it is
not. Without it, the reader has to take the agent's first sentence on trust, and the entire
purpose of the dashboard is to not do that.

**Build it:**

1. Insert a **Line chart**.
   - X-axis: `Date[Date]` — set to **Continuous**, not Categorical. Categorical omits days with
     no rows and silently closes gaps in the series, which is precisely the artefact a
     revenue-collapse investigation must not have.
   - Y-axis: `Total Revenue`.
2. Add a second line for the counterfactual: `Anomaly Expected Revenue` from
   `detected_anomaly_points`. It is populated only on flagged days, so it appears as short
   segments exactly where the detector fired — the visual answer to "expected versus actual".
3. Overlay the anomalies themselves. Two options, and the second is better:
   - *Simple:* add `Anomaly Days` as a second Y-axis column on a **Line and stacked column
     chart**, so bars mark flagged days beneath the revenue line.
   - *Better:* keep the line chart, and add **Error bars** or a **scatter layer** — or most
     simply, set the line's **Data colors → conditional formatting** driven by a measure:
     ```dax
     Anomaly Marker = IF ( NOT ISBLANK ( [Anomaly Days] ), [Total Revenue], BLANK () )
     ```
     Add `Anomaly Marker` as a third series with markers on and no line. Flagged days become
     dots sitting directly on the revenue curve.
4. Turn on the **Zoom slider** for the X-axis. A 731-day series needs it.
5. Title it explicitly: **"Daily gross revenue with detected anomalies"** — "gross" belongs in
   the title, per the assumption discipline.

**Read it as:** the dots are where the detector fired. If a brief claims a dip the dots do not
mark, either the brief is discussing something below the detection threshold, or it is wrong.

---

## Step 5 — Visual 2: revenue and margin by category

**Why it earns its place.** A brief that attributes a revenue move to a *cause* implies the move
is confined to a slice. This visual tests that instantly: a genuine category-specific event looks
like one bar moving while four sit still. It also carries the margin dimension, which is what
separates a discount-driven revenue lift (revenue up, margin % down) from genuine demand growth
(both up) — the distinction that decides whether ANOM-01 is "a promotion" or "a good week".

**Build it:**

1. Insert a **Line and clustered column chart**.
   - X-axis: `dim_category[category]`
   - Column y-axis: `Total Revenue`
   - Line y-axis: `Estimated Gross Margin %`
2. Format the line axis as a percentage and **do not start it at zero**. Margin varies across a
   narrow band; a zero-based axis flattens the differences that matter.
3. **Add the caveat, and do not skip this.** Place a **Card** directly beneath, bound to
   `Margin Caveat`. Sliced to Beauty it reads *"Estimated — only 88.9% of SKUs are costed"*.
   The margin bar and its coverage caveat must be readable in the same glance, or the caveat is
   not doing its job.
4. Add a **Card** for `SKUs Excluded from Cost Basis` (reads **8**) somewhere on the page.

**Why the caveat is non-negotiable here.** `cost_basis_is_complete` is `FALSE` on all 43,860
margin rows — no category has a complete cost basis. A margin percentage on a dashboard is read
as a financial fact within about two seconds. If the only disclosure lives in a markdown file
nobody opens, the dashboard is misleading by omission, which is the failure mode the marts layer
`estimated_` prefixes exist to prevent.

---

## Step 6 — Visual 3: the ANOM-02 stockout window

**Why it earns its place, and why this specific example.** The other visuals are general. This
one is a **worked example of the hardest reasoning the agent has to do**, and it is the single
best thing on the dashboard for demonstrating the project to another person.

ANOM-02 is the negative control. Electronics × West revenue collapses 52% over 2025-06-09 →
06-15, and **marketing spend is deliberately untouched across that window**. An investigator that
explains every dip by pointing at marketing spend gets this one wrong. The stockout is the real
cause, and it is unambiguous in the data:

| Date | SKUs tracked | Out of stock | Rate |
|---|---|---|---|
| 2025-06-07 | 8 | 0 | 0.0% |
| 2025-06-08 | 8 | 0 | 0.0% |
| **2025-06-09** | 8 | **6** | **75.0%** |
| **2025-06-10** | 8 | **6** | **75.0%** |
| **2025-06-11** | 8 | **6** | **75.0%** |
| **2025-06-12** | 8 | **6** | **75.0%** |
| **2025-06-13** | 8 | **6** | **75.0%** |
| **2025-06-14** | 8 | **6** | **75.0%** |
| **2025-06-15** | 8 | **6** | **75.0%** |
| 2025-06-16 | 8 | 0 | 0.0% |
| 2025-06-17 | 8 | 0 | 0.0% |

Zero either side, six of eight for exactly the seven event days. Put that on a page next to flat
marketing spend and the conclusion is not a judgement call — it is visible.

**Build it as a dedicated page**, named "ANOM-02 — worked example":

1. **Page-level filters** (Filters pane → *Filters on this page*), so the page is permanently
   scoped without slicers the reader must set correctly:
   - `dim_category[category]` is `Electronics`
   - `dim_region[region]` is `West`
   - `Date[Date]` between `2025-06-01` and `2025-06-24`
2. **Top visual — Line chart:** X `Date[Date]` (Continuous), Y `Total Revenue` and
   `Anomaly Expected Revenue`. The gap between the two lines is the loss.
3. **Middle visual — Line and clustered column chart:** X `Date[Date]`, columns
   `SKUs Out of Stock`, line `Stockout Rate %`. The block of six lines up under the revenue
   trough.
4. **Bottom visual — Line chart:** X `Date[Date]`, Y `Marketing Spend`. **This is the point of
   the page.** It is flat. A visual whose job is to show that nothing happened is doing real
   work — it is what rules the alternative out.
5. **Cards:** `Revenue Impact` and `Days With Stockout` (**7**).
6. **Add the scope caveat.** Place a Card bound to `Stockout Scope Note`. On this page it stays
   blank — no channel filter is applied — which is exactly right, and it will appear if someone
   later adds a channel slicer and wonders why the stockout numbers do not move.
7. **Text box, in plain words**, because the page should be self-explanatory to someone who has
   never seen the project:

   > Electronics revenue in the West region fell 52% over 2025-06-09 to 06-15. Six of eight
   > tracked SKUs were out of stock for exactly those seven days, and zero on the days either
   > side. Marketing spend did not change. This is an inventory failure, not a marketing one —
   > and it is the case that catches an investigator who explains every dip with marketing spend.

**Note on stacking the three charts.** Align their X-axes and set all three to the same date
range so the vertical alignment carries the argument. Three charts sharing an axis is a
lead-lag comparison; three charts on different axes is three unrelated pictures.

---

## Step 7 — Filters

Add these as slicers on the main page. Put them in a left-hand column or a collapsible pane, not
scattered.

| Slicer | Field | Style | Why |
|---|---|---|---|
| Date range | `Date[Date]` | **Between** (slider) | The primary control. A brief covers a window; the reader needs to match it |
| Category | `dim_category[category]` | Dropdown, multi-select | 5 values |
| Channel | `dim_channel[channel]` | Tile / horizontal | Only 3 values — a dropdown wastes a click |
| Region | `dim_region[region]` | Dropdown, multi-select | 4 values |

**Bind every slicer to the dimension tables, never to a fact column.** A slicer on
`fct_daily_revenue[category]` filters revenue only, leaving the margin and stockout visuals on
the same page showing unfiltered data with no indication that they are.

**Two things to do deliberately:**

1. **Set *Edit interactions* for the ANOM-02 page** so the main-page slicers do not reach it, or
   simply keep that page's filters at page level as in Step 6. The worked example must stay
   scoped to its window; a reader changing the date range on another page should not silently
   break it.
2. **Keep `Stockout Scope Note` visible on any page carrying both a channel slicer and a stockout
   visual.** This is the one place where a filter appears to work and does not: stock is physical
   and has no channel grain, so a channel selection cannot reach `fct_daily_stockout`. The
   measure says so on screen. Faking a channel relationship to make the slicer "work" would
   invent a grain the business does not have.

---

## Step 8 — Check the build

Before calling it done, verify each of these. Each catches a specific, common, silent error:

| Check | Expected | Catches |
|---|---|---|
| `Total Revenue`, no filters | **$101,656,971.77** | `dim_product` fan-out |
| `Marketing Spend`, no filters | **$9,779,277.61** | Broken spend import |
| `Estimated Gross Margin %`, no filters | **35.37%** | Margin computed as an average |
| `Anomaly Count`, no filters | **44** | Relationship on the wrong date column |
| Select one category — do margin *and* stockout visuals both move? | Yes | Slicers bound to a fact instead of a dimension |
| Select one channel — does the stockout visual stay put? | Yes, **and the note appears** | Correct: stock has no channel grain |
| Zoom the date axis to a single week | No gaps, no closed-up days | Categorical X-axis hiding missing days |
| ANOM-02 page, `Days With Stockout` | **7** | Page filters set to the wrong window |

The margin % check is the one worth doing carefully. `AVERAGE(estimated_gross_margin_pct)` across
all 43,860 rows returns something close to 35% and looks perfectly fine — it is wrong for every
sliced total, and only a deliberate check finds it.

---

## What was deliberately not built

- **No executive KPI banner.** Big cards showing revenue and margin invite the dashboard to be
  read as a performance report. It is a verification tool for a brief; a headline number nobody
  is going to act on is a distraction with a strong claim to attention.
- **No forecasting or trend line.** The detector already produces a counterfactual —
  `expected_revenue` — that is derived from the documented detection method and validated against
  ground truth. A Power BI trend line would be a *second, different, untested* expectation on the
  same chart, and where the two disagree the reader has no way to know which to believe.
- **No SKU-level revenue drill-through.** It does not exist in the data. The facts stop at
  category grain, and building a visual that appears to offer SKU revenue would be a fabrication
  — the same fan-out that Step 2e refuses.
- **No `.pbix` in this repo yet.** The file is built by hand from this guide. It is added, with
  a screenshot, once it has been built and confirmed working.
