#!/usr/bin/env python3
'Each case is an input the review showed the first version got wrong. Same two-host\nsimulation as test-test-lock-shared.py: two lock state dirs sharing one store.\n\nRun: python3 ~/.agent-context/global/scripts/test-test-lock-shared-edges.py'
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REAL_STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
SCRIPTS = os.path.join(REAL_STORE, "global", "scripts")
LOCK_TOOL = os.path.join(SCRIPTS, "test-lock.py")
EDIT_HOOK = os.path.join(REAL_STORE, "global", "hooks", "block-locked-test-edit.py")

TMP = os.path.realpath(tempfile.mkdtemp(prefix="test-lock-edges-"))
STORE = os.path.join(TMP, "store")
RECORDS = os.path.join(STORE, "global", "test-locks")
ORIGINAL = "def test_x():\n    assert 0\n"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:600]
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)


def env(host):
    e = dict(os.environ)
    e.update({"TEST_LOCK_STATE_DIR": os.path.join(TMP, "host-" + host, "locks"),
              "XDG_STATE_HOME": os.path.join(TMP, "host-" + host, "state"),
              "TEST_LOCK_STORE_ROOT": STORE, "AGENT_CONTEXT_STORE": STORE,
              "TEST_LOCK_TOOL": LOCK_TOOL,
              "SHELL_COMMAND_SCAN": os.path.join(SCRIPTS, "shell-command-scan.py")})
    return e


def run(argv, host, stdin=None):
    return subprocess.run([sys.executable] + argv, input=stdin, capture_output=True,
                          text=True, env=env(host), cwd=TMP, timeout=120)


def edit_hook(host, payload):
    return run([EDIT_HOOK], host, stdin=json.dumps(payload))


def write_payload(path, cwd=STORE):
    return {"tool_name": "Write", "cwd": cwd, "tool_input": {"file_path": path, "content": "x"}}


def mcp(tool, **tool_input):
    return {"tool_name": "mcp__agent-context__" + tool, "cwd": STORE, "tool_input": tool_input}


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    test_x = os.path.join(STORE, "global", "scripts", "test-x.py")
    under = os.path.join(STORE, "global", "scripts", "test__lock.py")
    nested = os.path.join(STORE, "global", "scripts", "test", "lock.py")
    other_x = os.path.join(STORE, "projects", "Other", "scripts", "test-x.py")
    for p in (test_x, under, nested, other_x):
        write(p, ORIGINAL)
    git(STORE, "init", "-q")

    p = run([LOCK_TOOL, "lock", test_x, under, nested], "a")
    check("setup: three store tests lock", p.returncode == 0, p.stdout + p.stderr)

    
    
    wt = os.path.join(STORE, ".agents", "worktrees", "wt")
    w = git(STORE, "worktree", "add", "-q", "--orphan", "-b", "wt", wt)
    write(os.path.join(wt, "global", "scripts", "test-x.py"), ORIGINAL)
    check("setup: a store worktree exists", os.path.isfile(os.path.join(wt, ".git")),
          w.stderr)
    p = edit_hook("b", write_payload(os.path.join(wt, "global", "scripts", "test-x.py"), wt))
    check("a Write to a store worktree's copy of a locked test is refused",
          p.returncode == 2, "rc=%s err=%s" % (p.returncode, p.stderr))
    p = edit_hook("b", {"tool_name": "Bash", "cwd": wt, "tool_input": {
        "command": "echo pass > global/scripts/test-x.py"}})
    check("a shell write to the worktree copy is refused", p.returncode == 2, p.stderr)

    
    names = glob.glob(os.path.join(RECORDS, "*.json"))
    check("record names do not collide: three locks, three records", len(names) == 3,
          str([os.path.basename(n) for n in names]))
    for path in (under, nested):
        p = edit_hook("b", write_payload(path))
        check("host B refuses %s" % os.path.relpath(path, STORE), p.returncode == 2, p.stderr)

    
    bad = os.path.join(STORE, "global", "scripts", "test-typed.py")
    write(bad, "def f() -> int:\n    return 'not an int'\n")
    rel = "global/scripts/test-typed.py"
    os.makedirs(RECORDS, exist_ok=True)
    rec = {"path": rel, "sha256": sha(bad), "locked_at": "2026-09-14T00:00:00Z"}
    run([LOCK_TOOL, "lock", test_x], "a")  
    existing = [n for n in glob.glob(os.path.join(RECORDS, "*.json"))]
    write(os.path.join(RECORDS, "seed-typed.json"), json.dumps(rec))
    p = run([LOCK_TOOL, "status", STORE], "a")
    listed = rel in p.stdout
    p = run([LOCK_TOOL, "lock", bad], "a")
    check("re-locking an unchanged locked file exits 0 even when it has type errors",
          listed and p.returncode == 0, "listed=%s rc=%s err=%s" % (listed, p.returncode,
                                                                    p.stderr[:300]))
    del existing

    
    outside = os.path.join(TMP, "outside.py")
    write(outside, ORIGINAL)
    write(os.path.join(RECORDS, "escape.json"),
          json.dumps({"path": "../outside.py", "sha256": sha(outside), "locked_at": ""}))
    write(os.path.join(RECORDS, "corrupt.json"), "{not json")
    p = run([LOCK_TOOL, "status", STORE], "b")
    check("status does not list a record that escapes the store",
          "outside.py" not in p.stdout.replace("escape.json", ""), p.stdout)
    check("status reports the escaping and the corrupt record and exits 1",
          p.returncode == 1 and "escape.json" in p.stdout + p.stderr
          and "corrupt.json" in p.stdout + p.stderr, "rc=%s out=%s err=%s"
          % (p.returncode, p.stdout, p.stderr))
    p = edit_hook("b", write_payload(outside, TMP))
    check("a file outside the store named by a bad record is not locked", p.returncode == 0,
          p.stderr)
    os.remove(os.path.join(RECORDS, "escape.json"))
    os.remove(os.path.join(RECORDS, "corrupt.json"))

    
    
    
    
    p = edit_hook("b", mcp("edit_body", kind="script", key="test-x", project="../global",
                           old_string="0", new_string="1"))
    check("an MCP payload is not handled here any more (the server refuses it)",
          p.returncode == 0, p.stderr)
    p = edit_hook("b", mcp("bulk_edit", project="Missing", edits=[
        {"kind": "script", "key": "test-x", "replacements": [["0", "1"]]}]))
    check("bulk_edit is not handled here any more either", p.returncode == 0, p.stderr)
    p = edit_hook("b", mcp("bulk_edit", project="Other", edits=[
        {"kind": "script", "key": "test-x", "replacements": [["0", "1"]]}]))
    check("bulk_edit with a top-level project that has its own unlocked script is allowed",
          p.returncode == 0, p.stderr)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = results.count(False)
    print("\n%d passed, %d failed" % (results.count(True), failed))
    sys.exit(1 if failed else 0)
