#!/usr/bin/env python3
'remind-critical-rules cites only sections that exist.\n\nThe hook is a pointer (it must never restate a rule), so its whole value is that the section\nnames it cites can be found. The global instruction once had a section "Worktree, deploy and git\nwrites"; it was split into "Worktrees" and "Filesystem and git safety", and the hook, four project\ninstructions and three docs kept citing the old title. This test reads the real headings and\nfails when a cited name is not one of them.\n\nReads the store\'s own files (read-only) and runs the hook as a subprocess.'
import json
import os
import re
import subprocess
import sys

STORE = os.path.expanduser("~/.agent-context")
HOOK = os.path.join(STORE, "global", "hooks", "remind-critical-rules.py")
GLOBAL = os.path.join(STORE, "global", "instructions", "global-agent-instructions.md")
AGENTS = os.path.join(STORE, "AGENTS.md")

STALE_TITLE = "Worktree, deploy and git writes"

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def headings(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return [m.group(2).strip() for m in re.finditer(r"^(#{1,6}) +(.+?) *$", text, re.M)]


def hook_output():
    proc = subprocess.run([sys.executable, HOOK], capture_output=True, text=True, timeout=30)
    return proc, proc.stdout


print("remind-critical-rules: cited sections exist")

proc, out = hook_output()
check("the hook exits 0 and prints one JSON line", proc.returncode == 0 and out.count("\n") == 1,
      "rc=%r out=%r" % (proc.returncode, out))
try:
    payload = json.loads(out)
    spec = payload["hookSpecificOutput"]
    msg = spec["additionalContext"]
    shape_ok = spec["hookEventName"] == "SessionStart"
except (ValueError, KeyError, TypeError):
    msg, shape_ok = "", False
check("the output is a SessionStart additionalContext", shape_ok and bool(msg), "out=%r" % out)

cited = re.findall(r'§"([^"]+)"', msg)
global_heads = headings(GLOBAL)
agents_heads = [h.split(" (")[0] for h in headings(AGENTS)]

check("it cites the Worktrees section", "Worktrees" in cited, "cited=%r" % cited)
check("it cites the Filesystem and git safety section", "Filesystem and git safety" in cited,
      "cited=%r" % cited)
check("it cites the AGENTS.md Critical rules section", "Critical rules" in cited, "cited=%r" % cited)

for name in cited:
    where = agents_heads if name == "Critical rules" else global_heads
    check("the cited section %r is a real heading" % name, name in where,
          "headings=%r" % where)

check("the stale section title is gone from the output", STALE_TITLE not in msg)
with open(HOOK, encoding="utf-8") as fh:
    source = fh.read()
check("the stale section title is gone from the hook source", STALE_TITLE not in source)


for phrase in ("Standing orders still apply", "worktree mandate", "deploy gate", "git-write limits",
               "Global Agent Instructions", "AGENTS.md", "get_instructions()",
               "before editing source, deploying, or writing git"):
    check("the pointer still says %r" % phrase, phrase in msg)
check("the pointer restates no rule (it stays short)", len(msg) < 400, "len=%d" % len(msg))

print("\nremind-critical-rules: %d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
