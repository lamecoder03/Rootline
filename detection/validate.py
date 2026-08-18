# Scores the detector against docs/ground_truth_anomalies.csv AND against the decoy list.
# Exists because a detector is only as credible as its false-positive record: staying silent on
# Black Friday is worth nothing unless someone checked that it did, and reported the check.
# Reports hit rate and detection lag per anomaly, then an explicit verdict per decoy.

import os
import sys

import numpy as np
import pandas as pd

from detection import config as cfg
from detection.detector import detect, to_episodes
from detection.persist import REPO_ROOT, build_engine

GROUND_TRUTH_CSV = os.path.join(REPO_ROOT, "docs", "ground_truth_anomalies.csv")

# The affected slice per anomaly, taken from docs/ground_truth_anomalies.md. Kept here rather
# than inferred, so the eval is scored against the stated answer and not against itself.
ANOMALY_SLICES = {
    "ANOM-01": {"category": "Apparel"},
    "ANOM-02": {"category": "Electronics", "region": "West"},
    "ANOM-03": {"channel": "Mobile App"},
}
ANOMALY_LABELS = {
    "ANOM-01": "Spring Style Event promotion (easy)",
    "ANOM-02": "West-region Electronics stockout (medium)",
    "ANOM-03": "Mobile App acquisition budget cut (hard)",
}

# Decoys from the ground-truth doc. Each is a legitimate movement the detector must not report.
DECOY_DATES = {
    "Black Friday": ["2024-11-29", "2025-11-28"],
    "Cyber Monday": ["2024-12-02", "2025-12-01"],
    "Christmas Day": ["2024-12-25", "2025-12-25"],
    "Christmas Eve": ["2024-12-24", "2025-12-24"],
}
Q4_RAMP_WINDOWS = [("2024-11-01", "2024-12-20"), ("2025-11-01", "2025-12-20")]


def _slice_mask(frame, slice_spec):
    mask = pd.Series(True, index=frame.index)
    for column, value in slice_spec.items():
        mask &= frame[column] == value
    return mask


def evaluate_anomalies(points, truth):
    """For each injected anomaly: how many of its affected cell-days were flagged, when the
    detector first fired, and how many days that was after the event actually began."""
    results = []
    for anomaly_id, slice_spec in ANOMALY_SLICES.items():
        days = truth[truth.anomaly_id == anomaly_id]
        start, end = days.order_date.min(), days.order_date.max()

        in_event = points[
            points.order_date.between(start, end) & _slice_mask(points, slice_spec)
        ]
        hits = in_event[in_event.is_anomaly]
        first = hits.order_date.min() if len(hits) else pd.NaT

        results.append({
            "anomaly_id": anomaly_id,
            "label": ANOMALY_LABELS[anomaly_id],
            "window_start": start.date(),
            "window_end": end.date(),
            "event_days": (end - start).days + 1,
            "cells_affected": in_event.cell_key.nunique(),
            "cells_detected": hits.cell_key.nunique(),
            "cell_days_in_window": len(in_event),
            "cell_days_flagged": len(hits),
            "recall_pct": round(100.0 * len(hits) / max(len(in_event), 1), 1),
            "detected": bool(len(hits)),
            "first_detection": first.date() if pd.notna(first) else None,
            "detection_lag_days": int((first - start).days) if pd.notna(first) else None,
            "peak_z": round(float(hits.z_score.abs().max()), 2) if len(hits) else None,
            "direction": hits.direction.mode()[0] if len(hits) else None,
        })
    return pd.DataFrame(results)


