# Runs every keyed scenario end to end: investigate, save the brief, grade it, report.
# Exists so the eval is one reproducible command rather than a sequence someone remembers, and
# so the result is reported per scenario with the reason for each miss, not as a single score.
#
#   python -m agent.eval.run_eval

from __future__ import annotations

import argparse
import json
import os
import sys

from .. import config as cfg
from ..guardrails.db import REPO_ROOT
from ..investigator import investigate
from ..run_investigation import load_anomalies, save_brief
from . import answer_key
from .grader import grade

RESULTS_PATH = os.path.join(REPO_ROOT, "docs", "sample_briefs", "eval_results.json")


def save(previous, results):
    """Writes after every scenario, not at the end. A sweep costs more than one day's free quota,
    so it can die mid-run on a rate limit - and an end-of-run write would throw away every brief
    already scored. Ordered by the answer key so the file reads the same however it was filled."""
    merged = dict(previous)
    merged.update({r["anomaly_key"]: r for r in results})
    ordered = [merged[s.anomaly_key] for s in answer_key.all_scenarios()
               if s.anomaly_key in merged]
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(ordered, handle, indent=2)
    return ordered


def load_previous():
    """Results accumulate across runs instead of being overwritten. Not a convenience: the free
    tier's 200,000-token DAILY cap is smaller than one full ten-scenario sweep, so the eval has
    to be completable in batches over more than one day without losing what it already scored."""
    if not os.path.exists(RESULTS_PATH):
        return {}
    with open(RESULTS_PATH, encoding="utf-8") as handle:
        try:
            return {r["anomaly_key"]: r for r in json.load(handle)}
        except (ValueError, KeyError, TypeError):
            return {}


def main():
    parser = argparse.ArgumentParser(description="Run the agent eval.")
    parser.add_argument("--only", action="append", dest="only",
                        help="Restrict to these anomaly keys; repeatable.")
    parser.add_argument("--source", choices=["ground_truth", "operator"], default=None,
                        help="Restrict to one half of the answer key.")
    args = parser.parse_args()

    if not answer_key.is_ready():
        print("The answer key is incomplete.\n")
        print("OPERATOR_SCENARIOS in agent/eval/answer_key.py is empty, so only the three")
        print("ground-truth cases are keyed. Run\n")
        print("    python -m agent.eval.candidate_evidence\n")
        print("and have the project owner decide the expected conclusion for each candidate")
        print("before running the eval. Grading the agent against a key written by the agent's")
        print("own author is not an evaluation.")
        return 2

    scenarios = answer_key.all_scenarios()
    if args.source:
        scenarios = [s for s in scenarios if s.source == args.source]
    if args.only:
        wanted = set(args.only)
        scenarios = [s for s in scenarios if s.anomaly_key in wanted]
    if not scenarios:
        print("No scenarios matched the filters.")
        return 2

    by_key = {a["anomaly_key"]: a for a in load_anomalies([s.anomaly_key for s in scenarios])}
    provider = cfg.build_provider()
    previous = load_previous()
    results = []

    print(f"Running {len(scenarios)} scenarios at {cfg.MAX_TOOL_CALLS} tool calls each, "
          f"model {cfg.MODEL}.")
    if previous:
        print(f"Merging into {len(previous)} scenario(s) already scored in "
              f"{os.path.relpath(RESULTS_PATH, REPO_ROOT)}.")
    print()

    for scenario in scenarios:
        anomaly = by_key.get(scenario.anomaly_key)
        if anomaly is None:
            print(f"  !! {scenario.anomaly_key} is not in analytics.detected_anomalies - skipped")
            continue

        print(f"=== {scenario.anomaly_key}  {scenario.label}")
        try:
            investigation = investigate(anomaly, provider=provider)
            path = save_brief(investigation, anomaly, subdir="eval")
            verdict = grade(scenario, investigation.brief, provider=provider)
        except Exception as error:
            # A daily-quota exhaustion mid-sweep is expected on the free tier and must not look
            # like a scored failure. Stop cleanly, keep what completed, and name the reason.
            print(f"  !! {scenario.anomaly_key} could not be completed: "
                  f"{type(error).__name__}: {str(error)[:220]}")
            print(f"  Stopping. {len(results)} scenario(s) scored this run and already saved; "
                  f"re-run with --only to continue.\n")
            break

        mark = "PASS" if verdict.passed else "FAIL"
        print(f"  {mark}  concluded '{verdict.claimed_cause}' (expected "
              f"'{scenario.expected_cause}')  {investigation.calls_used} calls"
              f"{'  [PARTIAL]' if investigation.truncated else ''}")
        for note in verdict.failure_notes:
            print(f"        - {note}")
        print(f"        brief: {os.path.relpath(path, REPO_ROOT)}\n")

        results.append({
            "anomaly_key": scenario.anomaly_key, "label": scenario.label,
            "source": scenario.source, "ground_truth_id": scenario.ground_truth_id,
            "expected_cause": scenario.expected_cause,
            "acceptable_alternatives": list(scenario.acceptable_alternatives),
            "claimed_cause": verdict.claimed_cause, "passed": verdict.passed,
            "checks": verdict.checks, "failure_notes": verdict.failure_notes,
            "extraction": verdict.extraction,
            "tool_calls_used": investigation.calls_used,
            "tool_calls_rejected": investigation.rejected_calls,
            "truncated": investigation.truncated,
            "results_elided": investigation.results_elided,
            "stop_reason": investigation.stop_reason,
            "model": cfg.MODEL,
            "elapsed_s": round(investigation.elapsed_s, 1),
            "input_tokens": investigation.input_tokens,
            "output_tokens": investigation.output_tokens,
            "brief_path": os.path.relpath(path, REPO_ROOT).replace("\\", "/"),
            "investigation_id": investigation.investigation_id,
            "provider": investigation.provider,
        })
        save(previous, results)

    passed = sum(1 for r in results if r["passed"])
    cause_right = sum(1 for r in results if r["claimed_cause"] in
                      {r["expected_cause"], *r["acceptable_alternatives"]})
    calls = [r["tool_calls_used"] for r in results]

    print("=" * 78)
    print(f"  Scenarios run              : {len(results)}")
    print(f"  Correct cause family       : {cause_right}/{len(results)}")
    print(f"  Passed every check         : {passed}/{len(results)}")
    print(f"  Truncated by the call cap  : {sum(1 for r in results if r['truncated'])}")
    if calls:
        print(f"  Tool calls  min/median/max : {min(calls)} / "
              f"{sorted(calls)[len(calls)//2]} / {max(calls)}  (ceiling {cfg.MAX_TOOL_CALLS})")
    print("=" * 78)

    ordered = save(previous, results)
    print(f"\nResults: {os.path.relpath(RESULTS_PATH, REPO_ROOT)}  "
          f"({len(ordered)} of {len(answer_key.all_scenarios())} scenarios scored in total)")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
