# Ground truth — injected anomalies

The answer key for the synthetic dataset. Day 5's detection logic is scored against this
file, so every figure here is exact rather than descriptive.

**This file is never loaded into Postgres.** The warehouse holds only the blind series;
the counterfactual lives here and in `docs/ground_truth_anomalies.csv`.

## How these numbers are exact

Each cell is generated as `baseline × injected_multiplier`, where the baseline is the
full seasonal signal *including its noise draw*. Baseline and actual therefore share the
same random draw, so the difference between them is purely the injected event. That is
why every `delta_pct` below lands exactly on `(multiplier − 1) × 100` with no residual.

Reproducibility: `SEED = 42` in [generators/config.py](../generators/config.py), series
range 2024-01-01 to 2025-12-31 (731 days), grain `category × channel × region` (60 cells).
Regenerating with the same seed reproduces these figures to the cent. Changing the seed
invalidates every USD figure below — the dates, slices, and multipliers are fixed
constants and would survive, but the doc would need re-deriving from the regenerated CSV.

## Summary

| ID | Window | Days | Cause | Slice | Cells | Baseline USD | Actual USD | Delta USD | Delta % |
|---|---|---|---|---|---|---|---|---|---|
| ANOM-01 | 2025-03-14 → 2025-03-17 | 4 | Promotion | `category = Apparel` | 12 | 122,498.83 | 241,671.96 | **+119,173.13** | **+97.29%** |
| ANOM-02 | 2025-06-09 → 2025-06-15 | 7 | Inventory stockout | `category = Electronics`<br>`AND region = West` | 3 | 92,488.82 | 44,339.44 | **−48,149.38** | **−52.06%** |
| ANOM-03 | 2025-09-22 → 2025-10-05 | 14 | Marketing spend cut | `channel = Mobile App` | 20 | 710,218.97 | 547,501.35 | **−162,717.62** | **−22.91%** |

---

## ANOM-01 — Spring Style Event promotion

**Cause family:** `promotion` · **Detection difficulty:** easy

A four-day sitewide Apparel promotion running Friday to Monday. Revenue roughly doubles
across all 3 channels and all 4 regions of the Apparel category.

**Affected slice:** `category = 'Apparel'` — all channels, all regions (12 of 60 cells).

| Date | Weekday | Multiplier | Baseline USD | Actual USD | Delta USD | Delta % |
|---|---|---|---|---|---|---|
| 2025-03-14 | Fri | 1.95 | 30,956.29 | 60,364.75 | +29,408.46 | +95.00% |
| 2025-03-15 | Sat | **2.15** | 32,516.67 | 69,910.85 | +37,394.18 | **+115.00%** |
| 2025-03-16 | Sun | 2.05 | 31,578.17 | 64,735.26 | +33,157.09 | +105.00% |
| 2025-03-17 | Mon | 1.70 | 27,447.70 | 46,661.10 | +19,213.40 | +70.00% |

**Peak day:** 2025-03-15 at +115.00%.

**Corroborating evidence the investigator should find:**

- `raw.marketing_spend` — Apparel spend is multiplied by **2.60** across all channels from
  **2025-03-12 to 2025-03-17**, i.e. the media surge *leads* the revenue spike by 2 days.
- `raw.daily_revenue` — average order value drops ~20% during the window (the generator
  applies a 0.80 AOV factor), so **units rise faster than revenue**. Observed Apparel
  units go 547 (03-13) → 1,423 (03-15), a 2.6× lift against a 2.4× revenue lift. This is
  the fingerprint that distinguishes a discount-driven spike from a demand-driven one.
- `raw.inventory_snapshot` — no stockout signal. Inventory is not the explanation.

**A correct detection reports:** a positive anomaly on Apparel spanning 2025-03-14 to
2025-03-17, attributed to promotional/marketing activity, not to a demand shift.

---

## ANOM-02 — West-region Electronics stockout

