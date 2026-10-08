#!/usr/bin/env python3
"block-locked-test-edit: a locked acceptance test is not edited to make code pass.\n\nPreToolUse(Write|Edit|MultiEdit|NotebookEdit|Bash, and the store's edit_body, bulk_edit,\nupsert_script and upsert_hook). The test-first-delivery skill locks a task's tests with\ntest-lock.py once they are agreed and seen failing. This refuses a write to a locked\nfile: Write/Edit by path, Bash by the targets shell-command-scan reads from the command,\nan MCP store write by the script or hook file its kind, key and project name. A write\nmade inside an interpreter is invisible here; locked-test-drift-gate catches it by hash\nwhen the turn ends.\n\nA lock comes off only when user approves: the refusal gives the approval question,\napproval-question grants it, and block-consent-self-grant refuses an agent that\nunlocks by itself.\n\nFails open on any error, because this runs on every write and every shell command."

import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp
import store_task

SCRIPTS = hp.scripts_dir()
LOCK_TOOL = os.environ.get("TEST_LOCK_TOOL") or os.path.join(SCRIPTS, "test-lock.py")
SCAN_TOOL = (os.environ.get("SHELL_COMMAND_SCAN")
             or os.path.join(SCRIPTS, "shell-command-scan.py"))

DENY = """Blocked: %s is a locked acceptance test (locked %s).

The tests were agreed before implementation and seen failing. Change the code so it
passes them. Do not change the test to match the code.

If the test itself is wrong, stop and tell user which assertion is wrong and why,
then ask with one AskUserQuestion. Header "Approval", options "Approve" and "Deny",
and the question:

    Unlock the locked test %s? [approval:test-unlock:%s:0]

The approval-question hook removes the lock when he picks Approve.

Do not route around this with another write path. A changed locked file also stops
the turn from ending.
"""


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _check_live(target):
    ' check live.'
    try:
        result = store_task.run("test-lock", ["check", target])
    except (store_task.store_mcp.StoreUnreachable, store_task.store_mcp.ToolError):
        return None
    if not isinstance(result, dict) or result.get("exit") != 2:
        return None
    rel, _, stamp = (result.get("stdout") or "").strip().partition("\t")
    return (rel, stamp) if rel else None


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    if tool == "Bash":
        cmd = ti.get("command")
        if not isinstance(cmd, str) or not cmd:
            return 0
        targets = load_module(SCAN_TOOL, "shell_command_scan").write_targets(cmd, cwd)
    elif tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = (ti.get("file_path") or ti.get("filePath")
                or ti.get("notebook_path") or ti.get("notebookPath") or "")
        targets = [path] if isinstance(path, str) and path else []
    else:
        
        
        
        
        return 0
    if not targets:
        return 0
    tl = load_module(LOCK_TOOL, "test_lock")
    store = tl.store_root() if hasattr(tl, "store_root") else None
    for target in targets:
        root = None
        if store and hasattr(tl, "resolve_root"):
            root, _key = tl.resolve_root(target, cwd)
        if store and root == store:
            
            
            hit = _check_live(target)
            if hit:
                rel, stamp = hit
                path = os.path.join(store, rel)
                sys.stderr.write(DENY % (rel, stamp, path, path))
                return 2
            continue
        hit = tl.locked_entry(target, cwd)
        if not hit:
            continue
        doc, rel = hit
        entry = doc["files"].get(rel)
        stamp = entry.get("locked_at", "at an unknown time") if isinstance(entry, dict) else "?"
        path = os.path.join(doc["root"], rel)
        sys.stderr.write(DENY % (rel, stamp, path, path))
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("block-locked-test-edit: hook failed, allowing: %r\n" % (exc,))
        sys.exit(0)
