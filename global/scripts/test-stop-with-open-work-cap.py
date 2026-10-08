#!/usr/bin/env python3
"Tests for block-stop-with-open-work's continuation cap and open-item feedback.\n\nAnthropic's Opus 5.5 prompting guide (2026-09): name the open items when sending a\nmodel back to work, and stop after two or three automatic continuations so a stuck\nrun ends. The hook counts its own refusals from the Stop hook feedback records the\nharness writes into the transcript as user messages."

import json
import os
import shutil
import subprocess
import sys
import tempfile

HOOK = os.path.expanduser("~/.agent-context/global/hooks/block-stop-with-open-work.py")

BACK = "Parser fixed.\n\nBack to it."
LISTED = ("Parser fixed.\n\nRemaining:\n- migrate the orders endpoint\n"
          "- update its tests\n\nDone for now.")

failures = []
tmp = tempfile.mkdtemp(prefix="stop-open-work-cap-test-")


def transcript(name, user_text, assistant, own=0, other=0):
    path = os.path.join(tmp, name + ".jsonl")
    rows = [{"type": "user", "message": {"content": user_text}}]
    feedback = (["[block-stop-with-open-work.py] BLOCKED by block-stop-with-open-work: x"]
                * own
                + ["[require-structured-questions.py] BLOCKED by require-structured-questions"]
                * other)
    for f in feedback:
        rows.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": assistant}]}})
        rows.append({"type": "user", "message": {
            "content": "Stop hook feedback:\n[hook-dispatch.py Stop]: " + f}})
    rows.append({"type": "assistant", "message": {"content": [
        {"type": "text", "text": assistant}]}})
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def case(name, message, expect_block, user_text="do the thing", own=0, other=0,
         active=False, expect_in_reason=None, path=None):
    payload = {"session_id": name, "last_assistant_message": message, "cwd": tmp,
               "transcript_path": path or transcript(name, user_text, message, own, other),
               "stop_hook_active": active}
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30)
    if p.returncode not in (0, 2):
        failures.append("%s: exit %d" % (name, p.returncode))
    if (p.returncode == 2) != expect_block:
        failures.append("%s: expected block=%s, got exit %d (stderr %r)"
                        % (name, expect_block, p.returncode, p.stderr[:300]))
    if expect_in_reason and expect_in_reason not in p.stderr:
        failures.append("%s: reason missing %r (stderr %r)"
                        % (name, expect_in_reason, p.stderr[:400]))


case("names-the-open-items", LISTED, True,
     expect_in_reason="  - migrate the orders endpoint\n  - update its tests\n")
case("asks-what-blocks", BACK, True, expect_in_reason="say what blocks it")
case("blocks-again-after-one-refusal", BACK, True, own=1, active=True)
case("stands-down-after-two-refusals", BACK, False, own=2, active=True)
case("stands-down-when-another-hook-caused-the-continuation", BACK, False, other=1,
     active=True)
case("stands-down-with-no-transcript", BACK, False, active=True,
     path=os.path.join(tmp, "missing.jsonl"))

shutil.rmtree(tmp, ignore_errors=True)

if failures:
    print("FAIL (%d)" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("stop-with-open-work-cap: all cases pass")
