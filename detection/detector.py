# The rolling z-score detector: finds days where one revenue cell moved unlike its own history
# AND unlike the rest of the business on the same day.
# Exists because a Revenue Ops lead needs the dip flagged before the weekly report, and because
# a naive rolling mean on this data flags every Monday, every holiday and the whole Q4 ramp.
# Five stages: same-weekday baseline, common-calendar removal, robust scaling, empirical-null
# calibration, then a windowed hypothesis test with false-discovery-rate control.

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from . import config as cfg
from . import coverage


def _load_frame(engine):
    """Reads the revenue fact at its native grain. Nothing is aggregated: ANOM-02 touches 3 of
    60 cells and disappears entirely into any total."""
    frame = pd.read_sql(
        f"""
        select order_date, category, channel, region, gross_revenue,
               is_holiday, retail_significance, day_of_week, is_weekend
        from {cfg.SOURCE_TABLE}
        order by order_date
        """,
        engine,
    )
    frame["order_date"] = pd.to_datetime(frame["order_date"])
    frame["cell_key"] = frame.category + " | " + frame.channel + " | " + frame.region
    return frame


def _pivot(frame):
    """Reshapes to date x cell matrices. Work happens in log space because the generating
    process is multiplicative (trend x weekday x season x holiday) and its noise is lognormal,
    so logs turn a product into a sum and make the spread constant across the level."""
    dates = np.array(sorted(frame.order_date.unique()))
    cells = sorted(frame.cell_key.unique())
    log_revenue = (
        frame.assign(y=np.log(frame.gross_revenue.astype(float)))
        .pivot_table(index="order_date", columns="cell_key", values="y")
        .loc[dates, cells]
        .values
    )
    # fillna(False) before the bool cast: a cell that did not exist on a date leaves a NaN here,
    # and NaN casts to True. Without this a single newly launched cell marks EVERY day a holiday,
    # which silently switches the whole series to the widened holiday control limit.
    is_holiday = (
        frame.pivot_table(index="order_date", columns="cell_key", values="is_holiday")
        .loc[dates, cells]
        .fillna(False)
        .values.astype(bool)
    )
    return pd.DatetimeIndex(dates), cells, log_revenue, is_holiday


def _seasonal_residuals(log_revenue, is_holiday):
    """Stage 1. Each day is compared against the median of the same weekday over the trailing
    BASELINE_WEEKS, skipping BASELINE_GAP_WEEKS so a running event cannot enter its own
    baseline. Holidays are left out of the reference set: the baseline should describe a normal
    day, and a holiday is by definition not one."""
    day_count, cell_count = log_revenue.shape
    residuals = np.full((day_count, cell_count), np.nan)
    baseline_n = np.zeros((day_count, cell_count), dtype=int)

    offsets = [
        7 * (week + cfg.BASELINE_GAP_WEEKS)
        for week in range(1, cfg.BASELINE_WEEKS + 1)
    ]

    for position in range(day_count):
        candidates = np.array([position - offset for offset in offsets])
        candidates = candidates[candidates >= 0]
        if len(candidates) < cfg.MIN_BASELINE_OBSERVATIONS:
            continue
        for cell in range(cell_count):
            usable = candidates[~is_holiday[candidates, cell]]
            if len(usable) < cfg.MIN_BASELINE_OBSERVATIONS:
                continue
            residuals[position, cell] = log_revenue[position, cell] - np.median(
                log_revenue[usable, cell]
            )
            baseline_n[position, cell] = len(usable)

    return residuals, baseline_n


def _remove_common_calendar_factor(residuals, cell_categories, day_is_holiday):
    """Stage 2. Every cell shares one calendar, so a move the whole business made together is
    the calendar, not an incident. Subtracting the cross-sectional median each day removes the
    Q4 ramp and the seasonal drift without ever naming them, and a median tolerates up to half
    the cells being affected, which is why a 20-of-60-cell event still survives it.

    On holidays the peer group narrows to the cell's own category. Holiday response is
    category-specific in retail - Electronics lives or dies on Black Friday, Home & Garden
    barely notices - so one median across all 60 cells cannot represent both ends at once.
    Measured on Christmas Day, Electronics falls to 12% of a normal day while Home & Garden
    only falls to 51%; a single global median leaves that spread behind and reads it as an
    incident. The trade-off is explicit: a category-wide anomaly landing exactly on a holiday
    would be absorbed. That is 30 days of 731, and the alternative is a detector that reports
    Christmas every year."""
    adjusted = residuals.copy()
    common = np.full(residuals.shape[0], np.nan)
    categories = np.asarray(cell_categories)

    for position in range(residuals.shape[0]):
        row = residuals[position, :]
        if day_is_holiday[position]:
            for category in np.unique(categories):
                members = categories == category
                values = row[members]
                if np.isfinite(values).sum() >= cfg.MIN_CELLS_FOR_CATEGORY_FACTOR:
                    adjusted[position, members] = values - np.nanmedian(values)
            if np.isfinite(row).any():
                common[position] = np.nanmedian(row)
        elif np.isfinite(row).sum() >= cfg.MIN_CELLS_FOR_COMMON_FACTOR:
            common[position] = np.nanmedian(row)
            adjusted[position, :] = row - common[position]

    return adjusted, common


