#!/usr/bin/env python3
'Each case is an input the review showed the first version got wrong, plus the\nneighbors that must keep passing. Same harness as test-stop-with-open-work.py.'

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List

HOOK = os.path.expanduser("~/.agent-context/global/hooks/block-stop-with-open-work.py")

M4_REMAINING = (
    "Remaining\n\n73 sites across 28 files, none yet run through a gate.\n\n"
    "I have not run the gate yet. I will run it once when the edits are complete.")

failures = []
tmp = tempfile.mkdtemp(prefix="stop-open-work-edges-")


def transcript(name, assistant, tool_uses=None):
    path = os.path.join(tmp, name + ".jsonl")
    content: List[Dict[str, Any]] = list(tool_uses or [])
    content.append({"type": "text", "text": assistant})
    rows = [{"type": "user", "message": {"content": "do the thing"}},
            {"type": "assistant", "message": {"content": content}}]
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def case(name, message, expect_block, tool_uses=None):
    payload = {"session_id": name, "transcript_path": transcript(name, message, tool_uses),
               "last_assistant_message": message, "cwd": tmp}
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30)
    if p.returncode not in (0, 2):
        failures.append("%s: exit %d" % (name, p.returncode))
    if (p.returncode == 2) != expect_block:
        failures.append("%s: expected block=%s, got %s (stderr %r)"
                        % (name, expect_block, p.returncode == 2, p.stderr[:160]))



case("passes-remaining-risk-none", "Remaining risk: none. Landed and verified.", False)
case("passes-remaining-none", "Landed d3557c6.\n\nRemaining: none.", False)
case("passes-report-when-the-run-finishes",
     "Started the suite. I'll report when the run finishes.", False)


case("blocks-continuing-with", "Store side is half done.\n\nContinuing with the store side next.",
     True)
case("blocks-pronoun-dropped-future-work", "Edits are in. Will run the gate after the edits.",
     True)
case("blocks-bulleted-todo", "Progress so far.\n\n- TODO: rewrite the auth module", True)
case("blocks-bulleted-remaining",
     "Progress so far.\n\n- Remaining: rerun the gate on the last 3 files", True)
case("blocks-curly-apostrophe-future-work", "I’ll run the migration next.", True)
case("blocks-curly-apostrophe-not-done", "I haven’t run the gate yet.", True)


case("a-run-in-background-string-false-is-not-a-wake", M4_REMAINING, True,
     tool_uses=[{"type": "tool_use", "name": "Bash",
                 "input": {"command": "ls", "run_in_background": "false"}}])
case("a-real-background-run-is-a-wake", M4_REMAINING, False,
     tool_uses=[{"type": "tool_use", "name": "Bash",
                 "input": {"command": "python3 suite.py", "run_in_background": True}}])

shutil.rmtree(tmp, ignore_errors=True)

if failures:
    print("FAIL (%d)" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("stop-with-open-work edges: all cases pass")
