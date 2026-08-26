# Every tunable constant for the rolling z-score detector, in one place.
# Exists so the method can be described, reviewed and re-tuned without reading the algorithm,
# and so the Airflow DAG and the validation harness provably run the same configuration.
# Each value carries the reason it holds that number, because each one is a real trade-off.

# --- Stage 1: seasonal baseline -------------------------------------------------------
# Compare each day against the same weekday in prior weeks, never against raw daily values.
# WEEKDAY_FACTOR in generators/config.py swings Monday to Saturday by 0.92..1.15, so a naive
# rolling mean would read every Monday as a 20% shortfall against a Saturday-inflated average.
BASELINE_WEEKS = 8

# Whole weeks skipped between the evaluated day and the nearest baseline observation.
# 1 means the closest reference point is 14 days back, so a two-week event cannot pollute the
# baseline it is being measured against. This is what keeps ANOM-03 (14 days) detectable
# instead of being slowly absorbed into its own normal.
BASELINE_GAP_WEEKS = 1

# Below this many usable reference days the baseline is not trustworthy and the day is skipped
# rather than scored on thin evidence.
MIN_BASELINE_OBSERVATIONS = 6

# --- Stage 2: common calendar factor --------------------------------------------------
# All 60 cells share one calendar. Subtracting the cross-sectional median residual removes the
# movement every cell made together - the Q4 ramp, Black Friday, Christmas - and leaves only
# what a cell did differently from the rest of the business.
MIN_CELLS_FOR_COMMON_FACTOR = 30

# --- Stage 3: dispersion ---------------------------------------------------------------
# Scale is estimated from the recent distribution of FORECAST ERRORS, not from the spread of
# the baseline values. Pooling across weekdays is valid because log-space noise is
# homoscedastic, which buys ~56 observations instead of 8.
RESIDUAL_WINDOW_DAYS = 56
RESIDUAL_GAP_DAYS = 7
MIN_RESIDUAL_OBSERVATIONS = 30

# MAD -> sigma under normality. Robust so that a real anomaly sitting in the trailing window
# inflates the scale far less than a standard deviation would.
MAD_TO_SIGMA = 1.4826
MIN_SCALE = 1e-6

# --- Stage 4: thresholds ---------------------------------------------------------------
# Base control limit in calibrated sigma. 3.0 is the conventional control-chart limit.
Z_THRESHOLD = 3.0

# Holidays are expected-variance days, not excluded days. The bar is raised, not removed, so a
# genuine incident on Black Friday still fires. Measured: holiday |z| runs p95 3.45 against
# 2.11 on ordinary days, so 1.6x restores comparable specificity without blinding the detector.
HOLIDAY_THRESHOLD_MULTIPLIER = 1.6

# --- Stage 5: hypothesis test ----------------------------------------------------------
# A threshold crossing is a candidate, not a finding. Each candidate is confirmed by pooling
# its residual over trailing windows of these lengths; a sustained shift gains sqrt(k) while an
# isolated noise spike does not. The search over three window lengths is paid for by Bonferroni.
CONFIRMATION_WINDOWS = (1, 2, 3)

# Benjamini-Hochberg false discovery rate. At 43,860 points, an uncorrected p < 0.003 would
# yield ~118 findings by chance alone, so multiplicity has to be controlled explicitly.
FDR_Q = 0.01

# Cap on the degrees of freedom credited to the scale estimate, so a long clean run does not
# imply more precision than the robust estimator actually delivers.
MAX_SCALE_DF = 200

# --- Output ----------------------------------------------------------------------------
SOURCE_TABLE = "analytics.fct_daily_revenue"
POINTS_TABLE = "detected_anomaly_points"
EPISODES_TABLE = "detected_anomalies"
OUTPUT_SCHEMA = "analytics"

# Per-cell confidence report. Written alongside the two anomaly tables so a consumer can ask
# "is this cell judgeable yet?" before trusting the absence of an anomaly for it. A newly
# launched category has no baseline, and silence about it would read as good news.
COVERAGE_TABLE = "detection_coverage"

# Consecutive flagged days in one cell are one incident. A gap of more than this many days
# starts a new episode rather than bridging two unrelated events.
EPISODE_MAX_GAP_DAYS = 1

# Peer group size on holidays. The common factor narrows to the cell's own category on holiday
# dates because holiday response is category-scaled; 12 cells per category, so 8 is the floor.
MIN_CELLS_FOR_CATEGORY_FACTOR = 8
