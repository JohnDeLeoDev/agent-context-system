#!/usr/bin/env python3
'test-hook-firing-report -- what hook-firing-report counts as a firing.\n\nRun: python3 global/scripts/test-hook-firing-report.py'
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
TMP = os.path.expanduser("~/.cache/agent-context/tests/hook-firing-report.%d" % os.getpid())
failed = 0


def check(label, cond, detail=""):
    global failed
    if cond:
        print("  ok   " + label)
    else:
        failed += 1
        print("  FAIL " + label + ((" :: " + detail[:600]) if detail else ""))


def result(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": text}]}}


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


frame = "/py /u/.agent-context/global/scripts/hook-dispatch.py PreToolUse"
records = [
    
    result('links: ["[[global/hooks/block-agent-file-force-add.py]]", '
           '"[[global/hooks/lsp-failure-tripwire.py]]"]\nupdated_at: "2026-09-20"'),
    
    result('156\tMISSING_SCAN_MSG = """BLOCKED by block-destructive-git.py: %s'),
    result('hooks/block-instruction-budget-overrun.py:173:        "BLOCKED by '
           'block-instruction-budget-overrun.\\n\\n"'),
    
    result("PreToolUse:Bash hook error: [/u/.agent-context/global/hooks/block-git-stash.py]: "
           "Blocked: git stash"),
    result("PreToolUse:Bash hook error: [%s]: [block-destructive-git.py] BLOCKED by "
           "block-destructive-git.py: This command discards work" % frame),
    result("BLOCKED by memory-husk-guard: the body contains a credential"),
    result("Error: BLOCKED by block-instruction-budget-overrun: this write puts the layer over"),
    
    {"type": "attachment", "attachment": {
        "type": "hook_additional_context",
        "content": ["REWROTE by require-resolvable-read-path: `a` -> `/b`"]}},
    {"type": "system", "content": "PreToolUse:Bash hook additional context: REWROTE by "
                                  "require-resolvable-read-path: `a` -> `/b`"},
]

try:
    shutil.rmtree(TMP, ignore_errors=True)
    put(os.path.join(TMP, ".claude", "settings.json"), json.dumps({"hooks": {}}))
    put(os.path.join(TMP, ".claude", "projects", "fixture", "session.jsonl"),
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    proc = subprocess.run([sys.executable, os.path.join(HERE, "hook-firing-report.py")],
                          capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, HOME=TMP, AGENT_CONTEXT_STORE=STORE))
    report = proc.stdout

    def count(name):
        m = re.search(r"^\s+%s\s+(\d+)" % re.escape(name), report, re.M)
        return int(m.group(1)) if m else 0

    print("noise is not a firing")
    check("a [[…hooks/<name>.py]] link in frontmatter is not counted",
          count("block-agent-file-force-add") == 0 and count("lsp-failure-tripwire") == 0, report)
    print("real refusals are counted once each")
    check("a direct harness frame `[<path>]: ` is counted", count("block-git-stash") == 1, report)
    check("a dispatcher refusal is counted once, and a source read of the same guard is not",
          count("block-destructive-git") == 1, report)
    check("a tool result that starts with `BLOCKED by` is counted", count("memory-husk-guard") == 1, report)
    check("an `Error: BLOCKED by` tool error is counted, and a grep of the source is not",
          count("block-instruction-budget-overrun") == 1, report)
    check("a rewrite notice is counted once, not once per stored copy",
          count("require-resolvable-read-path") == 1, report)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\nFAILED: %d" % failed if failed else "\nall passed")
sys.exit(1 if failed else 0)
