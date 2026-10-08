#!/usr/bin/env python3
"Tests for terse-output-gate + terse-judge.\n\nState is redirected into a temp dir through the env vars the hook and the judge\nboth honor, so running this never touches a real session's ledger."

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List

HOOK = os.path.expanduser("~/.agent-context/global/hooks/terse-output-gate.py")
JUDGE = os.path.expanduser("~/.agent-context/global/scripts/terse-judge.py")

LONG = " ".join(["word"] * 120)
SHORT = "Done. Three call sites updated."

failures = []
tmp = tempfile.mkdtemp(prefix="terse-gate-test-")


def transcript(path, user_text="do the thing", assistant="ok",
               failed=False, asked=False):
    'Write a minimal but REAL transcript: one prompt, then assistant content.'
    rows = [{"type": "user", "message": {"content": user_text}}]
    content: List[Dict[str, Any]] = [{"type": "text", "text": assistant}]
    if asked:
        content.insert(0, {"type": "tool_use", "name": "AskUserQuestion", "input": {}})
    rows.append({"type": "assistant", "message": {"content": content}})
    if failed:
        rows.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "boom"}]}})
        rows.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": assistant}]}})
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def run(name, payload, expect_block, expect_in_reason=None, env=None,
        raw=None, expect_rows=1):
    'Drive the hook once and check the decision, the reason and the telemetry.'
    e = dict(os.environ)
    state = os.path.join(tmp, name)
    telemetry = os.path.join(state, "t.jsonl")
    e["TERSE_GATE_STATE_DIR"] = os.path.join(state, "gate")
    e["TERSE_TELEMETRY"] = telemetry
    e["TERSE_DEPTH_DIR"] = os.path.join(state, "depth")
    e.update(env or {})
    os.makedirs(state, exist_ok=True)

    stdin = raw if raw is not None else json.dumps(payload)
    p = subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True,
                       text=True, env=e)

    blocked = False
    reason = ""
    if p.stdout.strip():
        try:
            out = json.loads(p.stdout)
            blocked = out.get("decision") == "block"
            reason = out.get("reason") or ""
        except ValueError:
            failures.append("%s: stdout was not JSON: %r" % (name, p.stdout[:200]))
            return
    if p.returncode != 0:
        failures.append("%s: exit %d (a Stop hook must always exit 0)" % (name, p.returncode))
    if blocked != expect_block:
        failures.append("%s: expected block=%s, got %s" % (name, expect_block, blocked))
    if expect_in_reason and expect_in_reason not in reason:
        failures.append("%s: reason missing %r" % (name, expect_in_reason))

    rows = 0
    if os.path.exists(telemetry):
        with open(telemetry, encoding="utf-8") as fh:
            rows = len([ln for ln in fh if ln.strip()])
    if rows != expect_rows:
        failures.append("%s: expected %d telemetry row(s), got %d"
                        % (name, expect_rows, rows))
    return p



t = transcript(os.path.join(tmp, "long.jsonl"))
run("blocks-unearned-long",
    {"session_id": "s1", "transcript_path": t, "last_assistant_message": LONG,
     "cwd": tmp},
    expect_block=True, expect_in_reason="120 words")


run("allows-short",
    {"session_id": "s2", "transcript_path": t, "last_assistant_message": SHORT,
     "cwd": tmp},
    expect_block=False)

run("allows-empty",
    {"session_id": "s3", "transcript_path": t, "last_assistant_message": "",
     "cwd": tmp},
    expect_block=False)


run("does-not-count-fenced-code",
    {"session_id": "s4", "transcript_path": t, "cwd": tmp,
     "last_assistant_message": "Fixed.\n\n```\n%s\n```" % LONG},
    expect_block=False)

t_asked = transcript(os.path.join(tmp, "asked.jsonl"), asked=True)
run("allows-when-a-question-was-asked",
    {"session_id": "s5", "transcript_path": t_asked,
     "last_assistant_message": LONG, "cwd": tmp},
    expect_block=False)

t_failed = transcript(os.path.join(tmp, "failed.jsonl"), failed=True)
run("allows-when-a-tool-failed",
    {"session_id": "s6", "transcript_path": t_failed,
     "last_assistant_message": LONG, "cwd": tmp},
    expect_block=False)

t_report = transcript(os.path.join(tmp, "report.jsonl"),
                      user_text="give me a full report on the sync failure")
run("allows-when-a-report-was-asked-for",
    {"session_id": "s7", "transcript_path": t_report,
     "last_assistant_message": LONG, "cwd": tmp},
    expect_block=False)


run("stands-down-on-stop-hook-active",
    {"session_id": "s8", "transcript_path": t, "last_assistant_message": LONG,
     "cwd": tmp, "stop_hook_active": True},
    expect_block=False)


