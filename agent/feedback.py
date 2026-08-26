# Captures what a human thought of a brief, tied to the brief that produced it.
# Exists because an analyst who spots a wrong conclusion currently has nowhere to put it, so the
# same mistake ships again next week. Deliberately capture-only: it records the correction and
# aggregates it, and does NOT feed anything back automatically - see docs/aic_feedback_loop.md.
#
#   python -m agent.feedback --anomaly-key DET-0023 --verdict accurate --by "ops.analyst"
#   python -m agent.feedback --summary

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from .guardrails.db import build_owner_engine

# Three verdicts, not five. A scale invites a 3-out-of-5 that means nothing; these force a
# decision about whether the brief was safe to act on, which is the only question that matters.
VERDICTS = ("accurate", "partially_accurate", "inaccurate")

# Which part was wrong. Without this a correction says "this is wrong" and the next person cannot
# tell whether the detector, the diagnosis or the recommendation needs attention - and those three
# live in different layers of this pipeline.
ASPECTS = ("cause", "magnitude", "action", "confidence", "not_an_anomaly", "other")

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS feedback;

CREATE TABLE IF NOT EXISTS feedback.brief_feedback (
    feedback_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    submitted_at      timestamptz NOT NULL DEFAULT now(),
    anomaly_key       text        NOT NULL,
    persona           text,
    investigation_id  text,
    verdict           text        NOT NULL,
    disputed_aspect   text,
    correction_note   text,
    submitted_by      text,
    CONSTRAINT brief_feedback_verdict_ck
        CHECK (verdict IN ('accurate', 'partially_accurate', 'inaccurate')),
    CONSTRAINT brief_feedback_aspect_ck
        CHECK (disputed_aspect IS NULL OR disputed_aspect IN
               ('cause', 'magnitude', 'action', 'confidence', 'not_an_anomaly', 'other')),
    CONSTRAINT brief_feedback_correction_ck
        CHECK (verdict = 'accurate' OR disputed_aspect IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS brief_feedback_anomaly_idx
    ON feedback.brief_feedback (anomaly_key, submitted_at DESC);

COMMENT ON TABLE feedback.brief_feedback IS
    'Human verdicts on generated briefs. Capture only: nothing in this pipeline reads this table '
    'to alter its own behaviour. Owned by the operator role, not the agent - the agent has no '
    'grant here and cannot mark its own work correct.';
"""


def ensure_schema(engine=None):
    """Idempotent. Kept out of guardrails/provision.py on purpose: that module provisions what the
    AGENT may touch, and the agent must never be able to write here."""
    engine = engine or build_owner_engine()
    with engine.begin() as connection:
        connection.execute(text(SCHEMA_SQL))
    return engine


def submit(anomaly_key, verdict, aspect=None, note=None, persona=None,
           investigation_id=None, by=None, engine=None):
    """Writes one verdict. Uses the OWNER engine because feedback is a human action about the
    agent, not an action by it - routing it through the agent's role would let the agent's
    credentials write the record that grades the agent."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    if aspect is not None and aspect not in ASPECTS:
        raise ValueError(f"aspect must be one of {ASPECTS}, got {aspect!r}")
    if verdict != "accurate" and aspect is None:
        raise ValueError("a non-accurate verdict must name the disputed aspect")

    engine = ensure_schema(engine)
    with engine.begin() as connection:
        return connection.execute(
            text("""INSERT INTO feedback.brief_feedback
                    (anomaly_key, persona, investigation_id, verdict, disputed_aspect,
                     correction_note, submitted_by)
                    VALUES (:key, :persona, :inv, :verdict, :aspect, :note, :by)
                    RETURNING feedback_id"""),
            {"key": anomaly_key, "persona": persona, "inv": investigation_id,
             "verdict": verdict, "aspect": aspect, "note": note, "by": by},
        ).scalar()


def summarise(engine=None):
    """The aggregate a human reviews before deciding what to change. Returning it rather than
    acting on it is the whole design: the loop is closed by a person, and this is what they read."""
    engine = ensure_schema(engine)
    with engine.begin() as connection:
        totals = connection.execute(text(
            """SELECT verdict, count(*) n FROM feedback.brief_feedback
               GROUP BY verdict ORDER BY n DESC""")).all()
        aspects = connection.execute(text(
            """SELECT disputed_aspect, count(*) n FROM feedback.brief_feedback
               WHERE disputed_aspect IS NOT NULL GROUP BY disputed_aspect ORDER BY n DESC""")).all()
        recent = connection.execute(text(
            """SELECT anomaly_key, persona, verdict, disputed_aspect, correction_note,
                      submitted_by, submitted_at
               FROM feedback.brief_feedback ORDER BY submitted_at DESC LIMIT 10""")).all()
    return {
        "by_verdict": [dict(r._mapping) for r in totals],
        "by_aspect": [dict(r._mapping) for r in aspects],
        "recent": [dict(r._mapping) for r in recent],
        "total": sum(r.n for r in totals),
    }


def _print_summary(summary):
    if not summary["total"]:
        print("No feedback captured yet.")
        return
    print(f"\n{summary['total']} verdict(s) captured\n")
    print("  By verdict:")
    for row in summary["by_verdict"]:
        share = 100.0 * row["n"] / summary["total"]
        print(f"    {row['verdict']:<20} {row['n']:>3}  ({share:.0f}%)")
    if summary["by_aspect"]:
        print("\n  Disputed aspect (non-accurate verdicts only):")
        for row in summary["by_aspect"]:
            print(f"    {row['disputed_aspect']:<20} {row['n']:>3}")
    print("\n  Most recent:")
    for row in summary["recent"]:
        stamp = row["submitted_at"].strftime("%Y-%m-%d %H:%M")
        aspect = f" [{row['disputed_aspect']}]" if row["disputed_aspect"] else ""
        who = row["submitted_by"] or "anonymous"
        print(f"    {stamp}  {row['anomaly_key']:<10} {row['verdict']:<18}{aspect}  by {who}")
        if row["correction_note"]:
            print(f"                 note: {row['correction_note']}")
    print("\n  Nothing in this pipeline reads this table to change its own behaviour.")
    print("  Acting on it is a human decision - see docs/aic_feedback_loop.md.\n")


def main():
    parser = argparse.ArgumentParser(description="Capture or review feedback on a brief.")
    parser.add_argument("--anomaly-key", help="The brief being reviewed, e.g. DET-0023.")
    parser.add_argument("--verdict", choices=VERDICTS)
    parser.add_argument("--aspect", choices=ASPECTS, help="What was wrong. Required unless accurate.")
    parser.add_argument("--note", help="Free-text correction.")
    parser.add_argument("--persona", help="Which render was read, e.g. analyst / executive.")
    parser.add_argument("--investigation-id", help="Ties this verdict to the audit trail.")
    parser.add_argument("--by", help="Who is submitting.")
    parser.add_argument("--summary", action="store_true", help="Print the aggregate and exit.")
    args = parser.parse_args()

    if args.summary:
        _print_summary(summarise())
        return 0
    if not (args.anomaly_key and args.verdict):
        parser.error("--anomaly-key and --verdict are required (or use --summary)")

    try:
        feedback_id = submit(
            anomaly_key=args.anomaly_key, verdict=args.verdict, aspect=args.aspect,
            note=args.note, persona=args.persona, investigation_id=args.investigation_id,
            by=args.by)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Recorded feedback #{feedback_id} on {args.anomaly_key}: {args.verdict}"
          + (f" [{args.aspect}]" if args.aspect else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