**Cause family:** `inventory` · **Detection difficulty:** medium

A delayed supplier shipment empties the West distribution centre of its top Electronics
SKUs. The narrowest of the three events — only 3 of 60 cells — so it is easy to miss at
an aggregated grain and only obvious once the series is cut by category *and* region.

**Affected slice:** `category = 'Electronics' AND region = 'West'` — all 3 channels
(3 of 60 cells).

| Date | Weekday | Multiplier | Baseline USD | Actual USD | Delta USD | Delta % |
|---|---|---|---|---|---|---|
| 2025-06-09 | Mon | 0.72 | 12,796.83 | 9,213.72 | −3,583.11 | −28.00% |
| 2025-06-10 | Tue | 0.48 | 11,320.52 | 5,433.86 | −5,886.66 | −52.00% |
| 2025-06-11 | Wed | 0.38 | 13,468.20 | 5,117.92 | −8,350.28 | −62.00% |
| 2025-06-12 | Thu | **0.35** | 12,514.69 | 4,380.14 | −8,134.55 | **−65.00%** |
| 2025-06-13 | Fri | 0.35 | 12,685.14 | 4,439.80 | −8,245.34 | −65.00% |
| 2025-06-14 | Sat | 0.41 | 15,401.11 | 6,314.45 | −9,086.66 | −59.00% |
| 2025-06-15 | Sun | 0.66 | 14,302.33 | 9,439.55 | −4,862.78 | −34.00% |

**Peak day:** 2025-06-12 at −65.00% (tied on multiplier with 06-13; 06-12 is the larger
percentage-loss day by a rounding margin and 06-14 is the largest dollar-loss day at
−9,086.66).

**Corroborating evidence the investigator should find:**

- `raw.inventory_snapshot` — exactly **6 SKUs** hit `units_on_hand = 0` on exactly the
  7 event days, and zero on every surrounding day: `ELEC-0004, ELEC-0008, ELEC-0012,
  ELEC-0020, ELEC-0024, ELEC-0028`, all with `region = 'West'`.
- Stock begins depleting **2025-06-06**, three days before revenue reacts, and ramps back
  from 2025-06-16 to 2025-06-19.

**This is the negative control for spend-based reasoning.** `raw.marketing_spend` is
deliberately left untouched across this window — there is no spend override for ANOM-02.
An investigator that explains every dip by pointing at marketing spend will get this one
wrong, which is precisely what it is here to test.

**A correct detection reports:** a negative anomaly confined to Electronics × West
spanning 2025-06-09 to 2025-06-15, attributed to inventory availability, explicitly
*ruling out* marketing spend.

---

## ANOM-03 — Mobile App acquisition budget cut

**Cause family:** `marketing_spend` · **Detection difficulty:** hard

Paid user-acquisition spend on the Mobile App channel is cut to 30% of plan. Revenue does
not step down — it decays gradually over two weeks and partially recovers. This is the
hardest of the three: on any single day early in the window the drop sits inside normal
daily variation, and a rolling baseline will start absorbing the new lower level the
longer the event runs.

**Affected slice:** `channel = 'Mobile App'` — all categories, all regions
(20 of 60 cells).

