#!/usr/bin/env python3
"Regression cases from the review of deps-check.py's check_any key.\n\n- A command name with a trailing line break passed the name pattern (`$` matches before a final\n  newline) and read as `missing` at run time. It is now a validation error, for `check` too.\n- `check_any` moves to the next alternative only when a command is not on PATH. A command that\n  is found and fails its `expect` text decides, like a found command that exits nonzero.\n- `check_any` holds at most four commands, so the worst-case run time is bounded.\n\nFake tools are small sh scripts under ~/.cache, because /tmp is mounted noexec on the Synology nodes."

import importlib.util
import os
import shutil
import sys
import tempfile
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("deps_check_review", os.path.join(HERE, "deps-check.py"))
assert _spec is not None and _spec.loader is not None
deps: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deps)

BASE = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-tests")
os.makedirs(BASE, exist_ok=True)
ROOT = tempfile.mkdtemp(dir=BASE, prefix="deps-review-")
BIN = os.path.join(ROOT, "bin")
os.makedirs(BIN)
passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        failures.append("%s %s" % (label, detail))
        print("FAIL:", label, detail)


def tool(name, body):
    path = os.path.join(BIN, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)


def manifest(tool_body):
    return deps.tomllib.loads(
        'schema = 1\n[machine.box]\nos = "ubuntu"\nroles = ["base"]\n[role.base]\ntools = ["t"]\n'
        "[tool.t]\n" + tool_body + '\nchannel = "manual"\nfix = "install t"\n')




for label, body in (("check with a trailing newline", 'check = ["ruff\\n", "--version"]'),
                    ("check_any with a trailing newline", 'check_any = [["ruff\\n"]]'),
                    ("an argument with a newline", 'check = ["ruff", "--version\\n"]'),
                    ("an argument with a carriage return", 'check = ["ruff", "a\\rb"]')):
    errors = deps.validate(manifest(body))
    check("validate rejects: " + label, errors != [], str(errors))
check("a plain name still validates", deps.validate(manifest('check = ["ruff", "--version"]')) == [])



marker = os.path.join(ROOT, "second-ran")
tool("first", 'echo "other output"')
tool("second", 'touch "%s"; echo "needle"' % marker)
env = {"PATH": BIN + os.pathsep + "/usr/bin:/bin"}
report = deps.evaluate(
    manifest('check_any = [["first"], ["second"]]\nexpect = "needle"'), "box", env, timeout=3, home=ROOT)
res = report["tools"]["t"]
check("a found command that fails its expect text decides", res["status"] == "missing", str(res))
check("the next alternative is not run", not os.path.exists(marker))
report = deps.evaluate(
    manifest('check_any = [["absent-command"], ["second"]]\nexpect = "needle"'), "box", env, timeout=3, home=ROOT)
check("a command that is not on PATH still moves on", report["tools"]["t"]["status"] == "ok"
      and os.path.exists(marker), str(report["tools"]["t"]))



four = "[" + ", ".join('["c%d"]' % i for i in range(4)) + "]"
five = "[" + ", ".join('["c%d"]' % i for i in range(5)) + "]"
check("four alternatives validate", deps.validate(manifest("check_any = " + four)) == [])
errors = deps.validate(manifest("check_any = " + five))
check("five alternatives are refused", any("at most 4" in line for line in errors), str(errors))

shutil.rmtree(ROOT, ignore_errors=True)
print("%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
