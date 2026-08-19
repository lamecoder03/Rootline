# Day 9 — DAX measure reference

Every measure the dashboard needs, with the assumption it rests on stated next to it. This is
the Day 4 `estimated_` prefix discipline carried into the BI layer: a measure whose name does
not admit its approximation will be quoted as fact by whoever reads the dashboard.

Two rules govern everything below.

**1. Ratios are recomputed, never averaged.** `AVERAGE(fct_daily_margin[estimated_gross_margin_pct])`
is wrong at every grain above a single fact row, because it weights a $40 cell and a $40,000 cell
equally. Every percentage here is `DIVIDE(SUM(numerator), SUM(denominator))`, which re-derives the
ratio at whatever grain the visual is asking about. The same applies to
`stockout_rate_pct` and `cost_basis_coverage_pct` — both are pre-computed per row and both are
traps if aggregated with `AVERAGE`.

**2. `DIVIDE()`, not `/`.** `DIVIDE` returns `BLANK()` on a zero denominator instead of an error.
A visual sliced to a category-region-day with no rows should show empty, not break the page.

---

## Prerequisite: a date table

The six imported tables contain **no date dimension**, and Power BI's time intelligence
(`DATESYTD`, `SAMEPERIODLASTYEAR`, period-over-period) requires one marked table with a
contiguous, gap-free date column.

More importantly for this project: the three fact tables carry **three different date columns** —
`fct_daily_revenue[order_date]`, `fct_daily_stockout[snapshot_date]` and
`detected_anomaly_points[order_date]`. Without a shared date dimension there is no way to put
revenue and stockouts on the same axis, which is the whole point of the ANOM-02 visual.

Create it as a calculated table (*Modeling → New table*):

```dax
Date =
VAR MinDate = MIN ( fct_daily_revenue[order_date] )
VAR MaxDate = MAX ( fct_daily_revenue[order_date] )
RETURN
ADDCOLUMNS (
    CALENDAR ( MinDate, MaxDate ),
    "Year",        YEAR ( [Date] ),
    "Month",       FORMAT ( [Date], "mmm yyyy" ),
    "MonthStart",  EOMONTH ( [Date], -1 ) + 1,
    "Quarter",     "Q" & FORMAT ( [Date], "Q" ) & " " & YEAR ( [Date] ),
    "DayOfWeek",   FORMAT ( [Date], "ddd" ),
    "DayOfWeekNo", WEEKDAY ( [Date], 2 ),
    "IsWeekend",   WEEKDAY ( [Date], 2 ) > 5
)
```

Then *Table tools → Mark as date table → Date*. Sort `Month` by `MonthStart` and `DayOfWeek` by
`DayOfWeekNo`, or they sort alphabetically and April leads the year.

The range is derived from the fact rather than hardcoded, so it stays correct if the generator's
2024-01-01 → 2025-12-31 window ever changes. Actual span today: **2024-01-01 to 2025-12-31,
731 days.**

---

## Revenue

```dax
Total Revenue = SUM ( fct_daily_revenue[gross_revenue] )
```

`gross_revenue` is `numeric(14,2)` in Postgres and lands as Decimal — money is never a float
anywhere in this project, and it should not become one here. If Power BI types the column as
*Decimal Number* (floating point), change it to **Fixed Decimal Number** in the model view.

**Assumption stated:** this is *gross* revenue — before returns, discounts and refunds, none of
which exist in the source data. It is not net revenue and must not be labelled "Revenue" on a
visual without qualification. Measured total across the full 731-day series:
**$101,656,971.77** over 43,860 rows.

Use that as the load check. If the card does not read $101,656,971.77 the import is
incomplete — most often because *Transform Data* was used and a filter step survived.

### Revenue by category, channel and region

These need no separate measures. `Total Revenue` sliced by `dim_category[category]`,
`dim_channel[channel]` or `dim_region[region]` gives the breakdown, because the relationships
propagate the filter. Writing `Revenue - Electronics = CALCULATE ( [Total Revenue], ... )` for
each value hardcodes the dimension into the model and breaks the moment a category is added.

