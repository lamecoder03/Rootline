# Day 5 — Anomaly detection: method, design choices, and validation results

The detector watches 60 revenue series independently and reports the days where one of them
moved unlike its own history *and* unlike the rest of the business on that same day.

Code is in `detection/`. This document explains the method, the two design decisions that
mattered, and the full validation against `docs/ground_truth_anomalies.md` — including what it
gets wrong.

---

## Headline results

| | Result |
|---|---|
| Anomalies detected | **3 of 3** |
| Worst detection lag | **4 days** (vs the 3-7 days a human takes) |
| Best detection lag | **0 days** — ANOM-01 flagged on the day it started |
| False-positive rate outside the injected windows | **0.09%** of cell-days |
| Black Friday / Cyber Monday / Christmas Eve | **not flagged** |
| Christmas Day | 1 of 120 cell-days flagged — analysed below |
| Episode precision | 70.5% (31 real, 13 false, over two years) |

The business case in CLAUDE.md is that a Revenue Ops lead currently learns about a dip three to
seven days late. The worst case here is four days, and two of the three fire within one day.

---

## The problem the method has to solve

Revenue is recorded per date x category x channel x region — 60 cells, 731 days.

**Detection runs on all 60 cells independently, never on a total.** ANOM-02 touches 3 of 60
cells. Aggregated to a daily company total, a 52% drop in 3 cells is a ~2.6% dip in the total —
indistinguishable from noise. The narrow event is only visible at the grain it happened at.

Three things stand between a raw rolling average and a usable detector:

1. **Weekly seasonality.** `WEEKDAY_FACTOR` swings from 0.92 (Tuesday) to 1.15 (Saturday), and
   each channel amplifies it differently. A rolling mean over raw daily values reads every
   Monday as a ~20% shortfall against a Saturday-inflated average, and flags it.
2. **Yearly seasonality.** A Gaussian Q4 bump peaking 8 December adds up to +45%, and a sine
   wave moves the spring/summer level. Any trailing baseline lags a moving curve and reads the
   lag as an anomaly.
3. **Holidays.** Christmas Day runs at 0.35x a normal day before category sensitivity — the
   deepest single-day drop in the entire series is a calendar effect, not an incident.

---

## The method, stage by stage

Everything happens in **log space**, because the generating process is multiplicative
(trend x weekday x season x holiday) and its noise is lognormal. Taking logs turns that product
into a sum and makes the spread constant across the level, so one dispersion estimate is valid
for a large cell and a small one alike.

### Stage 1 — Same-weekday baseline, with a deliberate gap

Each day is compared against the **median of the same weekday over the previous 8 weeks**,
skipping the most recent week.

- **Same weekday, not a rolling daily window.** This is the direct answer to weekly seasonality:
  a Monday is only ever compared against Mondays, so the weekly shape cancels instead of being
  averaged into a misleading mean.
- **8 weeks** balances two failures. Too short and the dispersion estimate is unusable; too long
  and the baseline no longer reflects the current level, since categories grow 5-22% a year.
- **The one-week gap is the important part.** It puts the nearest baseline observation 14 days
  back. ANOM-03 runs for 14 days, so even on its final day, every observation in its own
  baseline predates the event. Without that gap a two-week decay slowly becomes its own normal
  and the detector goes quiet exactly when the problem is worst.
- **Holidays are excluded from the reference set.** A baseline is supposed to describe a normal
  day, and a holiday is by definition not one.

### Stage 2 — Remove the common calendar factor

All 60 cells share one calendar. If every cell moves together, that is the calendar, not an
incident. So for each day the **cross-sectional median residual across all 60 cells** is
subtracted, leaving only what each cell did *differently from the rest of the business*.

This is what handles yearly seasonality, and it does it without ever naming a season. The Q4
ramp lifts all 60 cells, so the common factor absorbs it entirely. A median tolerates up to half
the cells being genuinely affected, which is why ANOM-03 — 20 of 60 cells — still survives it.

**Measured effect:** false positives in November-December fell from **9.52% to 0.92%** of
cell-days, a 10x reduction, while ANOM-01 and ANOM-02 detection *improved*.

**The cost, stated plainly:** the broader an event, the more of it this removes. ANOM-03's
within-window recall fell from 44.6% to 24.6%, because a fifth of the business moving together
partly looks like a common factor. It is still detected, four days in, which is the trade that
was worth making — the alternative was a detector that fired continuously through every Q4.

