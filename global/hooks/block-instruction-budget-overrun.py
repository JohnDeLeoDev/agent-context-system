#!/usr/bin/env python3

'Refuse an instruction write that pushes the always-loaded layer past budget.\n\nWhy. The always-loaded instruction layer is the one thing every session on every\nmachine pays for, in full, before it does anything. `check_integrity` reports an\noverage only when an audit runs. Without a check at the moment of writing, no\nsingle write is the problem and the sum is nobody\'s job, so the layer grows past\nits budget one reasonable edit at a time. This hook checks the sum at the only\nmoment a write can be declined cheaply.\n\nWhy a hook and no server check. The obvious home is `upsert_instruction` itself.\nEditing the store server means a dirty `server/` tree, which halts this machine\'s\nsync and its self-redeploy until a release is cut. A PreToolUse gate needs none\nof that and refuses at the same moment.\n\nWhat it costs to be wrong. A false positive blocks a legitimate instruction\nedit, which is annoying but never destructive, and the message says what to cut\nand by how much. A false negative taxes every session on the fleet. So this fails\nclosed on a computable overrun and open on anything it cannot compute: an\nunreadable directory, a scope it cannot resolve, a payload shape it does not\nrecognize.\n\nWhat counts. The global layer is every instruction the\nsession loads: the always-loaded instruction files plus AGENTS.md and the user\'s\nCLAUDE.md stub, against one budget owned by `context_budget.py`.\n\nNot covered: a write that shrinks an already-over-budget layer.\nRefusing those would make an over-budget scope unfixable, which is how a guard\ngets switched off. Any write that reduces the total is allowed regardless.\n\nServer task (policy). This hook asks the daemon for the numbers, through\n`store_task.run("context_budget", ...)`. That runs on every host, relay included,\nwith no local store checkout.'

import json
import os
import sys
TYPE_CHECKING = False  
if TYPE_CHECKING:
    from typing import NoReturn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import store_task



BUDGET_FALLBACK = int(os.environ.get("INSTRUCTION_BUDGET_BYTES") or "12000")


def allow() -> "NoReturn":
    sys.exit(0)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    if not isinstance(payload, dict):
        allow()

    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        allow()

    
    if tool.endswith("edit_body") and ti.get("kind") != "instruction":
        allow()

    
    
    
    project = ti.get("project") or ti.get("workspace")
    is_workspace = bool(ti.get("workspace") and not ti.get("project"))
    argv = ["--json", "--scope-report"]
    if project:
        argv += ["--workspace" if is_workspace else "--project", str(project)]

    try:
        result = store_task.run("context_budget", argv, deadline=30)
    except Exception:
        allow()                       
    if not isinstance(result, dict) or result.get("exit") != 0:
        allow()
    try:
        report = json.loads(result.get("stdout") or "")
    except ValueError:
        allow()
    if not isinstance(report, dict):
        allow()

    sizes = report.get("sizes") or {}
    if not sizes:
        allow()                       
    budget = report.get("budget", BUDGET_FALLBACK)
    fixed = 0 if project else report.get("fixed_bytes", 0)

    current = sum((info or {}).get("bytes", 0) for info in sizes.values()) + fixed

    
    
    
    
    
    name = ti.get("name") or ti.get("key") or ""
    title = ti.get("title") or ""
    fname = None
    for cand, info in sizes.items():
        if cand[:-3] == name or (title and (info or {}).get("title") == title):
            fname = cand
            break

    if tool.endswith("upsert_instruction"):
        body = ti.get("body") or ti.get("instruction_body") or ""
        if not isinstance(body, str) or not body:
            allow()                   
        old_size = (sizes.get(fname) or {}).get("bytes", 0)
        new_total = current - old_size + len(body.encode("utf-8"))
    elif tool.endswith("edit_body"):
        old = ti.get("old_string")
        new = ti.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            allow()
        delta = len(new.encode("utf-8")) - len(old.encode("utf-8"))
        new_total = current + delta
    else:
        allow()

    
    
    if new_total <= current or new_total <= budget:
        allow()

    over = new_total - budget
    scope_label = "global" if not project else str(project)
    msg = (
        "BLOCKED by block-instruction-budget-overrun: this write puts the always-loaded "
        "instruction layer for `%s` at %d bytes, %d over the %d budget.\n"
        "Do one of:\n"
        "  1. Cut %d bytes elsewhere in the layer first; move rationale to a doc and leave a pointer.\n"
        "  2. Make the new material a lazy instruction or a doc.\n"
        "  3. Ask user for a budget decision.\n"
        "A write that shrinks the layer is never blocked."
    ) % (scope_label, new_total, over, budget, over)

    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": msg}}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        allow()