The one case that *does* earn a measure is a share-of-total, where the denominator must escape
the current filter:

```dax
Revenue % of Total =
DIVIDE (
    [Total Revenue],
    CALCULATE ( [Total Revenue], REMOVEFILTERS ( dim_category, dim_channel, dim_region ) )
)
```

`REMOVEFILTERS` is named explicitly rather than using `ALL()` over the whole model, so a date
slicer still applies — "Electronics was 31% of revenue **in this period**" is the useful
statement; "31% of all revenue ever" is not what the visual is asking.

### Supporting volume measures

```dax
Total Units  = SUM ( fct_daily_revenue[units] )
Total Orders = SUM ( fct_daily_revenue[orders] )

Average Order Value = DIVIDE ( [Total Revenue], [Total Orders] )
Revenue per Unit    = DIVIDE ( [Total Revenue], [Total Units] )
```

`Revenue per Unit` is the discount fingerprint the agent looks for: in a promotion, units rise
faster than revenue, so this falls while `Total Revenue` climbs. It is the fastest way for a
human to sanity-check a brief that claims "promotion".

### Marketing spend

```dax
Marketing Spend = SUM ( fct_daily_revenue[marketing_spend_usd] )
Return on Ad Spend = DIVIDE ( [Total Revenue], [Marketing Spend] )
```

**Assumption stated, and it matters more than it looks.** `marketing_spend_usd` in
`fct_daily_revenue` is **allocated, not observed**. Source spend is one grain coarser than
revenue — it has no region — so Day 4 splits each date × channel × category figure across
regions by that region's **trailing 28-day revenue share, excluding the current day**.

Consequences for anyone reading a spend visual:

- At **date × channel × category** grain the number is real source data.
- At any grain that involves **region**, it is an estimate produced by a documented rule.
- The trailing-28-day basis is deliberate: same-day allocation would drag allocated spend down
  wherever revenue collapsed, manufacturing a marketing explanation for the ANOM-02 stockout.
  Measured, it would have shown a 72% spend collapse on 12 June that never happened.
- 60 rows (day one of the series, which has no history) fall back to an even split and are
  flagged `spend_allocation_basis = 'even_split_no_history'`.

A measure that surfaces the caveat rather than burying it in this document:

```dax
Spend Allocation Note =
IF (
    ISFILTERED ( dim_region[region] ),
    "Spend is allocated to region by trailing 28-day revenue share, not observed",
    BLANK ()
)
```

Drop that in a card beside any region-sliced spend visual. It disappears when it does not apply.

---

## Margin — and the flag that does not do what its name suggests

This is the section where the honest answer differs from the obvious one, so it is worth reading
before writing any margin measure.

### The correct margin measures

```dax
Estimated COGS         = SUM ( fct_daily_margin[estimated_cogs] )
Estimated Gross Margin = SUM ( fct_daily_margin[estimated_gross_margin] )

Estimated Gross Margin % =
DIVIDE (
    SUM ( fct_daily_margin[estimated_gross_margin] ),
    SUM ( fct_daily_margin[gross_revenue] )
)
```

Keep `Estimated` in the measure names. It is not verbosity — it is the same reason the Day 4
columns carry the prefix, and a measure called `Gross Margin %` on a dashboard will be read as
audited financial truth.

Measured across the full series: estimated gross margin **$35,954,262.15** on
$101,656,971.77 of revenue, i.e. **35.37%**. Use it as the second load check.

**Why `estimated`:** revenue is recorded per *category*, but cost is recorded per *SKU*. There is
no SKU dimension on the fact, so units are costed at the **category average unit cost over the
costed SKUs only**. That is an approximation whenever a category's SKUs have differing costs and
differing mix — which is always.

### `is_margin_calculable` vs `is_margin_estimable`, and why neither is a useful filter

There are two similarly-named flags and they live at different grains:

| Flag | Table | Grain | Meaning | Values today |
|---|---|---|---|---|
| `is_margin_calculable` | `dim_product` | one SKU | This SKU has a `unit_cost` | **112 true, 8 false** |
| `is_margin_estimable` | `fct_daily_margin` | one fact row | This row has a usable cost basis | **43,860 true, 0 false** |

