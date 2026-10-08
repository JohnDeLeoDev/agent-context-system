#!/usr/bin/env python3
"test-eval-fixture: an eval fixture must look like a real project clone.\n\nChecks, each against a fresh build_fixture() in a temp dir under $TMPDIR:\n  1. .git/info/exclude carries the agent-context block (.claude/, .agents/).\n  2. the tree is clean and only the case's own files are tracked.\n  3. .claude/scripts/wt-finish.sh, run from a worktree with a commit, lands the branch on\n     main, removes the worktree and deletes the branch (exit 0).\n  4. the same script refuses a worktree with uncommitted changes: non-zero, main\n     unchanged, worktree still there.\n  5. `dirty` files are written after the fixture commit and left uncommitted.\nExit 0 when all pass, 1 otherwise."
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location("eval_run", os.path.join(HERE, "eval-run.py"))
if spec is None or spec.loader is None:
    raise ImportError("cannot load spec for eval_run")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)

failures = []


def check(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name, (" :: " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def fresh():
    root = tempfile.mkdtemp(prefix="eval-fixture-test-")
    ev.build_fixture({"fixture": {"app.py": "VALUE = 1\n"}}, root)
    return root


root = fresh()
excl = open(os.path.join(root, ".git", "info", "exclude"), encoding="utf-8").read()
check("exclude block lists .claude/ and .agents/",
      "agent-context generated (never commit)" in excl
      and ".claude/" in excl.split() and ".agents/" in excl.split(), excl[-300:])

check("tree is clean after build", git(root, "status", "--porcelain").stdout.strip() == "",
      git(root, "status", "--porcelain").stdout)
check("only the case's files are tracked",
      git(root, "ls-files").stdout.split() == ["app.py"], git(root, "ls-files").stdout)

finish = os.path.join(root, ".claude", "scripts", "wt-finish.sh")
check("finish script exists and is executable", os.access(finish, os.X_OK), finish)


wt = os.path.join(root, ".claude", "worktrees", "change")
git(root, "worktree", "add", "-q", wt, "-b", "change")
with open(os.path.join(wt, "app.py"), "w", encoding="utf-8") as fh:
    fh.write("VALUE = 2\n")
git(wt, "commit", "-qam", "change")
tip = git(wt, "rev-parse", "HEAD").stdout.strip()
p = subprocess.run(["bash", finish], cwd=wt, capture_output=True, text=True) if os.path.exists(finish) \
    else subprocess.CompletedProcess([], 127, "", "no finish script")
check("finish exits 0 on a committed worktree", p.returncode == 0, (p.stdout + p.stderr)[-300:])
check("main now holds the worktree commit", git(root, "rev-parse", "main").stdout.strip() == tip)
check("worktree directory removed", not os.path.isdir(wt))
check("branch deleted", git(root, "branch", "--list", "change").stdout.strip() == "")


wt2 = os.path.join(root, ".claude", "worktrees", "dirty")
git(root, "worktree", "add", "-q", wt2, "-b", "dirty")
with open(os.path.join(wt2, "app.py"), "w", encoding="utf-8") as fh:
    fh.write("VALUE = 3\n")
before = git(root, "rev-parse", "main").stdout.strip()
p = subprocess.run(["bash", finish], cwd=wt2, capture_output=True, text=True) if os.path.exists(finish) \
    else subprocess.CompletedProcess([], 0, "", "")
check("finish refuses uncommitted changes", p.returncode != 0, (p.stdout + p.stderr)[-300:])
check("main unchanged after the refusal", git(root, "rev-parse", "main").stdout.strip() == before)
check("dirty worktree left in place", os.path.isdir(wt2))

shutil.rmtree(root, ignore_errors=True)



root = tempfile.mkdtemp(prefix="eval-fixture-test-")
ev.build_fixture({"fixture": {"app.py": "VALUE = 1\n", "rec.json": "{}\n"},
                  "dirty": {"rec.json": '{"a": 1}\n', "new.txt": "draft\n"}}, root)
st = git(root, "status", "--porcelain").stdout
check("dirty files are uncommitted: one modified, one untracked",
      " M rec.json" in st and "?? new.txt" in st, st)
check("the fixture commit holds the clean version",
      git(root, "show", "HEAD:rec.json").stdout == "{}\n"
      and open(os.path.join(root, "rec.json"), encoding="utf-8").read() == '{"a": 1}\n')
shutil.rmtree(root, ignore_errors=True)
print("\n  %d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