| Date | Weekday | Multiplier | Baseline USD | Actual USD | Delta USD | Delta % |
|---|---|---|---|---|---|---|
| 2025-09-22 | Mon | 0.94 | 45,724.12 | 42,980.70 | −2,743.42 | −6.00% |
| 2025-09-23 | Tue | 0.89 | 44,917.26 | 39,976.37 | −4,940.89 | −11.00% |
| 2025-09-24 | Wed | 0.84 | 46,412.96 | 38,986.90 | −7,426.06 | −16.00% |
| 2025-09-25 | Thu | 0.80 | 49,673.97 | 39,739.19 | −9,934.78 | −20.00% |
| 2025-09-26 | Fri | 0.77 | 54,387.76 | 41,878.58 | −12,509.18 | −23.00% |
| 2025-09-27 | Sat | 0.74 | 59,647.97 | 44,139.50 | −15,508.47 | −26.00% |
| 2025-09-28 | Sun | 0.72 | 55,982.06 | 40,307.09 | −15,674.97 | −28.00% |
| 2025-09-29 | Mon | **0.70** | 46,874.16 | 32,811.93 | −14,062.23 | **−30.00%** |
| 2025-09-30 | Tue | **0.70** | 44,012.89 | 30,809.02 | −13,203.87 | **−30.00%** |
| 2025-10-01 | Wed | 0.71 | 45,542.61 | 32,335.27 | −13,207.34 | −29.00% |
| 2025-10-02 | Thu | 0.72 | 49,140.22 | 35,380.95 | −13,759.27 | −28.00% |
| 2025-10-03 | Fri | 0.74 | 55,467.17 | 41,045.68 | −14,421.49 | −26.00% |
| 2025-10-04 | Sat | 0.76 | 57,138.37 | 43,425.16 | −13,713.21 | −24.00% |
| 2025-10-05 | Sun | 0.79 | 55,297.45 | 43,685.01 | −11,612.44 | −21.00% |

**Trough:** 2025-09-29 and 2025-09-30, both at −30.00%. Largest single-day dollar loss is
2025-09-28 at −15,674.97.

**Corroborating evidence the investigator should find:**

- `raw.marketing_spend` — Mobile App spend across **all categories** is multiplied by
  **0.30** from **2025-09-17 to 2025-10-05**. Observed daily spend falls from ~6,550 USD
  on 09-16 to ~2,191 USD on 09-17 and stays there.
- **The cause leads the effect by 5 days.** Spend is cut 2025-09-17; revenue does not
  visibly break trend until 2025-09-22. A same-day correlation will understate the link —
  the lag is the point of this case.
- Spend restores to normal on 2025-10-06, and revenue is back to baseline from 2025-10-06.

**A correct detection reports:** a negative anomaly on the Mobile App channel spanning
2025-09-22 to 2025-10-05, attributed to the paid-acquisition budget cut that began
2025-09-17, and correctly identifies the lag rather than treating the drop as sudden.

---

## Decoys — things that must NOT be flagged as incidents

The dataset contains several large, entirely legitimate movements. A detector that fires
on these is producing false positives, and the ratio matters as much as the hit rate.

1. **Black Friday / Cyber Monday** — 2024-11-29, 2024-12-02, 2025-11-28, 2025-12-01.
   The largest revenue days in the whole series, up to 2.6× a normal day before category
   sensitivity is applied. Electronics reacts hardest (sensitivity 1.35).
2. **Christmas Day** — 2024-12-25, 2025-12-25, at 0.35× a normal day. The deepest single-day
   drop in the series is a calendar effect, not an incident. Christmas Eve is 0.55×.
3. **The Q4 ramp** — a Gaussian bump peaking around 8 December each year, up to +45%.
   Sustained and seasonal, not an anomaly.
4. **Incidental stockouts** — 2,200 SKU-days sit at `units_on_hand = 0` outside the ANOM-02
   window, caused by ordinary lead-time variance and a 2% late-shipment rate. Finding "a
   stockout" is not evidence; finding *six SKUs in one category and region simultaneously
   out for seven consecutive days* is.
5. **Monthly marketing budget steps** — spend shifts at calendar-month boundaries by design.
   Spend/revenue correlation is 0.86–0.89 by channel, deliberately imperfect, so a
   correlation alone does not establish causation.

## Machine-readable version

`docs/ground_truth_anomalies.csv` — one row per anomaly per day, regenerated by
`generators/gen_revenue.py` on every run. Columns: `anomaly_id`, `order_date`,
`injected_multiplier`, `affected_cells`, `baseline_revenue_usd`, `actual_revenue_usd`,
`delta_usd`, `delta_pct`.
