# Classifies every cell-day the detector could NOT score, and why.
# Exists because a newly launched category has too little history to baseline, and silently
# dropping it looks identical to "nothing was wrong" - the failure mode this project exists
# to prevent. Turns an invisible dropna() into an explicit, queryable low-confidence report.

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as cfg

# Why a cell-day carries no z-score. Ordered by which constraint binds first: a cell that fails
# the baseline test never reaches the scale test, so the first matching reason is the real one.
NO_BASELINE = "insufficient_baseline_history"
NO_SCALE = "insufficient_residual_history"
DEGENERATE = "degenerate_scale"

# Reported alongside every unscored day so a consumer can tell "too new to judge" from
# "judged and normal". A cell with fewer than this many observed days in the whole series is
# treated as newly launched rather than merely gappy.
NEW_CELL_MAX_DAYS = 120


def classify(dates, cells, log_revenue, baseline_n, scale_n, z_raw):
    """One row per cell-day the detector could not score, carrying the binding constraint and
    the observation counts behind it. Days with no revenue row at all are excluded: the cell did
    not exist yet, which is not the same as existing and being unmeasurable."""
    observed = np.isfinite(log_revenue)
    unscored = observed & ~np.isfinite(z_raw)

    day_index, cell_index = np.meshgrid(
        np.arange(len(dates)), np.arange(len(cells)), indexing="ij"
    )

    frame = pd.DataFrame(
        {
            "order_date": dates[day_index.ravel()],
            "cell_key": np.array(cells)[cell_index.ravel()],
            "baseline_n": baseline_n.ravel(),
            "scale_n": scale_n.ravel(),
            "is_unscored": unscored.ravel(),
            "is_observed": observed.ravel(),
        }
    )
    frame = frame[frame.is_unscored].drop(columns=["is_unscored", "is_observed"])

    frame["reason"] = np.where(
        frame.baseline_n < cfg.MIN_BASELINE_OBSERVATIONS,
        NO_BASELINE,
        np.where(frame.scale_n < cfg.MIN_RESIDUAL_OBSERVATIONS, NO_SCALE, DEGENERATE),
    )
    frame["min_baseline_required"] = cfg.MIN_BASELINE_OBSERVATIONS
    frame["min_residual_required"] = cfg.MIN_RESIDUAL_OBSERVATIONS
    return frame.sort_values(["order_date", "cell_key"]).reset_index(drop=True)


def summarise(unscored, dates, cells, log_revenue):
    """Rolls the per-day report up to one row per cell - the grain a human or an agent actually
    asks about ("can I trust anything about this category yet?"). Carries the cell's first and
    last observed day so a newly launched cell is visibly new rather than merely unscored."""
    observed = np.isfinite(log_revenue)
    per_cell = []

    for index, cell in enumerate(cells):
        seen = observed[:, index]
        if not seen.any():
            continue
        days_observed = int(seen.sum())
        cell_rows = unscored[unscored.cell_key == cell]
        reasons = cell_rows.reason.value_counts()
        per_cell.append({
            "cell_key": cell,
            "category": cell.split(" | ")[0],
            "channel": cell.split(" | ")[1],
            "region": cell.split(" | ")[2],
            "first_observed": dates[seen.argmax()],
            "last_observed": dates[len(seen) - 1 - seen[::-1].argmax()],
            "days_observed": days_observed,
            "days_unscored": len(cell_rows),
            "days_scored": days_observed - len(cell_rows),
            "pct_scored": round(100.0 * (days_observed - len(cell_rows)) / days_observed, 1),
            "is_newly_launched": days_observed <= NEW_CELL_MAX_DAYS,
            "dominant_reason": reasons.index[0] if len(reasons) else None,
        })

    frame = pd.DataFrame(per_cell)
    if frame.empty:
        return frame
    # A cell is reportable only once enough of its days cleared BOTH history gates. Below that
    # the honest answer to "is this category anomalous?" is "not enough history to say".
    frame["confidence"] = np.where(
        frame.days_scored == 0, "none",
        np.where(frame.pct_scored < 50.0, "low", "normal"),
    )
    return frame.sort_values(["confidence", "cell_key"]).reset_index(drop=True)
