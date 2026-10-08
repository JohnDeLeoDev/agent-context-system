#!/usr/bin/env python3
"test-block-redundant-read — deny/allow cases for the block-redundant-read hook.\n\nA one-line wrapper on purpose, for the shared cases: they live in hook-test-cases.py and\nthe harness in hook-test-run.py, so twelve hooks share one runner instead of twelve copies\nthat rot apart. Below that, the stateful half the shared runner cannot express.\n\nThat is the dangerous shape here: the hook fails toward DENYING when agent_id is missing,\nso a harness change or a refactor that loses the field would start refusing every\nsubagent's FIRST read. Delegation is the pattern this whole rule exists to encourage, and\nit would break silently, looking like a working guard.\n\nFully hermetic: HOME is redirected at the fixture, so the ledger and the telemetry file are\ncreated under it and the real ones are never read or written."
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.environ.get("HOOKS_DIR") or os.path.expanduser("~/.agent-context/global/hooks"),
                     "block-redundant-read.py")

proc = subprocess.run(
    [sys.executable, os.path.join(HERE, "hook-test-run.py"),
     "--hook", "block-redundant-read.py"] + sys.argv[1:])
if proc.returncode != 0:
    sys.exit(1)

if not os.path.isfile(HOOK):
    print("missing or not executable: %s" % HOOK)
    sys.exit(1)

RROOT = os.path.expanduser("~/.cache/agent-context/redundant-read-test.%d" % os.getpid())
shutil.rmtree(RROOT, ignore_errors=True)
os.makedirs(os.path.join(RROOT, "home"), exist_ok=True)
os.makedirs(os.path.join(RROOT, "src"), exist_ok=True)
SRC = os.path.join(RROOT, "src", "sample.py")
with open(SRC, "w") as fh:
    for i in range(60):
        fh.write("x = %d\n" % i)
SID = "seq-%d" % os.getpid()
rfailed = 0


def rd(label, want, agent="", off=None):
    global rfailed
    tool_input: dict[str, object] = {"file_path": SRC}
    if off:
        tool_input["offset"] = off
        tool_input["limit"] = 10
    payload: dict[str, object] = {"tool_name": "Read", "session_id": SID, "tool_input": tool_input}
    if agent:
        payload["agent_id"] = agent
    env = {**os.environ, "HOME": os.path.join(RROOT, "home")}
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                           capture_output=True, text=True, env=env)
    got = "ALLOW" if proc.returncode == 0 else "DENY"
    if got == want:
        print("  ok   %-6s %s" % (got, label))
    else:
        print("  FAIL want=%s got=%s :: %s" % (want, got, label))
        rfailed = 1


print("--- the duplicate branch itself (untested until 2026-09-05) ---")
rd("orchestrator, 1st whole read", "ALLOW")
rd("orchestrator, 2nd whole read of same file", "DENY")

print("--- policy: a subagent gets its OWN ledger, not an exemption ---")



rd("worker A's 1st read, parent already denied", "ALLOW", "agent-A")



rd("worker A's 2nd read is metered", "DENY", "agent-A")

rd("worker B's 1st read is unaffected by A", "ALLOW", "agent-B")

print("--- a CHANGED file is never a duplicate ---")
time.sleep(1)
with open(SRC, "a") as fh:
    fh.write("x = 61\n")
rd("orchestrator re-reads after an edit", "ALLOW")

print("--- the bounded budget: 2 free, 3-4 nudge, 5th refused ---")
rd("bounded read 1", "ALLOW", off=1)
rd("bounded read 2", "ALLOW", off=11)
rd("bounded read 3 (nudges, still allows)", "ALLOW", off=21)
rd("bounded read 4 (nudges, still allows)", "ALLOW", off=31)
rd("bounded read 5 is refused", "DENY", off=41)

shutil.rmtree(RROOT, ignore_errors=True)
print()
print("ALL PASS" if rfailed == 0 else "FAILURES")
sys.exit(rfailed)
