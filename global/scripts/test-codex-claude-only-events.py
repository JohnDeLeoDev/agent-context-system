#!/usr/bin/env python3
'Tests for the Claude-only event table in harness-materialize.py.\n\nClaude events with no Codex counterpart that carry a written reason (WorktreeCreate:\nClaude-created worktrees are placed by the WorktreeCreate hook; Codex has no such\nevent) are reported as an informational line in report["codex"] and raise no finding.\nAny other unsupported event still raises the parity finding. materialize_codex_hooks\nreturns only the unsupported events that raised findings.'

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
tmp = tempfile.mkdtemp(prefix="codex-claude-only-test-", dir=scratch_root)

HANDLER = [{"hooks": [{"type": "command", "command": "/bin/fixture"}]}]


def run_materialize(extra_events):
    'Materialize a graph holding one supported event plus extra_events.'
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
    parity, lines, returned = run_materialize([])
    check("supported graph: no parity finding", not parity, str(parity))
    check("supported graph: nothing returned", not returned, str(returned))
    check("supported graph: no claude-only line",
          not any("claude-only" in line for line in lines), str(lines))

    parity, lines, returned = run_materialize(["WorktreeCreate"])
    check("WorktreeCreate alone raises no parity finding", not parity, str(parity))
    check("WorktreeCreate alone appears in an informational claude-only line",
          any("claude-only" in line and "WorktreeCreate" in line for line in lines),
          str(lines))
    check("WorktreeCreate alone is not reported as unsupported",
          not any("unsupported" in line for line in lines), str(lines))
    check("WorktreeCreate alone is not returned", "WorktreeCreate" not in returned,
          str(returned))

    parity, lines, returned = run_materialize(["MadeUpFutureEvent"])
    check("unknown event raises the parity finding",
          len(parity) == 1 and "MadeUpFutureEvent" in parity[0], str(parity))
    check("unknown event appears in the report",
          any("MadeUpFutureEvent" in line for line in lines), str(lines))
    check("unknown event is returned", list(returned) == ["MadeUpFutureEvent"],
          str(returned))

    parity, lines, returned = run_materialize(["WorktreeCreate", "MadeUpFutureEvent"])
    check("mixed graph raises one parity finding naming only the unknown event",
          len(parity) == 1 and "MadeUpFutureEvent" in parity[0]
          and "WorktreeCreate" not in parity[0], str(parity))
    check("mixed graph returns only the unknown event",
          list(returned) == ["MadeUpFutureEvent"], str(returned))
    check("mixed graph still lists WorktreeCreate as claude-only",
          any("claude-only" in line and "WorktreeCreate" in line for line in lines),
          str(lines))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

total = passed + len(failures)
print("test-codex-claude-only-events: %d/%d passed" % (passed, total))
sys.exit(1 if failures else 0)
