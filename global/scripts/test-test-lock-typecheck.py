#!/usr/bin/env python3
"Battery for test-lock.py lock's basedpyright gate.\n\nUses throwaway git checkouts under TMPDIR, each with its own minimal\npyrightconfig.json. TEST_LOCK_STATE_DIR isolates the lock manifests used here\nfrom the real ones under ~/.local/state/agent-context/test-locks/.\n\nRun: python3 ~/.claude/scripts/test-test-lock-typecheck.py"
import os
import shutil
import subprocess
import sys
import tempfile

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")


def _pick(store_rel, home_rel):
    path = os.path.join(STORE, "global", store_rel)
    return path if os.path.exists(path) else os.path.expanduser(home_rel)


TOOL = _pick("scripts/test-lock.py", "~/.claude/scripts/test-lock.py")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="test-lock-typecheck-"))

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:800]
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def make_repo(name):
    root = os.path.join(TMP, name)
    os.makedirs(root, exist_ok=True)
    subprocess.run(["git", "init", "-q", root], check=True, capture_output=True)
    write(os.path.join(root, "pyrightconfig.json"), "{}\n")
    return root


def env(extra=None):
    e = dict(os.environ)
    e["TEST_LOCK_STATE_DIR"] = os.path.join(TMP, "locks")
    if extra:
        e.update(extra)
    return e


def run_lock(files, cwd, extra_env=None):
    return subprocess.run([sys.executable, TOOL, "lock"] + files, cwd=cwd,
                           capture_output=True, text=True, timeout=90,
                           env=env(extra_env))


def run_status(root):
    return subprocess.run([sys.executable, TOOL, "status", root], cwd=root,
                           capture_output=True, text=True, timeout=30, env=env())


CLEAN = "def f(x: str) -> str:\n    return x.upper()\n"
BAD = "def f(x: str | None) -> str:\n    return x.upper()\n"


def case_clean_locks():
    root = make_repo("clean")
    f = os.path.join(root, "clean.py")
    write(f, CLEAN)
    proc = run_lock([f], root)
    check("a clean .py file locks", proc.returncode == 0 and "locked" in proc.stdout,
          proc.stdout + proc.stderr)


def case_error_refused():
    root = make_repo("bad")
    f = os.path.join(root, "bad.py")
    write(f, BAD)
    proc = run_lock([f], root)
    check("a file with a None-attribute error is refused (exit != 0)",
          proc.returncode != 0, proc.stdout + proc.stderr)
    check("the basedpyright error line is printed",
          "reportOptionalMemberAccess" in proc.stderr, proc.stderr)
    check("'fix these, then lock' is printed", "fix these, then lock" in proc.stderr,
          proc.stderr)
    status = run_status(root)
    check("the refused file was not recorded as locked",
          "bad.py" not in status.stdout, status.stdout)


def case_mixed():
    root = make_repo("mixed")
    good = os.path.join(root, "good.py")
    bad = os.path.join(root, "bad.py")
    write(good, CLEAN)
    write(bad, BAD)
    proc = run_lock([good, bad], root)
    check("a mixed call exits non-zero", proc.returncode != 0, proc.stdout + proc.stderr)
    check("the clean file in the same call still locks",
          "locked" in proc.stdout and "good.py" in proc.stdout, proc.stdout)
    check("the output says which file was refused", "bad.py" in proc.stderr, proc.stderr)
    status = run_status(root)
    check("status shows the clean file locked", "good.py" in status.stdout, status.stdout)
    check("status does not show the refused file", "bad.py" not in status.stdout,
          status.stdout)


def case_no_basedpyright():
    root = make_repo("nobasedpyright")
    f = os.path.join(root, "clean.py")
    write(f, CLEAN)
    fakehome = os.path.join(TMP, "fakehome")
    os.makedirs(fakehome, exist_ok=True)
    minimal_path = "/opt/homebrew/bin:/usr/bin:/bin"  
    proc = run_lock([f], root, extra_env={"PATH": minimal_path, "HOME": fakehome})
    check("locks anyway when basedpyright is not installed",
          proc.returncode == 0 and "locked" in proc.stdout, proc.stdout + proc.stderr)
    check("prints one warning line about the missing basedpyright",
          "basedpyright" in proc.stderr.lower()
          and ("not " in proc.stderr.lower() or "install" in proc.stderr.lower()),
          proc.stderr)


def case_non_py_unaffected():
    root = make_repo("nonpy")
    f = os.path.join(root, "notes.md")
    write(f, "# notes\nthis is not python\n")
    proc = run_lock([f], root)
    check("a non-.py file locks with no type check involved",
          proc.returncode == 0 and "locked" in proc.stdout, proc.stdout + proc.stderr)


def main():
    print("tool under test: %s" % TOOL)
    case_clean_locks()
    case_error_refused()
    case_mixed()
    case_no_basedpyright()
    case_non_py_unaffected()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)
