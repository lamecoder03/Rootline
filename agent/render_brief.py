# Renders one finished investigation for several audiences without re-investigating it.
# Exists to prove the intelligence and its presentation are separable: this module has no
# warehouse engine, no tool and no SQL - it reads a saved brief, extracts the action chain ONCE,
# and re-narrates that same evidence per persona, so no two audiences can be told different facts.
#
#   python -m agent.render_brief --brief docs/sample_briefs/eval/DET-0023.md
#   python -m agent.render_brief --anomaly-key DET-0023 --anomaly-key DET-0018 --subdir eval

from __future__ import annotations

import argparse
import json
import os
import sys

from . import config as cfg
from .actions import cap_confidence, extract_actions, render_actions
from .guardrails.db import REPO_ROOT
from .llm import UserTurn
from .personas import DEFAULT_PERSONAS, PERSONAS

OUTPUT_SUBDIR = "personas"

NARRATIVE_SYSTEM = """\
You are re-writing one finished investigation brief for a specific reader. The investigation is \
over. You have no warehouse access, no tools, and no knowledge of this incident beyond the brief \
in front of you.

{style}

# The one rule that overrides everything else

**You may not introduce a single fact, figure, date or cause that is not already in the brief.** \
You are changing altitude and emphasis, never content. If the brief says the cause is not known, \
your version says the cause is not known. If the brief gives no dollar figure, you invent none. \
Rounding a number the brief states is allowed; producing one it does not state is not.

If the brief declares itself partial or cut short, your version must say so too, in the register \
this reader expects.

# Output

Markdown. No document title - the surrounding page supplies one. Do not include a section of \
recommended actions: those are rendered separately from a structured record and would contradict \
you. Do not restate the query list. Write the narrative only.
"""

NARRATIVE_REQUEST = """Re-write this brief for: {audience}

---
{brief}
---"""


def parse_brief_file(path):
    """Splits a saved brief into the parts a re-render needs. The evidence trail is separated
    rather than discarded: the analyst render still shows it, and the action extractor is given
    the query purposes so it can tell a checked-and-flat finding from an unchecked one."""
    with open(path, encoding="utf-8") as handle:
        content = handle.read()

    head_body, _, trail = content.partition("## Evidence trail")
    marker = head_body.find("\n---\n")
    header = head_body[:marker] if marker != -1 else ""
    body = head_body[marker + len("\n---\n"):] if marker != -1 else head_body
    body = body.strip().rstrip("-").strip()

    trail_rows = [line for line in trail.splitlines()
                  if line.startswith("|") and not line.startswith("|---")]
    trail_table = "\n".join(trail_rows[:1] + ["|---|---|---|---|"] + trail_rows[1:])

    # Two different incompletenesses, and the declared status only reports the first. A brief can
    # say "complete" - meaning it never hit the tool-call ceiling - and still stop mid-sentence
    # because the closing turn ran out of output tokens. Both must cap an action's confidence.
    tail = next((line for line in reversed(body.splitlines()) if line.strip()), "")
    body_truncated = not tail.rstrip().endswith((".", "!", "?", "|", ")", '"', "*"))

    return {
        "header": header.strip(),
        "body": body,
        "trail_table": trail_table,
        "declared_partial": "PARTIAL" in header.upper(),
        "body_appears_truncated": body_truncated,
        "is_partial": "PARTIAL" in header.upper() or body_truncated,
        "anomaly_key": os.path.splitext(os.path.basename(path))[0],
    }


def render_narrative(brief_body, persona, provider):
    """One model call per persona, over the brief text alone. Retried once if it comes back empty
    for the same reason the investigation loop retries: gpt-oss spends output budget on reasoning
    before it writes, and an empty section shipped silently is worse than a recorded failure."""
    turns = [UserTurn(NARRATIVE_REQUEST.format(audience=persona.audience, brief=brief_body))]
    reply = provider.chat(
        system=NARRATIVE_SYSTEM.format(style=persona.style),
        turns=turns, tools=None, max_tokens=persona.max_narrative_tokens,
    )
    if not (reply.text or "").strip():
        turns.append(UserTurn(
            "Your previous turn produced no text. Write the re-framed brief now, from the "
            "brief already in front of you. Prose only, no preamble."))
        reply = provider.chat(
            system=NARRATIVE_SYSTEM.format(style=persona.style),
            turns=turns, tools=None, max_tokens=persona.max_narrative_tokens,
        )
    text = (reply.text or "").strip()
    if not text:
        text = ("**No narrative was produced.** The model exhausted its output allowance on "
                "reasoning twice without emitting text. This is a recorded failure, not a "
                "finding - the underlying brief and its action chain are unaffected.")
    return text, reply


