#!/usr/bin/env python3
"Battery for test-lock.py server_python and its use by `lock` (fleet dependency management, B).\n\npyrightconfig.json points basedpyright at server/.venv, which only the main checkout has. Locking\na test from a store worktree therefore failed to resolve pytest and agent_context, and the\nworkaround was an untracked server/.venv symlink that later blocked store-wt-finish. `lock` now\npasses basedpyright the interpreter of the main checkout's venv, then of the machine venv\n(AGENT_CONTEXT_VENVS or ~/.local/share/agent-context/venvs, subdirectory `server`).\n\nThrowaway git repos live under ~/.cache, because /tmp is mounted noexec on the Synology nodes.\nTEST_LOCK_STATE_DIR keeps the lock manifests out of the real state directory."

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "test-lock.py")
_spec = importlib.util.spec_from_file_location("test_lock_under_test", TOOL)
assert _spec is not None and _spec.loader is not None
tl: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tl)

BASE = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-tests")
os.makedirs(BASE, exist_ok=True)
ROOT = os.path.realpath(tempfile.mkdtemp(dir=BASE, prefix="tl-venv-"))
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]

GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        failures.append("%s %s" % (label, detail))
        print("FAIL:", label, detail)


def git(cwd, *args):
    return subprocess.run(GIT + list(args), cwd=cwd, capture_output=True, text=True, check=True,
                          env=GIT_ENV)


def fake_python(venv_dir):
    path = os.path.join(venv_dir, "bin", "python")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)
    return path


def make_repo(name, server=True):
    root = os.path.join(ROOT, name)
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    if server:
        os.makedirs(os.path.join(root, "server"))
        with open(os.path.join(root, "server", "pyproject.toml"), "w") as fh:
            fh.write('[project]\nname = "x"\n')
    with open(os.path.join(root, "README"), "w") as fh:
        fh.write("x\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def make_worktree(repo, name):
    path = os.path.join(ROOT, name)
    git(repo, "worktree", "add", "-q", path, "-b", name)
    return os.path.realpath(path)


def with_env(**values):
    old = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return old


def restore(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


nowhere = os.path.join(ROOT, "no-machine-venvs")



main = make_repo("main1")
main_py = fake_python(os.path.join(main, "server", ".venv"))
wt = make_worktree(main, "wt1")
old = with_env(AGENT_CONTEXT_VENVS=nowhere)
check("a checkout with its own venv needs no override", tl.server_python(main) is None)
check("a worktree borrows the main checkout's venv",
      tl.server_python(wt) == os.path.realpath(main) + "/server/.venv/bin/python",
      str(tl.server_python(wt)))
restore(old)

main2 = make_repo("main2")
wt2 = make_worktree(main2, "wt2")
machine = os.path.join(ROOT, "machine-venvs")
machine_py = fake_python(os.path.join(machine, "server"))
old = with_env(AGENT_CONTEXT_VENVS=machine)
check("with no main venv the machine venv is used", tl.server_python(wt2) == machine_py,
      str(tl.server_python(wt2)))
fake_python(os.path.join(main2, "server", ".venv"))
check("the main checkout's venv wins over the machine venv",
      tl.server_python(wt2) == os.path.realpath(main2) + "/server/.venv/bin/python")
restore(old)

old = with_env(AGENT_CONTEXT_VENVS=nowhere)
main3 = make_repo("main3")
check("no venv anywhere means no override", tl.server_python(make_worktree(main3, "wt3")) is None)
restore(old)

old = with_env(AGENT_CONTEXT_VENVS=machine)
other = make_repo("other-project", server=False)
check("a repo that is not the store's server tree never borrows the venv",
      tl.server_python(other) is None and tl.server_python(make_worktree(other, "wt4")) is None)
restore(old)

wt5 = make_worktree(main, "wt5")
os.makedirs(os.path.join(wt5, "server", ".venv", "bin"))
fake_python(os.path.join(wt5, "server", ".venv"))
check("a worktree that has its own venv is left to the config", tl.server_python(wt5) is None)



seen = []
real_run = tl.subprocess.run


class Done:
    stdout = "{}"
    stderr = ""
    returncode = 0


def capture(argv, *args, **kwargs):
    seen.append((list(argv), kwargs.get("cwd")))
    return Done()


tl.subprocess.run = capture
try:
    tl.run_basedpyright("bp", "t.py", "/r", "/venv/bin/python")
    tl.run_basedpyright("bp", "t.py", "/r")
finally:
    tl.subprocess.run = real_run
check("the interpreter is passed to basedpyright",
      seen[0][0] == ["bp", "--outputjson", "--pythonpath", "/venv/bin/python", "t.py"], str(seen[0]))
check("without one the command line is unchanged", seen[1][0] == ["bp", "--outputjson", "t.py"], str(seen[1]))
check("it still runs from the checkout root", seen[0][1] == "/r")



bp = tl.resolve_basedpyright()
if bp is None:
    print("note: basedpyright not found; end-to-end cases skipped")
else:
    e2e = make_repo("e2e")
    with open(os.path.join(e2e, "pyrightconfig.json"), "w") as fh:
        fh.write('{"venvPath": "server", "venv": ".venv", "typeCheckingMode": "standard"}\n')
    with open(os.path.join(e2e, "test_uses_dep.py"), "w") as fh:
        fh.write("import venvonlypkg\n\n\ndef test_x():\n    assert venvonlypkg.VALUE == 1\n")
    git(e2e, "add", "-A")
    git(e2e, "commit", "-q", "-m", "test")
    venv = os.path.join(ROOT, "e2e-machine", "server")
    subprocess.run([sys.executable, "-m", "venv", venv], check=True, capture_output=True)
    site = subprocess.run([os.path.join(venv, "bin", "python"), "-c",
                           "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, check=True).stdout.strip()
    os.makedirs(os.path.join(site, "venvonlypkg"))
    with open(os.path.join(site, "venvonlypkg", "__init__.py"), "w") as fh:
        fh.write("VALUE = 1\n")

    def lock_in(worktree, venvs):
        env = dict(os.environ, AGENT_CONTEXT_VENVS=venvs, TEST_LOCK_STATE_DIR=os.path.join(ROOT, "state"))
        return subprocess.run([sys.executable, TOOL, "lock", os.path.join(worktree, "test_uses_dep.py")],
                              capture_output=True, text=True, env=env, cwd=worktree)

    wt_bad = make_worktree(e2e, "wt-bad")
    refused = lock_in(wt_bad, nowhere)
    check("with no venv to borrow the import is unresolved and the lock is refused",
          refused.returncode == 3 and "venvonlypkg" in refused.stderr, refused.stderr[-400:])
    wt_ok = make_worktree(e2e, "wt-ok")
    locked = lock_in(wt_ok, os.path.join(ROOT, "e2e-machine"))
    check("with the machine venv the same test locks, and no server/.venv exists in the worktree",
          locked.returncode == 0 and "locked" in locked.stdout
          and not os.path.exists(os.path.join(wt_ok, "server", ".venv")), locked.stderr[-400:] + locked.stdout)
    check("the worktree stays clean of untracked files after locking",
          git(wt_ok, "status", "--porcelain").stdout.strip() == "", git(wt_ok, "status", "--porcelain").stdout)

shutil.rmtree(ROOT, ignore_errors=True)
print("%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
