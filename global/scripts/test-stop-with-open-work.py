#!/usr/bin/env python3
"Tests for block-stop-with-open-work.\n\nEvery case drives the hook the way the harness does: a JSON payload on stdin and a\nreal transcript on disk. The hook reads the transcript for three things the final\nmessage cannot show: an AskUserQuestion in the turn, a background task started in\nthe turn, and the user's own prompt."

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List

HOOK = os.path.expanduser("~/.agent-context/global/hooks/block-stop-with-open-work.py")

M4_REMAINING = (
    "Remaining\n\n"
    "73 sites across 28 files, none yet run through a gate: preflight.sh (11), the "
    "ci-run-*.ps1 comments (10), .githooks/pre-commit (4). Then the whole store side: "
    "wt-finish-core.py, 28 prose entities and the test-project-script-fixes.py battery."
    "\n\nI have not run the gate yet. Per your verify-once rule I will run it once when "
    "the edits are complete, not between them.")
M4_BACK = ("Nothing. You already made every call I needed: criteria approved, ci.yml "
           "bootstraps pnpm, both lockfiles, push when green.\n\nBack to it.")

failures = []
tmp = tempfile.mkdtemp(prefix="stop-open-work-test-")


def transcript(name, user_text="do the thing", assistant="ok", asked=False,
               background=False):
    "One prompt, then the assistant's tool calls and text for the turn."
    path = os.path.join(tmp, name + ".jsonl")
    content: List[Dict[str, Any]] = []
    if asked:
        content.append({"type": "tool_use", "name": "AskUserQuestion", "input": {}})
    if background:
        content.append({"type": "tool_use", "name": "Bash",
                        "input": {"command": "python3 run-suite.py",
                                  "run_in_background": True}})
    content.append({"type": "text", "text": assistant})
    rows = [{"type": "user", "message": {"content": user_text}},
            {"type": "assistant", "message": {"content": content}}]
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def run(name, payload, expect_block, expect_in_reason=None, env=None, raw=None):
    e = dict(os.environ)
    e.update(env or {})
    stdin = raw if raw is not None else json.dumps(payload)
    p = subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True,
                       text=True, env=e, timeout=30)
    blocked = p.returncode == 2
    if p.returncode not in (0, 2):
        failures.append("%s: exit %d (a Stop hook exits 0 or 2)" % (name, p.returncode))
    if blocked != expect_block:
        failures.append("%s: expected block=%s, got %s (stderr %r)"
                        % (name, expect_block, blocked, p.stderr[:200]))
    if expect_in_reason and expect_in_reason not in p.stderr:
        failures.append("%s: reason missing %r" % (name, expect_in_reason))


def case(name, message, expect_block, user_text="do the thing", asked=False,
         background=False, extra=None, expect_in_reason=None, env=None):
    t = transcript(name, user_text=user_text, assistant=message, asked=asked,
                   background=background)
    payload = {"session_id": name, "transcript_path": t,
               "last_assistant_message": message, "cwd": tmp}
    payload.update(extra or {})
    run(name, payload, expect_block, expect_in_reason=expect_in_reason, env=env)



case("blocks-remaining-list-with-future-work", M4_REMAINING, True,
     expect_in_reason="Do the work")
case("blocks-back-to-it", M4_BACK, True, user_text="So what do you want me to do?")
case("blocks-future-self-work-without-a-list",
     "Fixed the parser. Next I will update the three callers and rerun the suite.", True)


case("allows-finished-result",
     "Landed d3557c6. pnpm audit: no known vulnerabilities.", False)
case("allows-when-a-question-was-asked", M4_REMAINING, False, asked=True)
case("allows-when-a-background-task-will-wake-it",
     "The verify run is going in the background. I will check the result when it "
     "finishes.", False, background=True)
case("allows-an-answer-to-a-status-question",
     "Remaining: Phase 6c, which you launch, and pc's Go binary when pc wakes.", False,
     user_text="what is left?")
case("allows-a-step-only-user-can-take",
     "Remaining: your login. Run `! gcloud auth login` and I will continue after it.",
     False)
case("allows-a-wait-on-a-sleeping-machine",
     "pc is asleep. I will check its Go binary when pc wakes.", False)


case("stands-down-on-stop-hook-active", M4_BACK, False,
     extra={"stop_hook_active": True})
run("allows-malformed-payload", {}, False, raw="not json")
run("allows-empty-message", {"last_assistant_message": ""}, False)
case("stands-down-when-disabled-for-an-eval-arm", M4_REMAINING, False,
     env={"AGENT_CONTEXT_DISABLE_HOOKS": "block-stop-with-open-work"})

shutil.rmtree(tmp, ignore_errors=True)

if failures:
    print("FAIL (%d)" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("stop-with-open-work: all cases pass")