def evaluate_decoys(points, truth):
    """Every decoy gets an explicit verdict. Silence is only evidence if the silence was
    checked, so each row states what was inspected and what was found."""
    event_days = set()
    for anomaly_id, slice_spec in ANOMALY_SLICES.items():
        days = truth[truth.anomaly_id == anomaly_id]
        window = pd.date_range(days.order_date.min(), days.order_date.max())
        for day in window:
            event_days.add((anomaly_id, day))
    real_window_dates = {day for _, day in event_days}

    rows = []
    for name, dates in DECOY_DATES.items():
        dates = pd.to_datetime(dates)
        subset = points[points.order_date.isin(dates)]
        flagged = subset[subset.is_anomaly]
        rows.append({
            "decoy": name,
            "detail": ", ".join(d.strftime("%Y-%m-%d") for d in dates),
            "cell_days_checked": len(subset),
            "cell_days_flagged": len(flagged),
            "max_abs_z": round(float(subset.z_score.abs().max()), 2) if len(subset) else None,
            "verdict": "PASS - not flagged" if flagged.empty else f"FAIL - {len(flagged)} flagged",
        })

    ramp = points[
        np.logical_or.reduce([
            points.order_date.between(pd.Timestamp(a), pd.Timestamp(b))
            for a, b in Q4_RAMP_WINDOWS
        ])
    ]
    ramp = ramp[~ramp.order_date.isin(pd.to_datetime(sum(DECOY_DATES.values(), [])))]
    ramp_flagged = ramp[ramp.is_anomaly]
    rows.append({
        "decoy": "Q4 ramp",
        "detail": "1 Nov - 20 Dec, both years, excluding the named holidays",
        "cell_days_checked": len(ramp),
        "cell_days_flagged": len(ramp_flagged),
        "max_abs_z": round(float(ramp.z_score.abs().max()), 2) if len(ramp) else None,
        "verdict": (
            "PASS - not flagged" if ramp_flagged.empty
            else f"{len(ramp_flagged)} flagged ({100.0 * len(ramp_flagged) / len(ramp):.2f}% of window)"
        ),
    })

    month_start = points[points.order_date.dt.day == 1]
    other_days = points[points.order_date.dt.day != 1]
    ms_rate = 100.0 * month_start.is_anomaly.mean()
    od_rate = 100.0 * other_days.is_anomaly.mean()
    rows.append({
        "decoy": "Monthly budget steps",
        "detail": "flag rate on the 1st of each month vs every other day",
        "cell_days_checked": len(month_start),
        "cell_days_flagged": int(month_start.is_anomaly.sum()),
        "max_abs_z": round(float(month_start.z_score.abs().max()), 2),
        "verdict": f"{ms_rate:.2f}% on month starts vs {od_rate:.2f}% otherwise",
    })

    outside = points[~points.order_date.isin(real_window_dates)]
    outside_flagged = outside[outside.is_anomaly]
    rows.append({
        "decoy": "All other quiet days",
        "detail": "every scored cell-day outside the three injected windows",
        "cell_days_checked": len(outside),
        "cell_days_flagged": len(outside_flagged),
        "max_abs_z": round(float(outside.z_score.abs().max()), 2),
        "verdict": f"{100.0 * len(outside_flagged) / len(outside):.2f}% false-positive rate",
    })
    return pd.DataFrame(rows)


def summarise_episodes(episodes, truth):
    """Splits detected incidents into the ones that overlap a real injected window and the ones
    that do not. The second number is the honest cost of the first."""
    labels = []
    for row in episodes.itertuples():
        matched = None
        for anomaly_id, slice_spec in ANOMALY_SLICES.items():
            days = truth[truth.anomaly_id == anomaly_id]
            start, end = days.order_date.min(), days.order_date.max()
            in_slice = all(
                getattr(row, column) == value for column, value in slice_spec.items()
            )
            if in_slice and row.start_date <= end and row.end_date >= start:
                matched = anomaly_id
                break
        labels.append(matched)
    episodes = episodes.copy()
    episodes["matched_anomaly"] = labels
    return episodes


