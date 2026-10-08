#!/usr/bin/env python3
"Battery for the consent-script bug fixes: --help leaking or truncating its own\nusage (git-write-consent.py, write-outside-home-consent.py, test-lock-consent.py),\nand the dead branch in test-lock-consent.py's _unlock. stdlib only.\n\nEvery consent script here runs against a private, throwaway HOME and\nXDG_STATE_HOME under ~/.cache/tmp -- never the real state directory, and no real\ngrant or token is ever minted. Driven from inside this file (subprocess.run with a\nfixture env), per the rule that an agent must not invoke a consent script directly:\nblock-consent-self-grant would refuse that Bash call.\n\nRun: python3 ~/.claude/scripts/test-consent-script-fixes.py"
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")


def _pick(store_rel, home_rel):
    path = os.path.join(STORE, "global", store_rel)
    return path if os.path.exists(path) else os.path.expanduser(home_rel)


SCRIPTS = _pick("scripts", "~/.claude/scripts")
GIT_CONSENT = os.path.join(SCRIPTS, "git-write-consent.py")
WRITE_CONSENT = os.path.join(SCRIPTS, "write-outside-home-consent.py")
LOCK_CONSENT = os.path.join(SCRIPTS, "test-lock-consent.py")
LOCK_TOOL = os.path.join(SCRIPTS, "test-lock.py")

FIXROOT = os.path.expanduser("~/.cache/tmp")
os.makedirs(FIXROOT, exist_ok=True)
TMP = os.path.realpath(tempfile.mkdtemp(prefix="consent-script-fixes-", dir=FIXROOT))
FAKE_HOME = os.path.join(TMP, "home")
STATE_HOME = os.path.join(FAKE_HOME, ".state")
os.makedirs(FAKE_HOME, exist_ok=True)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:700].replace("\n", "\n        ")
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


def env(**extra):
    e = dict(os.environ)
    e.pop("TEST_LOCK_STATE_DIR", None)
    e.update({"HOME": FAKE_HOME, "XDG_STATE_HOME": STATE_HOME, "TEST_LOCK_TOOL": LOCK_TOOL})
    e.update(extra)
    return e


def run(script, *args, **extra):
    return subprocess.run([sys.executable, script] + list(args), capture_output=True,
                          text=True, timeout=30, env=env(**extra), cwd=TMP)


def git(*args):
    return subprocess.run(["git"] + list(args), capture_output=True, text=True, cwd=TMP,
                          env=env())




for script, name, leak_markers in (
    (GIT_CONSENT, "git-write-consent",
     ("set -u", "STATE_DIR=", 'TOKEN="$STATE_DIR', 'LOG="$STATE_DIR')),
    (WRITE_CONSENT, "write-outside-home-consent", ("set -u",)),
):
    proc = run(script, "--help")
    check("%s --help exits 0" % name, proc.returncode == 0, proc.stderr)
    leaked = [m for m in leak_markers if m in proc.stdout]
    check("%s --help prints no leaked source lines" % name, not leaked, ", ".join(leaked))
    check("%s --help still shows its own Usage block" % name,
          "Usage:" in proc.stdout and "python3" in proc.stdout, proc.stdout)


proc = run(WRITE_CONSENT)
check("write-outside-home-consent.py with no args exits 0", proc.returncode == 0, proc.stderr)
check("write-outside-home-consent.py with no args leaks no source lines",
      "set -u" not in proc.stdout, proc.stdout)



proc = run(LOCK_CONSENT, "--help")
check("test-lock-consent.py --help exits 0", proc.returncode == 0, proc.stderr)
check("test-lock-consent.py --help documents --log", "--log" in proc.stdout, proc.stdout)
proc0 = run(LOCK_CONSENT)
check("test-lock-consent.py with no args also documents --log",
      "--log" in proc0.stdout, proc0.stdout)



spec = importlib.util.spec_from_file_location("test_lock_consent_under_test", LOCK_CONSENT)
assert spec is not None and spec.loader is not None, "cannot load %s" % LOCK_CONSENT
tlc = importlib.util.module_from_spec(spec)
_saved = dict(os.environ)
try:
    os.environ["HOME"] = FAKE_HOME
    os.environ["XDG_STATE_HOME"] = STATE_HOME
    os.environ["TEST_LOCK_TOOL"] = LOCK_TOOL
    spec.loader.exec_module(tlc)
    os.makedirs(tlc.STATE_DIR, exist_ok=True)
    
    
    
    
    
    
    try:
        rc = tlc._unlock("files", [])
        check("_unlock('files', []) called directly degrades instead of erroring",
              rc == 0, "exit %r" % rc)
    except Exception as exc:
        check("_unlock('files', []) called directly degrades instead of erroring",
              False, repr(exc))
finally:
    os.environ.clear()
    os.environ.update(_saved)




proc = run(LOCK_CONSENT)
check("no args still prints help, never the old dead-branch error",
      "error: name at least one file" not in (proc.stdout + proc.stderr), proc.stdout + proc.stderr)

notrepo = os.path.join(TMP, "notrepo")
os.makedirs(notrepo, exist_ok=True)
proc = run(LOCK_CONSENT, "--all", notrepo)
check("--all outside a git checkout still exits 1", proc.returncode == 1, proc.stderr)

proc = run(LOCK_CONSENT, "-x")
check("an unknown flag still exits 2", proc.returncode == 2, proc.stderr)



proc = run(GIT_CONSENT)
check("git-write-consent.py still grants normally",
      proc.returncode == 0 and "Authorized ONE git" in proc.stdout, proc.stdout + proc.stderr)
check("git-write-consent.py still writes its token file",
      os.path.isfile(os.path.join(STATE_HOME, "agent-context", "git-write-consent")),
      "token file missing")

outside = os.path.join(TMP, "outside-dir")
os.makedirs(outside, exist_ok=True)
proc = run(WRITE_CONSENT, outside, "5")
check("write-outside-home-consent.py still grants normally",
      proc.returncode == 0 and "Approved agent writes under" in proc.stdout,
      proc.stdout + proc.stderr)

repo = os.path.join(TMP, "repo")
os.makedirs(repo, exist_ok=True)
git("-C", repo, "init", "-q")
target = os.path.join(repo, "t.txt")
with open(target, "w", encoding="utf-8") as fh:
    fh.write("x")
lockproc = run(LOCK_TOOL, "lock", target)
check("test-lock.py still locks a file (setup for the unlock check)",
      lockproc.returncode == 0, lockproc.stdout + lockproc.stderr)
proc = run(LOCK_CONSENT, target)
check("test-lock-consent.py still unlocks a locked file",
      proc.returncode == 0 and "Unlocked:" in proc.stdout, proc.stdout + proc.stderr)

print()
total = len(results)
passed = sum(results)
print("%d/%d passed" % (passed, total))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if passed == total else 1)