### Stage 3 — Scale by recent forecast error, not by baseline spread

The z-score divides by the **spread of that cell's recent residuals** — the previous 56 days,
excluding the last 7, holidays removed — using median and MAD.

The subtlety here caused the first version of this detector to fail, so it is worth being
precise. The question is not "how much do the baseline values vary?" but **"how large is
today's miss compared with how large misses usually are?"** Those are different quantities: the
second includes the uncertainty in the baseline estimate itself. Scaling by the first produced
z-scores where **13.68% of all points exceeded |z| > 3**, against the 0.27% a normal
distribution predicts.

Pooling residuals across weekdays is legitimate because log-space noise is homoscedastic, and it
buys ~56 observations instead of 8 — a far steadier scale estimate. Median and MAD are used
rather than mean and standard deviation so that a live anomaly sitting inside the trailing
window cannot inflate the very scale meant to reveal it.

### Stage 4 — Calibrate the null empirically

Even after stage 3 the z-scores come out mildly over-dispersed — a robust scale from a finite
sample does not perfectly match a normal. Rather than assume the textbook null, the null is
**measured from the data**: z is rescaled by the robust spread of the whole z distribution
(Efron's empirical null). The measured factor is 1.047, so the correction is small — but without
it the p-values in stage 5 would be quietly optimistic, and every downstream significance claim
would inherit that optimism.

### Stage 5 — A crossing is a candidate, not a finding

This is the hypothesis-test step. A point that clears the control limit is then **confirmed** by
pooling its residual over trailing windows of 1, 2 and 3 days.

The logic: a real shift persists, so pooling k days multiplies its signal by the square root of
k. An isolated noise spike does not persist, so pooling averages it away. The strongest of the
three windows is taken, and **Bonferroni-corrected by 3** for having looked three ways.

The pooled statistic is converted to a p-value using the **t-distribution**, not the normal,
with degrees of freedom from the scale estimate. With a baseline this small the difference is
not academic: at |z| = 3 the normal gives p = 0.0027 while t(7) gives p = 0.0199, seven times
larger. Using the normal would make the detector overconfident in exactly the tail where every
decision gets made.

Finally, **Benjamini-Hochberg false discovery rate control at q < 0.01** across all 38,460
tests. This is not optional at this scale: an uncorrected p < 0.003 threshold would produce
about 118 findings from pure chance. FDR is the right guarantee here rather than family-wise
error — a handful of wrong flags is tolerable, a detector nobody trusts is not.

A day is reported only if it clears **both** the control limit and the FDR test.

---

## Holidays: expected-variance days, not excluded days

The brief was explicit that a blind "skip if holiday" would hide the problem rather than solve
it — and it would also mean a genuine incident on Black Friday, the largest revenue day of the
year, could never be detected. Two mechanisms are used instead, and neither is a skip.

**1. Holidays are removed from baselines, not from scoring.** A holiday never enters a
reference set or a dispersion window, because those are meant to describe normal days. But every
holiday is itself scored.

**2. The control limit widens on holidays: |z| >= 4.8 instead of 3.0.** A holiday is a day where
a large move is *expected*, so the bar for calling it an incident is raised — not removed. The
1.6x multiplier is not arbitrary: measured across the series, |z| on holidays runs to a 95th
percentile of 3.45 against 2.11 on ordinary days, so 1.6x restores comparable specificity while
leaving a genuinely extreme holiday still detectable.

### The bug this exposed, and the fix

The first version applied one cross-sectional median across all 60 cells on every day, including
holidays. **Christmas failed badly: 30 of 120 cell-days flagged, peak |z| of 17.99.**

The cause is that holiday response is category-specific. Christmas Day at 0.35x base, scaled by
each category's sensitivity, works out as:

| Category | Sensitivity | Effective Christmas multiplier |
|---|---|---|
| Electronics | 1.35 | **0.12** (-87.8%) |
| Apparel | 1.10 | 0.29 (-71.5%) |
| Beauty | 1.00 | 0.35 (-65.0%) |
| Sports | 0.85 | 0.45 (-55.2%) |
| Home & Garden | 0.75 | **0.51** (-48.8%) |

Electronics falls four times further than Home & Garden. One global median cannot represent both
ends — whichever it lands near, the other end looks like a catastrophe.

**The fix: on holidays, the peer group narrows to the cell's own category.** The common factor
should be estimated across cells that share the same expected calendar response, and on a
holiday that group is the category, not the whole business. This is ordinary retail domain
knowledge — Electronics lives or dies on Black Friday, garden furniture barely notices — not
something reverse-engineered from the generator.

**Result: Christmas Day went from 30 flagged cell-days to 1, and peak |z| from 17.99 to 5.08.**
Black Friday, Cyber Monday and Christmas Eve went to zero flags.

**The trade-off, stated:** a category-wide anomaly landing exactly on a holiday would now be
absorbed by its own peer group and missed. That is a real blind spot covering 30 of 731 days.
It is accepted deliberately, because the alternative is a detector that reports Christmas as an
incident every single year and is therefore switched off by its users in January.

---

## Where detection output goes

Two tables in `analytics`, replaced in full on every run.

**`analytics.detected_anomalies`** — one row per incident. Consecutive flagged days in one cell
collapse into a single row, because a seven-day stockout is one thing that happened, not seven.
Carries the slice, the window, direction, peak date, peak z, peak delta %, total revenue delta
in USD, and the minimum q-value. **This is the table the Day 8 agent investigates.**

**`analytics.detected_anomaly_points`** — one row per flagged day, joined to the episode by
`anomaly_key`. The per-day evidence behind each incident: the observed and expected revenue, the
z-score, which confirmation window fired, and the p and q values.

Three choices worth defending:

- **`analytics`, not a new schema.** The CLAUDE.md guardrail grants the agent read-only access to
  the analytics schema alone. Detection output is something the agent must read, so it belongs
  where that grant already reaches. dbt only manages its own models and will not touch these.
- **Replace in full, not append.** The detector is deterministic over a fixed window, so
  re-running must converge on one answer rather than accumulate duplicates every time the Day 6
  DAG fires. `detection_run_id` and `detected_at` record which run produced the current rows.
- **Only flagged points are stored,** not all 38,460 scores. The unflagged scores are a
  diagnostic the validation harness recomputes on demand; persisting them would bloat the table
  the agent queries without telling it anything it needs.

---

## Validation results

Produced by `python -m detection.validate`, scored against `docs/ground_truth_anomalies.csv`
and the decoy list. The harness reports every check it made, because silence on a decoy is only
evidence if somebody checked that it was silent.

### The three injected anomalies — all detected

| ID | Difficulty | Window | First fired | **Lag** | Cells found | Recall in window | Peak abs z | Direction |
|---|---|---|---|---|---|---|---|---|
| ANOM-01 | easy | 03-14 → 03-17 | 2025-03-14 | **0 days** | 12 of 12 | 95.8% | 12.81 | spike |
| ANOM-02 | medium | 06-09 → 06-15 | 2025-06-10 | **1 day** | 3 of 3 | 85.7% | 17.72 | drop |
| ANOM-03 | hard | 09-22 → 10-05 | 2025-09-26 | **4 days** | 14 of 20 | 24.6% | 7.01 | drop |

Reading these properly:

**ANOM-01** fires on day one, in all twelve affected cells, correctly signed as a spike. A
doubling of revenue is the easy case and the detector treats it as such.

**ANOM-02** is the one that justifies running per cell. It is confined to 3 cells of 60 and is
invisible in any total, yet all 3 are found and it carries the highest peak z in the dataset
(17.72). One day of lag on a 7-day event leaves six days to act.

**ANOM-03 is the honest one.** 24.6% within-window recall looks poor until you see what it
measures: the event opens at -6%, which *should not* be detectable, because a 6% move sits well
inside normal daily variation and flagging it would mean flagging everything. The detector stays
silent for three days and fires on the fourth, when the drop reaches roughly -20%. Recall is low
because the denominator includes all 280 cell-days in the window, most of them early and shallow.
**The number that matters for the business case is the lag, and it is 4 days.**

The task said to stop and report if the z-score approach failed on ANOM-03. It did not fail: the
event is detected, correctly signed, in 14 of 20 affected cells, within the human baseline. No
STL decomposition was written.

### The decoys — every one checked and reported

| Decoy | Cell-days checked | Flagged | Max abs z | Verdict |
|---|---|---|---|---|
| Black Friday (2024-11-29, 2025-11-28) | 120 | **0** | 3.50 | PASS |
| Cyber Monday (2024-12-02, 2025-12-01) | 120 | **0** | 3.89 | PASS |
| Christmas Eve (2024-12-24, 2025-12-24) | 120 | **0** | 2.94 | PASS |
| Christmas Day (2024-12-25, 2025-12-25) | 120 | **1** | 5.08 | 1 flag — see below |
| Q4 ramp (1 Nov – 20 Dec, both years) | 5,700 | **2** | 4.33 | 0.04% of the window |
| Monthly budget steps (1st of month) | 1,260 | 13 | 5.53 | 1.03% vs 0.41% on other days |
| All other quiet days | 36,960 | 32 | 5.77 | **0.09% false-positive rate** |

**The four biggest calendar decoys are clean or near-clean.** Black Friday and Cyber Monday are
the two largest revenue days in the series, up to 2.6x a normal day, and neither produces a
single flag in either year. Christmas Day, the deepest drop in the series, produces one flag out
of 120.

**That one Christmas flag, examined rather than excused.** It is
`Beauty | Mobile App | North` on 2024-12-25, which fell 25.5% *relative to the other Beauty
cells that same day* — while the rest of the Beauty category ranged from -12.6% to +17.9%. So it
is not the detector misreading the Christmas calendar effect; that has been removed correctly.
It is one genuinely idiosyncratic cell. The likely mechanical cause is that this is a small cell
on a very low-volume day: 489 USD of revenue, where the log-space constant-variance assumption
holds least well. With 38,460 simultaneous tests at a 1% FDR, a handful of such flags is
expected and is what FDR control explicitly budgets for.

**Monthly budget steps show a mild real effect** — 1.03% flag rate on the 1st versus 0.41%
otherwise. Spend genuinely steps at month boundaries by design, and some of that reaches revenue.
This is reported rather than tuned away: it is a 2.5x elevation on a small base, and suppressing
it would mean blinding the detector to anything that genuinely happens on the 1st of a month.

### Episode-level precision

Over two years and 60 cells the detector produced **44 episodes: 31 matching a real injected
anomaly, 13 not.** Episode precision is 70.5%.

The 13 false episodes have a median length of **1 day**, and 10 of the 13 are single-day. They
are scattered — the worst month has 3 — with no seasonal cluster, which is the signature of
ordinary statistical noise rather than a systematic blind spot. That is the expected shape: the
FDR budget is 1%, and single-day scattered flags are exactly what that budget buys.

For contrast, the version before the holiday peer-group fix produced 47 false episodes, 31 of
them in December alone. That clustering was the tell that something was structurally wrong; its
absence now is the evidence that it is fixed.

---

## Known limitations

Recorded because they are real, and because the next person to touch this needs them.

1. **Broad events are attenuated by design.** The cross-sectional common factor removes anything
   affecting a large fraction of cells simultaneously. ANOM-03 (20 of 60 cells) loses roughly
   half its within-window recall to this. An event touching most of the business at once would be
   substantially suppressed. A slice-level detector running alongside the cell-level one would
   cover that gap, and is the obvious next increment.
2. **A category-wide anomaly on a holiday would be missed**, for the reason given above. 30 of
   731 days.
3. **Episode end dates run up to 2 days long.** The 3-day confirmation window keeps firing
   briefly after an event ends, so reported windows can extend past the true end. The start date
   and the lag — the numbers that matter operationally — are unaffected.
4. **Small, low-volume cells are noisier than the model assumes.** The one Christmas false
   positive is an instance. Weighting the dispersion estimate by volume would likely fix it.
5. **BH-FDR assumes independence or positive dependence** between tests. Revenue series are
   autocorrelated and cross-correlated, so the true FDR may differ from the nominal 1%. The
   empirical decoy results are the real evidence of specificity, not the nominal q.

---

## Running it

```
python -m detection.run_detection    # score, group into episodes, write both tables
python -m detection.validate         # score against ground truth and the decoy list
```

Both run from the generator venv (`.venv`, Python 3.14), which is where scipy is installed.
`detection/config.py` holds every tunable constant with the reason it holds that value, so the
method can be reviewed without reading the algorithm.