**Filtering the fact on `is_margin_estimable = TRUE` excludes nothing — it is true on all 43,860
rows.** A measure written as
`CALCULATE ( [Estimated Gross Margin %], fct_daily_margin[is_margin_estimable] = TRUE )` looks
careful, changes no number, and gives false comfort. Do not write it.

**And `is_margin_calculable` cannot be used to filter the fact either.** It is a property of a
SKU, and `fct_daily_margin` has no SKU column — its grain is date × category × channel × region.
The 8 uncosted SKUs were already excluded from the cost basis upstream, at build time. Their
*cost* is out; their *revenue and units are still in the fact*, because those are real sales
that genuinely happened. There is no filter that can retroactively remove them, and one that
appeared to would be lying.

So the correct treatment is **disclosure, not filtering** — which is exactly what the Day 4
design intended by counting the exclusion on every margin row.

### The disclosure measures

```dax
Cost Basis Coverage % =
DIVIDE (
    SUM ( fct_daily_margin[skus_with_unit_cost] ),
    SUM ( fct_daily_margin[skus_in_category] )
)

SKUs Excluded from Cost Basis =
CALCULATE (
    DISTINCTCOUNT ( dim_product[sku_id] ),
    dim_product[is_margin_calculable] = FALSE
)
```

`Cost Basis Coverage %` is a weighted recomputation, not an average of the pre-computed
`cost_basis_coverage_pct` column — that column is correct per row and misleading when averaged
across categories of different sizes.

Actual coverage, measured:

| Category | SKUs | Excluded | Coverage |
|---|---|---|---|
| Electronics | 32 | 1 | 96.9% |
| Apparel | 28 | 1 | 96.4% |
| Home & Garden | 22 | 2 | 90.9% |
| Sports | 20 | 2 | 90.0% |
| Beauty | 18 | 2 | 88.9% |

**`cost_basis_is_complete` is `FALSE` on all 43,860 rows** — every category has at least one
uncosted SKU, so no margin figure anywhere in this warehouse rests on a complete cost basis.
That is worth knowing before margin is quoted in a meeting.

A warning measure that makes it visible rather than footnoted:

```dax
Margin Caveat =
VAR Coverage = [Cost Basis Coverage %]
RETURN
SWITCH (
    TRUE (),
    ISBLANK ( Coverage ), BLANK (),
    Coverage < 0.90, "Estimated — only " & FORMAT ( Coverage, "0.0%" ) & " of SKUs are costed",
    Coverage < 1.00, "Estimated — " & FORMAT ( Coverage, "0.0%" ) & " cost-basis coverage",
    "Cost basis complete"
)
```

Sliced to Beauty this reads *"Estimated — only 88.9% of SKUs are costed"*. Put it on a card
directly beneath the margin % visual, where it is read at the same moment as the number it
qualifies.

---

## Anomalies

### Counts

```dax
Anomaly Count = DISTINCTCOUNT ( detected_anomalies[anomaly_key] )

Anomaly Days = COUNTROWS ( detected_anomaly_points )

Anomalies - Drops  = CALCULATE ( [Anomaly Count], detected_anomalies[direction] = "drop" )
Anomalies - Spikes = CALCULATE ( [Anomaly Count], detected_anomalies[direction] = "spike" )
```

`DISTINCTCOUNT` on `anomaly_key` rather than `COUNTROWS`, because `detected_anomalies` is one
row per incident but a date-sliced visual can double-count an incident spanning a period
boundary if the relationship is on `start_date` and the filter is on a month edge.

Totals today: **44 episodes** (26 drops, 18 spikes), spanning 2024-04-13 to 2025-10-05.

### Severity — a definition this document is inventing

**There is no `severity` column in the warehouse.** The detector emits `peak_z_score`,
`min_q_value` and `total_revenue_delta_usd`; "severity" is a presentation-layer banding, and
stating that is the whole point of this section. The thresholds below are anchored to Day 5's
actual control limits rather than chosen for roundness:

- **|z| ≥ 3** is the ordinary-day control limit.
- **|z| ≥ 4.8** is the widened limit the detector applies on holidays, where measured p95 of |z|
  is 3.45 versus 2.11 on ordinary days. It is the strictest threshold the detector uses anywhere,
  so it is a meaningful boundary rather than an arbitrary one.
- **|z| ≥ 8** is well beyond anything the detector treats as borderline.

```dax
Severity Band =
VAR Z = ABS ( MAX ( detected_anomalies[peak_z_score] ) )
RETURN
SWITCH (
    TRUE (),
    ISBLANK ( Z ), BLANK (),
    Z >= 8,   "1 - Critical",
    Z >= 4.8, "2 - High",
    Z >= 3,   "3 - Moderate",
    "4 - Low"
)
```

For a bar chart *by* severity you need it as a **calculated column** on `detected_anomalies`, not
a measure, so it can sit on an axis:

```dax
Severity =
VAR Z = ABS ( detected_anomalies[peak_z_score] )
RETURN
SWITCH ( TRUE (), Z >= 8, "1 - Critical", Z >= 4.8, "2 - High", Z >= 3, "3 - Moderate", "4 - Low" )
```

Measured distribution across the 44 episodes:

| Band | Episodes | Absolute revenue delta |
|---|---|---|
| 1 - Critical (\|z\| ≥ 8) | 8 | $83,719 |
| 2 - High (4.8 ≤ \|z\| < 8) | 16 | $85,189 |
| 3 - Moderate (3 ≤ \|z\| < 4.8) | 17 | $31,669 |
| 4 - Low (\|z\| < 3) | 3 | $1,003 |

**Read that table before trusting severity as a ranking.** "Critical" and "High" carry almost
identical total dollar impact ($83.7k vs $85.2k) despite a twofold difference in z. That is
expected and it is the reason severity must never be the only lens: **z measures how far from
normal, not how much money.** A tiny cell can post a huge z on a trivial dollar move.

Two companions, so the dashboard never shows statistical severity alone:

```dax
Revenue Impact = SUM ( detected_anomalies[total_revenue_delta_usd] )

Revenue Impact (Absolute) = SUMX ( detected_anomalies, ABS ( detected_anomalies[total_revenue_delta_usd] ) )
```

The absolute version exists because drops and spikes cancel: the net across all 44 episodes is
**+$33,443**, which would let a page of anomalies read as a *good* year. Absolute impact is
$201,580. Show both, or show absolute and split by direction — never net alone.

**Statistical confidence is a separate axis from severity**, and `min_q_value` is the honest
one — it is the Benjamini-Hochberg FDR q-value the detector confirmed at:

```dax
Min Q Value = MIN ( detected_anomalies[min_q_value] )

Confidence Band =
VAR Q = MIN ( detected_anomalies[min_q_value] )
RETURN
SWITCH ( TRUE (), ISBLANK ( Q ), BLANK (), Q < 0.0001, "Very high", Q < 0.001, "High", "Confirmed" )
```

Measured: 26 episodes at q < 0.0001, 5 at q < 0.001, 13 at q < 0.01. Note that the lowest peak
|z| in the whole set is 2.19 yet it still cleared FDR — because the detector confirms by pooling
over 1/2/3-day windows, so an episode can be statistically solid while its single worst day is
unremarkable. Ranking purely on `peak_z_score` would discard it.

### Per-day anomaly detail

```dax
Anomaly Actual Revenue   = SUM ( detected_anomaly_points[gross_revenue] )
Anomaly Expected Revenue = SUM ( detected_anomaly_points[expected_revenue] )
Anomaly Delta            = [Anomaly Actual Revenue] - [Anomaly Expected Revenue]

Peak Z Score = MAX ( detected_anomaly_points[z_score] )
Worst Z Score = MAXX ( detected_anomaly_points, ABS ( detected_anomaly_points[z_score] ) )
```

`Worst Z Score` uses `MAXX` over the absolute value because a drop's z is negative — `MAX` on a
page containing only drops returns the *least* severe one, which is precisely backwards.

---

## Stockouts