shared = os.path.join(tmp, "cap")
os.makedirs(shared, exist_ok=True)
cap_env = {"TERSE_GATE_STATE_DIR": os.path.join(shared, "gate")}
run("cap-first-blocks", {"session_id": "s9", "transcript_path": t,
                         "last_assistant_message": LONG, "cwd": tmp},
    expect_block=True, env=cap_env)
run("cap-second-allows", {"session_id": "s9", "transcript_path": t,
                          "last_assistant_message": LONG, "cwd": tmp},
    expect_block=False, env=cap_env)

run("allows-on-malformed-payload", {}, expect_block=False, raw="not json",
    expect_rows=0)
run("allows-on-missing-transcript",
    {"session_id": "s10", "transcript_path": "/nonexistent/x.jsonl",
     "last_assistant_message": LONG, "cwd": tmp},
    expect_block=True)   


loop_dir = os.path.join(tmp, "proj")
os.makedirs(os.path.join(loop_dir, ".claude"), exist_ok=True)
with open(os.path.join(loop_dir, ".claude", "ralph-loop.local.md"), "w") as fh:
    fh.write("---\nactive: true\niteration: 12\n---\nwork\n")

THIRTY = " ".join(["word"] * 30)
deep = os.path.join(loop_dir, ".claude", "worktrees", "x", "src")
os.makedirs(deep, exist_ok=True)
run("in-loop-budget-is-tighter",
    {"session_id": "s11", "transcript_path": t, "last_assistant_message": THIRTY,
     "cwd": deep},
    expect_block=True, expect_in_reason="ONE line")

with open(os.path.join(loop_dir, ".claude", "ralph-loop.local.md"), "w") as fh:
    fh.write("---\nactive: false\n---\nwork\n")
run("inactive-loop-uses-the-normal-budget",
    {"session_id": "s12", "transcript_path": t, "last_assistant_message": THIRTY,
     "cwd": deep},
    expect_block=False)




DRAMA = ("Check" "point. Two findings changed the sha" "pe of this work, and both are "
         "the kind that would have shipped sil" "ently.")
NOTICE = ("One thing worth your ca" "ll when I get there: the lockfile trap argues "
          "for a memory. I will draft it unless you would rat" "her it stay a comment.")
run("blocks-dramatic-framing",
    {"session_id": "s20", "transcript_path": t, "last_assistant_message": DRAMA,
     "cwd": tmp},
    expect_block=True, expect_in_reason="shape of")
run("blocks-advance-notice",
    {"session_id": "s21", "transcript_path": t, "last_assistant_message": NOTICE,
     "cwd": tmp},
    expect_block=True, expect_in_reason="worth your")
run("allows-plain-shape-and-heads-up",
    {"session_id": "s22", "transcript_path": t, "cwd": tmp,
     "last_assistant_message": "Fixed. The parser checks the shape of the payload; "
                               "the armed heads-up queue is unchanged."},
    expect_block=False)
run("allows-banned-phrase-in-a-code-span",
    {"session_id": "s24", "transcript_path": t, "cwd": tmp,
     "last_assistant_message": "Banned now: `changed the sha" "pe of this work`."},
    expect_block=False)


words_env = {"TERSE_GATE_STATE_DIR": os.path.join(tmp, "words-cap", "gate")}
run("banned-first-turn-blocks",
    {"session_id": "s23", "transcript_path": t, "last_assistant_message": DRAMA,
     "cwd": tmp},
    expect_block=True, env=words_env)
run("banned-second-turn-still-blocks",
    {"session_id": "s23", "transcript_path": t, "last_assistant_message": DRAMA,
     "cwd": tmp},
    expect_block=True, env=words_env)
run("banned-retry-stands-down-on-stop-hook-active",
    {"session_id": "s23", "transcript_path": t, "last_assistant_message": DRAMA,
     "cwd": tmp, "stop_hook_active": True},
    expect_block=False, env=words_env)


tele = os.path.join(tmp, "trend.jsonl")
env = {"TERSE_TELEMETRY": tele, "TERSE_DEPTH_DIR": os.path.join(tmp, "trend-depth"),
       "TERSE_GATE_STATE_DIR": os.path.join(tmp, "trend-gate")}
for i in range(3):
    e = dict(os.environ); e.update(env)
    subprocess.run([sys.executable, HOOK], text=True, capture_output=True, env=e,
                   input=json.dumps({"session_id": "s13", "transcript_path": t,
                                     "last_assistant_message": SHORT, "cwd": tmp}))
rows = [json.loads(x) for x in open(tele, encoding="utf-8") if x.strip()]
if len(rows) != 3:
    failures.append("telemetry: expected 3 rows for 3 passing turns, got %d" % len(rows))
elif [r["depth"] for r in rows] != [1, 2, 3]:
    failures.append("telemetry: depth must count turns, got %s" % [r["depth"] for r in rows])
elif any(r["over"] for r in rows):
    failures.append("telemetry: a passing turn was recorded as over budget")

shutil.rmtree(tmp, ignore_errors=True)

if failures:
    print("FAIL (%d)" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ok — terse-output-gate: all cases pass")