def build_document(parsed, persona, narrative, actions, source_path):
    """Assembles one persona's page. The action section is rendered from the shared structured
    record, never from the narrative, which is what stops the two audiences drifting apart."""
    lines = [
        f"# {parsed['anomaly_key']} — {persona.title}",
        "",
        parsed["header"].split("\n", 1)[-1].strip() if "\n" in parsed["header"] else "",
        "",
        f"*Written for: {persona.audience}*",
        "",
        "---",
        "",
        narrative,
        "",
        "---",
        "",
        "## Recommended actions",
        "",
        render_actions(actions, persona),
    ]

    if persona.show_evidence_trail:
        lines += [
            "---", "",
            "## Evidence trail",
            "",
            "Every query the original investigation ran. This render added none.",
            "",
            parsed["trail_table"], "",
        ]

    lines += [
        "---", "",
        "## Provenance",
        "",
        f"- Rendered from `{source_path}` — no new warehouse queries were run for this document.",
        f"- Action chain extracted once and shared across all personas; stance "
        f"`{actions['stance']}`, {len(actions['recommended_actions'])} action(s), "
        f"{len(actions['abstentions'])} abstention(s).",
        "- Persona affects presentation only. The underlying evidence, diagnosis and action "
        "content are identical across renders.",
        "",
    ]
    return "\n".join(line for line in lines if line is not None)


def render_brief(path, personas=DEFAULT_PERSONAS, provider=None, verbose=True):
    """One brief in, one document per persona out, plus the structured action record they share."""
    provider = provider or cfg.build_provider()
    parsed = parse_brief_file(path)
    rel_source = os.path.relpath(path, REPO_ROOT).replace("\\", "/")

    if verbose:
        flags = []
        if parsed["declared_partial"]:
            flags.append("declared PARTIAL")
        if parsed["body_appears_truncated"]:
            flags.append("body cut off mid-sentence")
        print(f"\n=== {parsed['anomaly_key']} "
              f"({'; '.join(flags) if flags else 'complete'} source brief) ===")
        print("  extracting action chain (1 call, no warehouse access)...")

    actions, action_reply = extract_actions(parsed["body"], parsed["trail_table"], provider)
    actions, lowered = cap_confidence(actions, parsed["is_partial"])
    if verbose:
        print(f"    stance={actions['stance']}  actions={len(actions['recommended_actions'])}  "
              f"abstentions={len(actions['abstentions'])}"
              + (f"  ({lowered} confidence capped)" if lowered else ""))

    directory = os.path.join(REPO_ROOT, cfg.BRIEFS_DIR, OUTPUT_SUBDIR)
    os.makedirs(directory, exist_ok=True)
    written, tokens = [], {"in": action_reply.input_tokens, "out": action_reply.output_tokens}

    for key in personas:
        persona = PERSONAS[key]
        if verbose:
            print(f"  rendering {persona.title}...")
        narrative, reply = render_narrative(parsed["body"], persona, provider)
        tokens["in"] += reply.input_tokens
        tokens["out"] += reply.output_tokens

        document = build_document(parsed, persona, narrative, actions, rel_source)
        out_path = os.path.join(directory, f"{parsed['anomaly_key']}-{persona.key}.md")
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(document)
        written.append(out_path)
        if verbose:
            print(f"    -> {os.path.relpath(out_path, REPO_ROOT)}")

    record = {
        "anomaly_key": parsed["anomaly_key"],
        "source_brief": rel_source,
        "source_brief_is_partial": parsed["is_partial"],
        "source_brief_declared_partial": parsed["declared_partial"],
        "source_brief_body_truncated": parsed["body_appears_truncated"],
        "personas_rendered": list(personas),
        "confidence_capped_by_partial_brief": lowered,
        "warehouse_queries_run_by_this_stage": 0,
        "provider": provider.describe(),
        "render_input_tokens": tokens["in"],
        "render_output_tokens": tokens["out"],
        "actions": actions,
    }
    json_path = os.path.join(directory, f"{parsed['anomaly_key']}-actions.json")
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2, ensure_ascii=False)
    written.append(json_path)
    if verbose:
        print(f"    -> {os.path.relpath(json_path, REPO_ROOT)}")
    return written, record


def main():
    parser = argparse.ArgumentParser(
        description="Re-render a finished brief for each persona. Runs no warehouse queries.")
    parser.add_argument("--brief", action="append", dest="briefs",
                        help="Path to a saved brief; repeatable.")
    parser.add_argument("--anomaly-key", action="append", dest="keys",
                        help="Anomaly key to render; resolved under --subdir. Repeatable.")
    parser.add_argument("--subdir", default="eval",
                        help="Subdirectory of docs/sample_briefs/ holding the source briefs.")
    parser.add_argument("--persona", action="append", dest="personas",
                        choices=sorted(PERSONAS), help="Persona to render; repeatable.")
    args = parser.parse_args()

    paths = list(args.briefs or [])
    for key in args.keys or []:
        paths.append(os.path.join(REPO_ROOT, cfg.BRIEFS_DIR, args.subdir, f"{key}.md"))
    if not paths:
        parser.error("give at least one --brief or --anomaly-key")

    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        print("No such brief:\n  " + "\n  ".join(missing))
        return 1

    provider = cfg.build_provider()
    for path in paths:
        render_brief(path, personas=tuple(args.personas or DEFAULT_PERSONAS), provider=provider)
    return 0


if __name__ == "__main__":
    sys.exit(main())