def main():
    engine = build_engine()
    truth = pd.read_csv(GROUND_TRUTH_CSV, parse_dates=["order_date"])

    points = detect(engine)
    episodes = summarise_episodes(to_episodes(points), truth)

    print("=" * 96)
    print("DETECTOR VALIDATION vs docs/ground_truth_anomalies.csv")
    print("=" * 96)
    print(f"scored {len(points):,} cell-days  |  flagged {int(points.is_anomaly.sum()):,} "
          f"({100.0 * points.is_anomaly.mean():.2f}%)  |  {len(episodes)} episodes")
    print(f"threshold |z| >= {cfg.Z_THRESHOLD:.1f} "
          f"({cfg.Z_THRESHOLD * cfg.HOLIDAY_THRESHOLD_MULTIPLIER:.1f} on holidays), "
          f"BH-FDR q < {cfg.FDR_Q}")

    print("\n--- 1. THE THREE INJECTED ANOMALIES " + "-" * 60)
    results = evaluate_anomalies(points, truth)
    for row in results.itertuples():
        status = "DETECTED" if row.detected else "*** MISSED ***"
        print(f"\n  {row.anomaly_id}  {row.label}")
        print(f"    window          {row.window_start} -> {row.window_end}  ({row.event_days} days)")
        print(f"    status          {status}")
        if row.detected:
            print(f"    first fired     {row.first_detection}   lag {row.detection_lag_days} day(s) "
                  f"after the event began")
            print(f"    cells           {row.cells_detected} of {row.cells_affected} affected cells flagged")
            print(f"    cell-days       {row.cell_days_flagged} of {row.cell_days_in_window} "
                  f"({row.recall_pct}% recall within the window)")
            print(f"    peak |z|        {row.peak_z}   direction reported: {row.direction}")

    print("\n--- 2. DECOYS: THINGS THAT MUST NOT BE FLAGGED " + "-" * 49)
    decoys = evaluate_decoys(points, truth)
    print(f"\n  {'decoy':<24} {'checked':>8} {'flagged':>8} {'max|z|':>7}  verdict")
    print("  " + "-" * 90)
    for row in decoys.itertuples():
        print(f"  {row.decoy:<24} {row.cell_days_checked:>8,} {row.cell_days_flagged:>8,} "
              f"{row.max_abs_z if row.max_abs_z is not None else '-':>7}  {row.verdict}")
        print(f"  {'':<24} {row.detail}")

    print("\n--- 3. EPISODE-LEVEL PRECISION " + "-" * 65)
    matched = episodes[episodes.matched_anomaly.notna()]
    unmatched = episodes[episodes.matched_anomaly.isna()]
    print(f"  episodes matching a real anomaly   {len(matched):>4}")
    print(f"  episodes not matching any anomaly  {len(unmatched):>4}")
    print(f"  episode precision                  {100.0 * len(matched) / max(len(episodes), 1):>7.1f}%")
    if len(unmatched):
        print(f"  false episodes: median {unmatched.day_count.median():.0f} day(s), "
              f"{int((unmatched.day_count == 1).sum())} of {len(unmatched)} are single-day")
        by_month = unmatched.start_date.dt.to_period("M").value_counts().sort_index()
        worst = by_month.nlargest(3)
        print(f"  worst months for false episodes: "
              + ", ".join(f"{p} ({n})" for p, n in worst.items()))

    print("\n--- 4. VERDICT " + "-" * 81)
    all_detected = bool(results.detected.all())
    hard_decoys = decoys[decoys.decoy.isin(list(DECOY_DATES))]
    decoys_clean = all(v.startswith("PASS") for v in hard_decoys.verdict)
    print(f"  all three anomalies detected     {'YES' if all_detected else 'NO'}")
    holiday_flags = int(hard_decoys.cell_days_flagged.sum())
    holiday_checked = int(hard_decoys.cell_days_checked.sum())
    print(f"  named holiday decoys all silent  {'YES' if decoys_clean else 'NO'}"
          f"  ({holiday_flags} of {holiday_checked} holiday cell-days flagged)")
    print(f"  worst detection lag              {int(results.detection_lag_days.max())} day(s) "
          f"(vs the 3-7 days a human takes)")
    print("=" * 96)
    return 0 if all_detected else 1


if __name__ == "__main__":
    sys.exit(main())