def _standardise(residuals, is_holiday):
    """Stage 3. Scales each residual by the recent spread of forecast errors in the same cell,
    not by the spread of the baseline values - the quantity that matters is how large today's
    miss is against how large misses usually are. Median and MAD keep a live anomaly inside the
    trailing window from inflating the very scale meant to reveal it."""
    day_count, cell_count = residuals.shape
    z_raw = np.full((day_count, cell_count), np.nan)
    scale_n = np.zeros((day_count, cell_count), dtype=int)

    for cell in range(cell_count):
        series = residuals[:, cell]
        for position in range(day_count):
            if not np.isfinite(series[position]):
                continue
            low = max(0, position - cfg.RESIDUAL_WINDOW_DAYS - cfg.RESIDUAL_GAP_DAYS)
            high = max(low, position - cfg.RESIDUAL_GAP_DAYS)
            history = series[low:high]
            usable = np.isfinite(history) & ~is_holiday[low:high, cell]
            history = history[usable]
            if len(history) < cfg.MIN_RESIDUAL_OBSERVATIONS:
                continue
            centre = np.median(history)
            scale = cfg.MAD_TO_SIGMA * np.median(np.abs(history - centre))
            if scale < cfg.MIN_SCALE:
                continue
            z_raw[position, cell] = (series[position] - centre) / scale
            scale_n[position, cell] = len(history)

    return z_raw, scale_n


def _calibrate_empirical_null(points):
    """Stage 4. The z-scores come out mildly over-dispersed - a robust scale estimated from 56
    points does not perfectly match a normal - so the null is measured from the data instead of
    assumed. Rescaling by the robust spread of the whole z distribution (Efron's empirical null)
    is what makes the p-values in stage 5 honest rather than optimistic."""
    centre = np.median(points.z_raw)
    spread = cfg.MAD_TO_SIGMA * np.median(np.abs(points.z_raw - centre))
    points["z_score"] = (points.z_raw - centre) / spread
    return points, centre, spread


def _confirm(points):
    """Stage 5. A threshold crossing is a candidate, not a finding. Each point's residual is
    pooled over trailing windows of 1, 2 and 3 days: a sustained shift grows by sqrt(k) while an
    isolated noise spike averages away. The strongest window is taken and Bonferroni-corrected
    for having looked three ways, then p-values are turned into q-values by Benjamini-Hochberg
    so that 43,860 simultaneous tests do not manufacture ~118 findings by chance."""
    points = points.sort_values(["cell_key", "order_date"]).reset_index(drop=True)

    pooled = {}
    for window in cfg.CONFIRMATION_WINDOWS:
        pooled[window] = points.groupby("cell_key", sort=False)["z_score"].transform(
            lambda s, w=window: s.rolling(w).mean() * np.sqrt(w)
        )
    stacked = pd.concat(pooled.values(), axis=1)

    strength = stacked.abs().values
    best = np.nanargmax(np.where(np.isnan(strength), -np.inf, strength), axis=1)
    points["z_confirmed"] = np.nanmax(strength, axis=1)
    points["confirmation_window_days"] = [cfg.CONFIRMATION_WINDOWS[i] for i in best]

    degrees_of_freedom = np.clip(points.scale_n, cfg.MIN_RESIDUAL_OBSERVATIONS, cfg.MAX_SCALE_DF) - 1
    points["p_value"] = np.minimum(
        1.0,
        len(cfg.CONFIRMATION_WINDOWS) * 2 * stats.t.sf(points.z_confirmed, degrees_of_freedom),
    )
    points["q_value"] = _benjamini_hochberg(points.p_value.values)
    return points


