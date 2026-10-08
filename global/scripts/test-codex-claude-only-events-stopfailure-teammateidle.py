#!/usr/bin/env python3
'Separate from test-codex-claude-only-events.py because that file is locked.\n\nBoth events have no Codex counterpart (StopFailure fires when a Claude turn ends\non an API error; TeammateIdle is a Claude Code agent-teams concept), so each must\nbe classified claude-only: reported as an informational line in report["codex"]\nand never raise the parity finding.'

import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MATERIALIZER = os.path.join(HERE, "harness-materialize.py")
ADAPTER = os.path.join(HERE, "codex-hook-adapter.py")
PARITY_FINDING = "Codex hook parity has unsupported non-empty Claude events"

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


spec = importlib.util.spec_from_file_location("harness_materialize", MATERIALIZER)
if spec is None or spec.loader is None:
    sys.exit("cannot load %s" % MATERIALIZER)
hm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hm)

scratch_root = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
os.makedirs(scratch_root, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="codex-claude-only-stopfailure-test-", dir=scratch_root)

HANDLER = [{"hooks": [{"type": "command", "command": "/bin/fixture"}]}]


def run_materialize(extra_events):
    hooks = {"Stop": HANDLER}
    for event in extra_events:
        hooks[event] = HANDLER
    home = tempfile.mkdtemp(dir=tmp)
    claude = os.path.join(home, ".claude", "settings.json")
    os.makedirs(os.path.dirname(claude))
    with open(claude, "w", encoding="utf-8") as fh:
        json.dump({"hooks": hooks}, fh)
    recorded = []
    real_finding = hm.finding
    setattr(hm, "finding", recorded.append)
    report = {}
    try:
        returned = hm.materialize_codex_hooks(
            report, home=home, claude_settings=claude,
            codex_hooks=os.path.join(home, ".codex", "hooks.json"),
            adapter=ADAPTER, interpreter=sys.executable)
    finally:
        setattr(hm, "finding", real_finding)
    parity = [f for f in recorded if PARITY_FINDING in f]
    lines = report.get("codex", [])
    return parity, lines, returned


try:
    for event in ("StopFailure", "TeammateIdle"):
        parity, lines, returned = run_materialize([event])
        check(event + " alone raises no parity finding", not parity, str(parity))
        check(event + " alone appears in an informational claude-only line",
              any("claude-only" in line and event in line for line in lines),
              str(lines))
        check(event + " alone is not reported as unsupported",
              not any("unsupported" in line for line in lines), str(lines))
        check(event + " alone is not returned", event not in returned, str(returned))

    parity, lines, returned = run_materialize(["StopFailure", "TeammateIdle"])
    check("both together raise no parity finding", not parity, str(parity))
    check("both together are not returned",
          "StopFailure" not in returned and "TeammateIdle" not in returned,
          str(returned))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

total = passed + len(failures)
print("test-codex-claude-only-events-stopfailure-teammateidle: %d/%d passed" % (passed, total))
sys.exit(1 if failures else 0)
