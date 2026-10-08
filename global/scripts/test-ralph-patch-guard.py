#!/usr/bin/env python3
"Tests for ralph-patch-guard's PATCH E after file-stat.sh is retired."

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "ralph-patch-guard.py")
STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
OLD_REF = "88b028f9"  
OLD_MARKER = "LOCAL PATCH E (agent-context, 2026-09-04)"
ANCHOR_E = "# Validate numeric fields before arithmetic operations"
UPSTREAM = ("#!/bin/bash\nset -euo pipefail\nHOOK_INPUT=$(cat)\n" + ANCHOR_E + "\n"
            'if [[ ! "$ITERATION" =~ ^[0-9]+$ ]]; then exit 0; fi\n')

tmp = tempfile.mkdtemp(prefix="ralph-patch-guard-test-")
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


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        sys.exit("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def old_module():
    proc = subprocess.run(["git", "-C", STORE, "show",
                           OLD_REF + ":global/hooks/ralph-patch-guard.py"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit("cannot read the 2026-09-04 patcher at %s: %s" % (OLD_REF, proc.stderr.strip()))
    path = os.path.join(tmp, "ralph_patch_guard_old.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(proc.stdout)
    return load("ralph_patch_guard_old", path)


def mtime_probe(text, target):
    'Run the patched `_ralph_mtime` under bash with no file-stat.sh reachable.'
    m = re.search(r"_ralph_mtime\(\) \{.*?\n\}", text, re.S)
    if not m:
        return None
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    proc = subprocess.run(["bash", "-c", m.group(0) + '\n_ralph_mtime "$1"', "probe", target],
                          capture_output=True, text=True, cwd=home,
                          env={"HOME": home, "PATH": "/usr/bin:/bin"})
    return proc.stdout


new = load("ralph_patch_guard_new", HOOK)
old = old_module()

fresh, _applied, _failed = new.apply_patches(UPSTREAM)
old_patched, _a, _f = old.apply_patches(UPSTREAM)
repatched, _a2, _f2 = new.apply_patches(old_patched)
again, _a3, _f3 = new.apply_patches(fresh)

print("ralph-patch-guard PATCH E without file-stat.sh")

check("the old patcher really carried the 2026-09-04 block that sources file-stat.sh",
      OLD_MARKER in old_patched and "file-stat.sh" in old_patched,
      "fixture is not the old block")

check("the E marker is new and cannot match the 2026-09-04 marker by substring",
      new.MARKER_E != OLD_MARKER and new.MARKER_E not in OLD_MARKER
      and OLD_MARKER not in new.MARKER_E,
      "MARKER_E is %r" % new.MARKER_E)

check("a fresh upstream stop-hook gets PATCH E with no file-stat reference",
      new.MARKER_E in fresh and "file-stat" not in fresh,
      "marker present %s, file-stat present %s" % (new.MARKER_E in fresh, "file-stat" in fresh))

check("a stop-hook patched with the 2026-09-04 block is re-patched to exactly one PATCH E",
      repatched.count("LOCAL PATCH E") == 1 and OLD_MARKER not in repatched
      and new.MARKER_E in repatched and "file-stat" not in repatched
      and repatched.count(ANCHOR_E) == 1,
      "PATCH E count %d, old marker %s, file-stat %s, anchor count %d"
      % (repatched.count("LOCAL PATCH E"), OLD_MARKER in repatched,
         "file-stat" in repatched, repatched.count(ANCHOR_E)))

check("re-applying to an already patched stop-hook changes nothing",
      again == fresh, "second apply changed the text")

target = os.path.join(tmp, "transcript.jsonl")
with open(target, "w", encoding="utf-8") as fh:
    fh.write("{}\n")
os.utime(target, (1700000000, 1700000000))
got = mtime_probe(fresh, target)
check("the inline probe prints the epoch mtime with no file-stat.sh on disk",
      got == "1700000000", "printed %r" % (got,))

got_missing = mtime_probe(fresh, os.path.join(tmp, "absent.jsonl"))
check("the inline probe prints nothing for a missing file, never 0",
      got_missing == "", "printed %r" % (got_missing,))

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