def _benjamini_hochberg(p_values):
    """Turns p-values into q-values by the Benjamini-Hochberg step-up procedure. Controls the
    expected proportion of false findings among those reported, which is the right guarantee
    here: a handful of wrong flags is tolerable, a detector that cries wolf is not."""
    count = len(p_values)
    order = np.argsort(p_values)
    ranked = p_values[order] * count / np.arange(1, count + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    q_values = np.empty(count)
    q_values[order] = ranked
    return np.clip(q_values, 0.0, 1.0)


def _flag(points):
    """Applies the two conditions a finding must satisfy: it clears the control limit for that
    day - widened on holidays, where large moves are expected rather than suspicious - and it
    survives false-discovery-rate control. Either one alone is not evidence."""
    limit = np.where(
        points.is_holiday, cfg.Z_THRESHOLD * cfg.HOLIDAY_THRESHOLD_MULTIPLIER, cfg.Z_THRESHOLD
    )
    points["z_threshold_applied"] = limit
    points["is_anomaly"] = (points.z_confirmed >= limit) & (points.q_value < cfg.FDR_Q)
    points["direction"] = np.where(points.z_score < 0, "drop", "spike")
    return points


def detect(engine):
    """Runs the whole pipeline and returns every scored point, with is_anomaly set on the ones
    that cleared both the control limit and the FDR test. Returns all points, not just the
    flagged ones, so the validation harness can prove what the detector did NOT fire on."""
    frame = _load_frame(engine)
    dates, cells, log_revenue, is_holiday = _pivot(frame)

    residuals, baseline_n = _seasonal_residuals(log_revenue, is_holiday)
    cell_categories = [cell.split(" | ")[0] for cell in cells]
    day_is_holiday = is_holiday.any(axis=1)
    adjusted, common = _remove_common_calendar_factor(residuals, cell_categories, day_is_holiday)
    z_raw, scale_n = _standardise(adjusted, is_holiday)

    day_index, cell_index = np.meshgrid(
        np.arange(len(dates)), np.arange(len(cells)), indexing="ij"
    )
    points = pd.DataFrame(
        {
            "order_date": dates[day_index.ravel()],
            "cell_key": np.array(cells)[cell_index.ravel()],
            "log_residual": adjusted.ravel(),
            "common_factor": np.repeat(common, len(cells)),
            "z_raw": z_raw.ravel(),
            "baseline_n": baseline_n.ravel(),
            "scale_n": scale_n.ravel(),
            "is_holiday": is_holiday.ravel(),
        }
    )

    # Everything dropped here is a day the detector could not score. Recorded before the drop,
    # because an unscored day is a low-confidence report, not an absence of news.
    unscored = coverage.classify(dates, cells, log_revenue, baseline_n, scale_n, z_raw)
    cell_coverage = coverage.summarise(unscored, dates, cells, log_revenue)

    points = points.dropna(subset=["z_raw"])

    points, null_centre, null_spread = _calibrate_empirical_null(points)
    points = _confirm(points)
    points = _flag(points)

    attributes = frame[
        ["order_date", "cell_key", "category", "channel", "region",
         "gross_revenue", "retail_significance", "day_of_week"]
    ]
    points = points.merge(attributes, on=["order_date", "cell_key"], how="left")
    points["expected_revenue"] = points.gross_revenue.astype(float) / np.exp(points.log_residual)
    points["delta_pct"] = 100.0 * (np.exp(points.log_residual) - 1.0)

    points.attrs["null_centre"] = null_centre
    points.attrs["null_spread"] = null_spread
    points.attrs["unscored"] = unscored
    points.attrs["cell_coverage"] = cell_coverage
    return points.sort_values(["order_date", "cell_key"]).reset_index(drop=True)


def to_episodes(points):
    """Collapses consecutive flagged days in one cell into a single incident, because a
    seven-day stockout is one thing that happened, not seven. The episode is the unit the agent
    agent investigates and the unit the validation scores detection lag against."""
    flagged = points[points.is_anomaly].sort_values(["cell_key", "order_date"])
    episodes = []

    for cell_key, group in flagged.groupby("cell_key", sort=False):
        run = []
        for row in group.itertuples():
            if run and (row.order_date - run[-1].order_date).days > cfg.EPISODE_MAX_GAP_DAYS:
                episodes.append(_summarise_episode(cell_key, run))
                run = []
            run.append(row)
        if run:
            episodes.append(_summarise_episode(cell_key, run))

    if not episodes:
        return pd.DataFrame(
            columns=["cell_key", "category", "channel", "region", "start_date", "end_date",
                     "day_count", "direction", "peak_date", "peak_z_score", "peak_delta_pct",
                     "total_revenue_delta_usd", "min_q_value"]
        )
    return pd.DataFrame(episodes).sort_values(["start_date", "cell_key"]).reset_index(drop=True)


def _summarise_episode(cell_key, run):
    """One incident reduced to what a brief needs: where, when, how deep, and how strong the
    statistical evidence was at its worst point."""
    frame = pd.DataFrame(run)
    peak = frame.loc[frame.z_score.abs().idxmax()]
    return {
        "cell_key": cell_key,
        "category": peak.category,
        "channel": peak.channel,
        "region": peak.region,
        "start_date": frame.order_date.min(),
        "end_date": frame.order_date.max(),
        "day_count": len(frame),
        "direction": "drop" if peak.z_score < 0 else "spike",
        "peak_date": peak.order_date,
        "peak_z_score": round(float(peak.z_score), 3),
        "peak_delta_pct": round(float(peak.delta_pct), 2),
        "total_revenue_delta_usd": round(
            float((frame.gross_revenue.astype(float) - frame.expected_revenue).sum()), 2
        ),
        "min_q_value": float(frame.q_value.min()),
    }
