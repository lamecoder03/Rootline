# Dumps the raw evidence behind each detected anomaly that no ground-truth event explains.
# Exists so the answer key for those scenarios is decided by a human looking at real numbers,
# rather than by the same author who will later grade the agent against it.
# Read-only, owner connection, no agent involvement - this is scoring infrastructure, not agent code.
#
#   python -m agent.eval.candidate_evidence

from __future__ import annotations

import datetime as dt

from sqlalchemy import text

from ..guardrails.db import build_owner_engine

# The three injected events, from docs/ground_truth_anomalies.md. A detected row overlapping one
# of these windows AND matching its slice is explained by it; everything else is a candidate.
GROUND_TRUTH = (
    ("ANOM-01", dt.date(2025, 3, 14), dt.date(2025, 3, 17),
     lambda r: r["category"] == "Apparel"),
    ("ANOM-02", dt.date(2025, 6, 9), dt.date(2025, 6, 15),
     lambda r: r["category"] == "Electronics" and r["region"] == "West"),
    ("ANOM-03", dt.date(2025, 9, 22), dt.date(2025, 10, 5),
     lambda r: r["channel"] == "Mobile App"),
)

CONTEXT_DAYS = 10


def attribution(row):
    hits = []
    for name, start, end, matches in GROUND_TRUTH:
        if row["start_date"] <= end and row["end_date"] >= start and matches(row):
            hits.append(name)
    return hits


def dossier(connection, row):
    """Everything a human needs to decide what the correct conclusion is: the flagged days, the
    surrounding baseline, spend either side, stockouts, and whether the calendar explains it."""
    start, end = row["start_date"], row["end_date"]
    lo, hi = start - dt.timedelta(days=CONTEXT_DAYS), end + dt.timedelta(days=CONTEXT_DAYS)
    params = {"lo": lo, "hi": hi, "s": start, "e": end,
              "cat": row["category"], "ch": row["channel"], "rg": row["region"]}

    daily = connection.execute(text("""
        SELECT order_date, gross_revenue, units, orders, marketing_spend_usd,
               is_holiday, holiday_name, is_retail_event, day_of_week
        FROM analytics.fct_daily_revenue
        WHERE order_date BETWEEN :lo AND :hi
          AND category = :cat AND channel = :ch AND region = :rg
        ORDER BY order_date"""), params).fetchall()

    points = connection.execute(text("""
        SELECT order_date, gross_revenue, expected_revenue, delta_pct, z_score, q_value,
               is_holiday, retail_significance
        FROM analytics.detected_anomaly_points
        WHERE anomaly_key = :k ORDER BY order_date"""), {"k": row["anomaly_key"]}).fetchall()

    stockout = connection.execute(text("""
        SELECT snapshot_date, skus_out_of_stock, skus_tracked, stockout_rate_pct,
               stocked_out_sku_ids
        FROM analytics.fct_daily_stockout
        WHERE snapshot_date BETWEEN :s AND :e AND category = :cat AND region = :rg
        ORDER BY snapshot_date"""), params).fetchall()

    # Did the rest of the business move the same way on those days? A cell-specific move and a
    # business-wide move have completely different explanations.
    peers = connection.execute(text("""
        SELECT count(*) AS other_cells_flagged
        FROM analytics.detected_anomaly_points p
        WHERE p.order_date BETWEEN :s AND :e AND p.cell_key <> :ck"""),
        {"s": start, "e": end, "ck": row["cell_key"]}).scalar()

    spend_before = connection.execute(text("""
        SELECT avg(marketing_spend_usd) FROM analytics.fct_daily_revenue
        WHERE order_date BETWEEN :lo AND :s - 1 AND category = :cat AND channel = :ch
          AND region = :rg"""), params).scalar()
    spend_during = connection.execute(text("""
        SELECT avg(marketing_spend_usd) FROM analytics.fct_daily_revenue
        WHERE order_date BETWEEN :s AND :e AND category = :cat AND channel = :ch
          AND region = :rg"""), params).scalar()

    return {"daily": daily, "points": points, "stockout": stockout, "peers": peers,
            "spend_before": spend_before, "spend_during": spend_during}


def main():
    engine = build_owner_engine()
    with engine.begin() as connection:
        rows = [dict(r._mapping) for r in connection.execute(text("""
            SELECT anomaly_key, cell_key, category, channel, region, start_date, end_date,
                   day_count, direction, peak_date, peak_z_score, peak_delta_pct,
                   total_revenue_delta_usd, min_q_value
            FROM analytics.detected_anomalies ORDER BY anomaly_key"""))]

        candidates = [r for r in rows if not attribution(r)]
        print(f"{len(rows)} detected anomalies; {len(rows) - len(candidates)} explained by "
              f"ANOM-01/02/03; {len(candidates)} candidates needing a human answer key.\n")

        for row in candidates:
            d = dossier(connection, row)
            print("=" * 100)
            print(f"{row['anomaly_key']}   {row['cell_key']}")
            print(f"  window {row['start_date']} .. {row['end_date']}  ({row['day_count']}d, "
                  f"{row['direction']})   peak {row['peak_date']}  z={float(row['peak_z_score']):.2f}"
                  f"  delta {float(row['peak_delta_pct']):+.1f}%  "
                  f"total ${float(row['total_revenue_delta_usd']):+,.0f}  "
                  f"q={float(row['min_q_value']):.2e}")
            print(f"  other cells flagged on the same days: {d['peers']}")

            print("\n  FLAGGED DAYS")
            for p in d["points"]:
                hol = f"  HOLIDAY({p.retail_significance})" if p.is_holiday else ""
                print(f"    {p.order_date}  actual {float(p.gross_revenue):>9,.0f}  "
                      f"expected {float(p.expected_revenue):>9,.0f}  "
                      f"{float(p.delta_pct):+7.1f}%   z={float(p.z_score):+6.2f}{hol}")

            print("\n  DAILY CONTEXT (+/- 10 days; > marks a flagged day)")
            flagged = {p.order_date for p in d["points"]}
            for r in d["daily"]:
                mark = ">" if r.order_date in flagged else " "
                cal = ""
                if r.is_holiday:
                    cal = f"  <- {r.holiday_name}"
                elif r.is_retail_event:
                    cal = "  <- retail event"
                print(f"   {mark} {r.order_date} {r.day_of_week[:3]}  rev {float(r.gross_revenue):>9,.0f}  "
                      f"units {r.units:>5}  orders {r.orders:>4}  "
                      f"spend {float(r.marketing_spend_usd):>8,.0f}{cal}")

            before = float(d["spend_before"] or 0)
            during = float(d["spend_during"] or 0)
            change = ((during - before) / before * 100) if before else 0
            print(f"\n  MARKETING SPEND  before {before:,.0f}/day -> during {during:,.0f}/day "
                  f"({change:+.1f}%)")

            print("  STOCKOUT (this category x region, flagged days only)")
            if not d["stockout"]:
                print("    no rows")
            for s in d["stockout"]:
                print(f"    {s.snapshot_date}  {s.skus_out_of_stock}/{s.skus_tracked} SKUs out "
                      f"({float(s.stockout_rate_pct):.0f}%)  {s.stocked_out_sku_ids or ''}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
