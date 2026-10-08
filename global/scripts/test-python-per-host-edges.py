#!/usr/bin/env python3
'test-python-per-host.py covers the approved criteria. These are the breaks the review\nfound afterwards, each reproduced before it became a case:\n\n  - interpreter-is-rendered missed a versioned bare interpreter: a list starting with\n    \'python3.14\', or a command string starting with "python3.14 ".\n  - require-worktree-edit, once it compared resolved paths, resolved a symlinked FILE\n    inside the repo as well, so an edit of link.py whose worktree copy had been read was\n    refused, pointing at the worktree copy of the file link.py points to. Before Phase 2\n    it was redirected to the worktree copy of link.py itself.\n\nInterpreter names are built by concatenation so the invariant does not report this file.\nThe worktree fixture lives under ~/.cache/hook-test-fixtures, outside every temp root the\nhook exempts.\n\nUsage: test-python-per-host-edges.py'

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "require-worktree-edit.py")
PY3 = "pyth" + "on3"
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

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


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def clip(text, size=240):
    return text if len(text) <= size else "..." + text[-size:]


def check_invariant():
    print("[1] interpreter-is-rendered and versioned interpreters")
    tmp = tempfile.mkdtemp(prefix="python-per-host-edges-")
    store = os.path.join(tmp, "store")
    scripts = os.path.join(store, "global", "scripts")
    os.makedirs(scripts)
    os.makedirs(os.path.join(store, "global", "hooks"))
    saved = os.environ.get("AGENT_CONTEXT_STORE")
    os.environ["AGENT_CONTEXT_STORE"] = store
    try:
        path = os.path.join(HERE, "invariant-check.py")
        spec = importlib.util.spec_from_file_location("invariant_check", path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load " + path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        inv = next((i for i in mod.REGISTRY if i.id == "interpreter-is-rendered"), None)
        check("interpreter-is-rendered is registered", inv is not None)
        if inv is None:
            return
        cases = (
            ("a list starting with a versioned bare interpreter",
             "subprocess.run(['" + PY3 + ".14', p])\n", True),
            ("a command string starting with a versioned bare interpreter",
             'cmd = "' + PY3 + '.14 tool.py"\n', True),
            ("unchanged: a list starting with an absolute interpreter",
             'subprocess.run(["/opt/homebrew/bin/' + PY3 + '.14", p])\n', False),
            ("unchanged: a path whose last component is the versioned name",
             'p = os.path.join(home, "bin", "' + PY3 + '.14")\n', False),
        )
        for n, (label, src, want) in enumerate(cases):
            fixture = os.path.join(scripts, "fixture-%d.py" % n)
            put(fixture, src)
            try:
                got = bool(inv.violated(fixture, mod._read(fixture)))
            except Exception as exc:
                check(label, False, "raised %s: %s" % (type(exc).__name__, exc))
                continue
            check(label + (" is reported" if want else " passes"), got == want,
                  "reported %r" % (got,))
    finally:
        if saved is None:
            os.environ.pop("AGENT_CONTEXT_STORE", None)
        else:
            os.environ["AGENT_CONTEXT_STORE"] = saved
        shutil.rmtree(tmp, ignore_errors=True)


def cksum(text):
    return subprocess.run(["cksum"], input=text, capture_output=True, text=True).stdout.split()[0]


def check_file_symlink():
    print("[2] require-worktree-edit and a symlinked file inside the repo")
    base = os.path.join(os.path.expanduser("~"), ".cache", "hook-test-fixtures")
    os.makedirs(base, exist_ok=True)
    root = tempfile.mkdtemp(prefix="python-per-host-edges-wt-", dir=base)
    try:
        proj = os.path.join(root, "proj")
        put(os.path.join(proj, "a.py"), "v1\n")
        os.symlink("a.py", os.path.join(proj, "link.py"))
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "one"],
                     ["worktree", "add", "-q", os.path.join(".claude", "worktrees", "fix-x"),
                      "-b", "fix-x"]):
            subprocess.run(["git", "-C", proj] + args, check=True, capture_output=True,
                           env=GIT_ENV)
        sid = "wt-filelink-%d" % os.getpid()
        state = os.path.join(root, "state")
        put(os.path.join(state, "claims", sid + ".json"),
            json.dumps({"session": sid, "worktree": "fix-x"}))
        home = os.path.join(root, "home")
        os.makedirs(home)
        env = dict(GIT_ENV, AGENT_CONTEXT_STATE_DIR=state, HOME=home,
                   TMPDIR=os.path.join(root, "not-a-temp-root"))
        wt_link = os.path.join(proj, ".claude", "worktrees", "fix-x", "link.py")
        payload = {"tool_name": "Edit", "session_id": sid, "cwd": proj,
                   "tool_input": {"file_path": os.path.join(proj, "link.py"),
                                  "old_string": "v1", "new_string": "v2"}}

        r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True,
                           text=True, timeout=60, env=env)
        check("never read: the refusal names the worktree copy of link.py",
              r.returncode == 2 and wt_link in r.stderr,
              "rc %d, stderr %s" % (r.returncode, clip(r.stderr)))

        ledger = os.path.join(home, ".local", "state", "agent-context", "read-ledger", sid)
        key = cksum(os.path.join(proj, "link.py"))
        put(os.path.join(ledger, key), "")
        put(os.path.join(ledger, key + ".sig." + cksum(wt_link)), "")
        r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True,
                           text=True, timeout=60, env=env)
        try:
            spec = (json.loads(r.stdout).get("hookSpecificOutput") or {})
        except ValueError:
            spec = {}
        moved = (spec.get("updatedInput") or {}).get("file_path")
        check("read: the edit is moved to the worktree copy of link.py",
              spec.get("permissionDecision") == "allow" and moved == wt_link,
              "rc %d, moved to %r, stderr %s" % (r.returncode, moved, clip(r.stderr)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    for section in (check_invariant, check_file_symlink):
        try:
            section()
        except Exception as exc:
            check(section.__name__ + " ran to the end", False,
                  "raised %s: %s" % (type(exc).__name__, exc))
    print("\n%d passed, %d failed" % (passed, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
