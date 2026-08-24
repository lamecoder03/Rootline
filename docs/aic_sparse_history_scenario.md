# Sparse-History Scenario — A Newly Launched Category

**The problem:** a category launched three weeks ago has no baseline. A detector that scores it
anyway invents a normal from too little data and fires on noise; a detector that drops it
silently reports nothing — which a reader interprets as *nothing is wrong*.

**What this scenario proves:** the detector recognises insufficient history, refuses to score
the cell, and **says so in a queryable table** rather than staying quiet.

---

## The setup

`generators/gen_sparse_category.py` appends one new cell to `raw.daily_revenue`:

| Property | Value |
|---|---|
| Cell | `Wearables \| Web \| North` |
| History | **21 days** (2025-12-11 → 2025-12-31) vs 731 for every other cell |
| Rows | 21 |
| Revenue | $93,133.49 |
| Anomaly injected | **None, deliberately** |

**Additive, not a regeneration.** The `SEED = 42` series is untouched, so all 44 existing
episodes stay reproducible. The new data uses a separate seed (`4242`) precisely so it cannot
imply a reproducibility relationship with the main series.

**No anomaly is injected on purpose.** The question under test is whether the engine admits it
cannot judge. An injected event would confuse *"abstained correctly"* with *"missed something"*.

```bash
python -m generators.gen_sparse_category            # add it
python -m generators.gen_sparse_category --remove   # remove it
```

---

## Why 21 days is not enough — the gates it fails

The detector requires history at two independent stages. Both are pre-existing constants in
`detection/config.py`; neither was invented for this scenario.

| Gate | Constant | Requires | Wearables has | Verdict |
|---|---|---|---|---|
| Stage 1 baseline | `MIN_BASELINE_OBSERVATIONS` | 6 same-weekday observations, each ≥14 days back | 8 | passes |
| Stage 3 dispersion | `MIN_RESIDUAL_OBSERVATIONS` | 30 residual days in a 56-day window | **0** | **fails — binding constraint** |

**Measured, and not what was assumed.** The baseline stage is satisfied: the same-weekday lookup
finds 8 candidate positions, because the offsets reach back beyond the cell's launch date. What
the cell cannot supply is **dispersion** — scaling a residual requires 30 prior residuals from
the same cell, and a 21-day-old cell has none. `scale_n = 0` on all 21 days.

This is the more interesting failure, and the reason the engine reports the *binding* constraint
rather than a generic "not enough data": a cell can clear one history gate and still be
unscoreable, and a consumer needs to know which requirement actually blocked it.

All 21 days are therefore classified `insufficient_residual_history`, not
`insufficient_baseline_history`.

---

## What the engine actually reports

Real output from `python -m detection.run_detection`:

```
  scored 38,460 cell-days across 60 cells
  empirical null: centre +0.004, spread 1.047
  flagged 166 cell-days (0.43%) in 44 episodes
  1 cell(s) below the history gates and NOT judgeable:
    Wearables | Web | North                  21 days observed,   0.0% scored, confidence=none
Wrote analytics.detected_anomalies           44 rows
Wrote analytics.detected_anomaly_points     166 rows
Wrote analytics.detection_coverage           61 rows
```

And in the warehouse, `analytics.detection_coverage`:

| cell_key | days_observed | days_scored | pct_scored | is_newly_launched | dominant_reason | confidence |
|---|---|---|---|---|---|---|
| Wearables \| Web \| North | 21 | 0 | 0.0 | true | insufficient_residual_history | **none** |

| confidence | cells |
|---|---|
| normal | 60 |
| **none** | **1** |

**Zero false positives on the new category.** It produced no episodes at all — not because
nothing happened, but because the engine states it cannot tell.

---

## The design decision: absence of evidence is not evidence of absence

Before this work, an unscoreable cell-day was removed by a single line — `dropna(subset=["z_raw"])`
in `detection/detector.py`. It was correct arithmetic and a **reporting failure**: the cell
vanished from the output, indistinguishable from a cell that had been examined and found normal.

`detection/coverage.py` classifies every dropped cell-day *before* the drop:

| Reason | Meaning |
|---|---|
| `insufficient_baseline_history` | Fewer than 6 usable same-weekday reference days |
| `insufficient_residual_history` | Fewer than 30 residual observations for the scale estimate |
| `degenerate_scale` | Scale below `MIN_SCALE` — a flat or near-constant series |