```dax
Stockout Rate % =
DIVIDE (
    SUM ( fct_daily_stockout[skus_out_of_stock] ),
    SUM ( fct_daily_stockout[skus_tracked] )
)

SKUs Out of Stock  = SUM ( fct_daily_stockout[skus_out_of_stock] )
SKUs Tracked       = SUM ( fct_daily_stockout[skus_tracked] )
Units On Hand      = SUM ( fct_daily_stockout[total_units_on_hand] )

Days With Stockout =
CALCULATE ( DISTINCTCOUNT ( fct_daily_stockout[snapshot_date] ), fct_daily_stockout[has_stockout] = TRUE )
```

**Do not use `AVERAGE ( fct_daily_stockout[stockout_rate_pct] )`.** That column is a correct
per-row percentage, and averaging it weights a category tracking 18 SKUs the same as one
tracking 32. The `DIVIDE(SUM, SUM)` form above recomputes the true rate at whatever grain the
visual asks for. This is the same error as averaging margin %, and it is easier to make here
because the column is *already called* a rate.

### The channel trap — the one modelling issue on this page

`fct_daily_stockout` has grain **date × category × region**. There is no channel column, and
that is deliberate: stock is physical. A pallet in the West distribution centre is not "web
stock" or "marketplace stock".

The consequence in Power BI is not obvious and will mislead someone: **a channel slicer will
appear to filter a stockout visual and will not.** Because `dim_channel` has no relationship to
`fct_daily_stockout`, selecting *Mobile App* filters revenue and margin while the stockout
numbers stay exactly as they were. The visual gives no hint that one of the slicers on the page
did not reach it.

The answer is not to fake a relationship — inventing a channel grain for physical stock would be
a fabrication. It is to say so on the visual:

```dax
Stockout Scope Note =
IF (
    ISFILTERED ( dim_channel[channel] ),
    "Stock is physical and not channel-specific — the channel filter does not apply here",
    BLANK ()
)
```

Same pattern as `Spend Allocation Note`: visible exactly when the caveat is live, invisible
otherwise.

---

## Measure summary

| Measure | Returns | Assumption it carries |
|---|---|---|
| `Total Revenue` | Sum of `gross_revenue` | Gross, not net — no returns or refunds exist in source |
| `Total Units` / `Total Orders` | Volume | — |
| `Average Order Value` | Revenue ÷ orders | — |
| `Revenue per Unit` | Revenue ÷ units | Falls during discounting — the promotion fingerprint |
| `Revenue % of Total` | Share within current date filter | Denominator escapes dimension filters, not the date filter |
| `Marketing Spend` | Sum of allocated spend | **Allocated to region by trailing 28-day revenue share** |
| `Return on Ad Spend` | Revenue ÷ spend | Inherits the allocation assumption |
| `Spend Allocation Note` | Caveat text | Appears only when region is filtered |
| `Estimated Gross Margin` | Sum of margin | Category-average unit cost over costed SKUs |
| `Estimated Gross Margin %` | Weighted margin ratio | Never an average of the pct column |
| `Cost Basis Coverage %` | Weighted SKU coverage | 88.9%–96.9%; complete nowhere |
| `SKUs Excluded from Cost Basis` | Count of uncosted SKUs | 8 of 120 |
| `Margin Caveat` | Caveat text | Names the actual coverage figure |
| `Anomaly Count` | Distinct incidents | 44 today |
| `Severity` (column) | Critical/High/Moderate/Low | **Defined here, not in the warehouse.** Measures distance from normal, not money |
| `Confidence Band` | Statistical confidence | From FDR q-value — a different axis from severity |
| `Revenue Impact` / `(Absolute)` | Dollar impact | Net cancels drops against spikes; prefer absolute or split |
| `Worst Z Score` | Largest \|z\| | `MAXX` over absolute — `MAX` inverts on drops |
| `Stockout Rate %` | Weighted stockout rate | Never an average of `stockout_rate_pct` |
| `Days With Stockout` | Distinct days flagged | — |
| `Stockout Scope Note` | Caveat text | Channel filter does not reach this fact |
