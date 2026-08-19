# Entry point: fetch one or more anomaly rows, investigate each, save the brief to disk.
# Exists so a brief is a file someone can open rather than terminal output that scrolls away -
# the brief is this project's deliverable, so it has to be an artifact.
#
#   python -m agent.run_investigation --anomaly-key DET-0021
#   python -m agent.run_investigation --list

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import text

from . import config as cfg
from .guardrails.db import REPO_ROOT, build_owner_engine
from .investigator import investigate

ANOMALY_COLUMNS = (
    "anomaly_key, cell_key, category, channel, region, start_date, end_date, day_count, "
    "direction, peak_date, peak_z_score, peak_delta_pct, total_revenue_delta_usd, min_q_value"
)


def load_anomalies(keys=None):
    """Reads with the OWNER engine on purpose. Selecting the work is the operator's action; the
    agent's read-only role is used only for the queries the agent itself chooses to run."""
    where, params = "", {}
    if keys:
        where = "WHERE anomaly_key = ANY(:keys)"
        params["keys"] = list(keys)
    with build_owner_engine().begin() as connection:
        rows = connection.execute(
            text(f"SELECT {ANOMALY_COLUMNS} FROM analytics.detected_anomalies {where} "
                 "ORDER BY abs(peak_z_score) DESC"),
            params,
        )
        return [dict(row._mapping) for row in rows]


def save_brief(investigation, anomaly, subdir=None):
    """One markdown file per investigation, with the evidence trail appended. The trail is the
    point: a brief whose queries are not visible cannot be checked, only believed."""
    directory = os.path.join(REPO_ROOT, cfg.BRIEFS_DIR, subdir or "")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{anomaly['anomaly_key']}.md")

    status = "PARTIAL - tool-call ceiling reached" if investigation.truncated else "complete"
    lines = [
        f"# Brief — {anomaly['anomaly_key']}",
        "",
        f"**{anomaly['category']} · {anomaly['channel']} · {anomaly['region']}** — "
        f"{anomaly['start_date']} to {anomaly['end_date']} "
        f"({anomaly['day_count']} days, {anomaly['direction']})",
        "",
        f"Status: **{status}** · {investigation.calls_used}/{cfg.MAX_TOOL_CALLS} tool calls "
        f"({investigation.rejected_calls} rejected) · {investigation.elapsed_s:.0f}s · "
        f"provider `{investigation.provider}`",
        "",
        "---",
        "",
        investigation.brief,
        "",
        "---",
        "",
        "## Evidence trail",
        "",
        "Every query this brief rests on, in the order the agent ran them. Each one also has a "
        f"row in `audit.agent_tool_calls` under `investigation_id = '{investigation.investigation_id}'`.",
        "",
        "| # | Purpose | Outcome | Rows |",
        "|---|---|---|---|",
    ]
    for call in investigation.tool_calls:
        outcome = call["outcome"] if call["outcome"] == "pass" else f"**{call['rejection_code']}**"
        rows = call["row_count"] if call["row_count"] is not None else "—"
        purpose = (call["purpose"] or "").replace("|", "\\|")
        lines.append(f"| {call['index']} | {purpose} | {outcome} | {rows} |")

    lines += ["", "<details>", "<summary>Full SQL for each call</summary>", ""]
    for call in investigation.tool_calls:
        lines += [f"**{call['index']}. {call['purpose']}**", "", "```sql", call["sql"].strip(),
                  "```", ""]
    lines += ["</details>", ""]

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    return path


def main():
    parser = argparse.ArgumentParser(description="Investigate detected revenue anomalies.")
    parser.add_argument("--anomaly-key", action="append", dest="keys",
                        help="Anomaly to investigate; repeatable. Default: all 44.")
    parser.add_argument("--list", action="store_true", help="List anomalies and exit.")
    parser.add_argument("--max-calls", type=int, default=None, help="Override the tool-call cap.")
    parser.add_argument("--subdir", default=None, help="Subdirectory under docs/sample_briefs/.")
    args = parser.parse_args()

    anomalies = load_anomalies(args.keys)
    if not anomalies:
        print("No matching anomalies.")
        return 1

    if args.list:
        print(f"{'key':<10} {'cell':<34} {'window':<25} {'dir':<4} {'peak z':>8} {'delta $':>12}")
        for a in anomalies:
            window = f"{a['start_date']}..{a['end_date']} ({a['day_count']}d)"
            print(f"{a['anomaly_key']:<10} {a['cell_key']:<34} {window:<25} "
                  f"{a['direction']:<4} {float(a['peak_z_score']):>8.2f} "
                  f"{float(a['total_revenue_delta_usd']):>12,.0f}")
        return 0

    for anomaly in anomalies:
        print(f"\n=== {anomaly['anomaly_key']}  {anomaly['cell_key']}  "
              f"{anomaly['start_date']}..{anomaly['end_date']} ===")
        result = investigate(anomaly, max_calls=args.max_calls)
        path = save_brief(result, anomaly, subdir=args.subdir)
        flag = " [PARTIAL]" if result.truncated else ""
        print(f"  -> {os.path.relpath(path, REPO_ROOT)}{flag}  "
              f"({result.calls_used} calls, {result.elapsed_s:.0f}s, "
              f"{result.output_tokens:,} output tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