Rolled up per cell into three confidence levels:

| Confidence | Rule | Means |
|---|---|---|
| `normal` | ≥50% of observed days scored | Findings for this cell are trustworthy |
| `low` | 1–49% scored | Partial coverage; absence is weak evidence |
| **`none`** | **0 days scored** | **No anomaly can be ruled in *or* out** |

The table comment states the contract in the database itself:

> `confidence=none` means no anomaly can be ruled in OR out for that cell — absence of a
> detected anomaly is not evidence of normality.

The existing 60 cells are unaffected: all report `normal`. The 5,400 unscored cell-days from the
series' own warm-up period — previously invisible — are now explained too.

---

## Two real bugs this scenario exposed

Building the scenario found two latent defects that no existing test could reach. Both are fixed.

### 1. A single new cell silently put the whole series into holiday mode

`_pivot()` built the holiday matrix with `.values.astype(bool)`. A cell that did not exist on a
date leaves `NaN` there — and **`NaN` casts to `True`**. With one newly launched cell,
`is_holiday.any(axis=1)` returned `True` for *every* day in the series.

The detector therefore applied the **widened holiday control limit (|z| ≥ 4.8)** and the
narrowed category peer group to all 731 days. Measured effect:

| | Before fix | After fix |
|---|---|---|
| ANOM-01 peak \|z\| | 3.25 — **MISSED** | **12.81 — detected, 0-day lag** |
| Episodes | 62 | 44 |
| Episode precision | 43.5% | 70.5% |

Fixed with `.fillna(False)` before the cast. **This bug predates the scenario** — it would have
fired on the first genuinely new category in production, degrading detection across the entire
business while looking like it was working.

### 2. `fct_daily_margin` silently dropped the category

The margin mart used `inner join` to the category cost basis. A category with no costed SKUs
disappeared from the fact entirely — and a category missing from the margin table **reads as
zero margin rather than unknown margin**.

Now a `left join`, with `is_margin_estimable` coalesced to `false` rather than left NULL: a
category with no cost basis has a *known* answer to "can margin be estimated?", and that answer
is no. Caught by `assert_marts_do_not_inflate_revenue`, which compares margin row counts against
the revenue fact.

---

## The data contract was extended, not weakened

Adding a category with no SKUs and no marketing spend broke 7 dbt tests. **None was deleted or
relaxed.** Each was scoped to state the exemption explicitly, and two were made stronger:

| Test | Treatment |
|---|---|
| `accepted_values` on category (staging + marts) | `Wearables` added to the allowlist |
| `relationships` category → product master / `dim_product` | Scoped `where category <> 'Wearables'`, with the reason in the column description |
| `not_null` on `spend_allocation_basis`, `cost_basis_avg_unit_cost`, `cost_basis_coverage_pct` | Scoped — undefined for a category with no SKUs |
| `not_null` on `skus_excluded_from_cost_basis` | **Kept unconditional** — coalesced to 0 in the model instead |
| `assert_revenue_grain_is_complete` | **Strengthened** — now checks each cell is contiguous from its own first day, catching mid-series holes *and* cells that stop reporting |
| `assert_margin_exclusions_are_accounted` | **Strengthened** — new clause asserts a category with no cost basis must declare `is_margin_estimable = false` |

**191 of 191 dbt tests pass** with the sparse category present.

Every exemption names `Wearables` explicitly, so a *second* new category fails the build until
someone consciously extends the contract — which is the intended behaviour, not friction.

---

## How this maps to the brief

| Requirement | Evidence |
|---|---|
| Sparse-history / newly launched KPI scenario | 21-day category, 0% scored, `confidence=none` |
| Communicates uncertainty | Three-level confidence + machine-readable reason per cell-day |
| Abstains when evidence is insufficient | Zero episodes emitted; explicit refusal instead of a fabricated baseline |
| Evidence showing analytical method | `dominant_reason` names the binding constraint and the threshold it failed |

---

## Reproducing it

```bash
python -m generators.gen_sparse_category     # append the 21-day category
run_dbt.bat build                            # 191 tests
python -m detection.run_detection            # writes analytics.detection_coverage
python -m detection.validate                 # all 3 anomalies still detected, 70.5% precision
```

To remove it and return to the 60-cell baseline:

```bash
python -m generators.gen_sparse_category --remove
run_dbt.bat build && python -m detection.run_detection
```
