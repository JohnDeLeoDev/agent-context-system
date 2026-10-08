#!/usr/bin/env python3
'Battery for check-harness-paths.py, the lint that finds hand-built harness paths.\n\ncheck-harness-paths.py PATH... walks each PATH (a file or a directory of .py files),\nparses every file with ast, and reports each place code builds a harness directory path\nitself instead of calling harness_paths. Output is one `file:line: shape` line per finding,\nthen `N finding(s) in M file(s)`. Exit 0 when clean, 3 when there are findings.\n\nShapes the fixtures pin, each one a way the store built a ~/.claude path:\n  literal     "~/.claude/hooks/x.py", "/Users/x/.claude/scripts"\n  join        os.path.join(HOME, ".claude", "hooks")\n  join-root   os.path.join(HOME, ".claude")\n  concat      HOME + "/.claude/scripts"\n  pathlib     Path.home() / ".claude" / "hooks"\n  fstring     f"{HOME}/.claude/state"\nNot findings: a docstring or comment that names .claude, a file with no .claude, and\nharness_paths.py itself, which is where the construction belongs.\n\nThe last case runs the checker on the six producer scripts and requires zero findings.\n\nRuns on Python 3.8, the system Python on the Synology nodes.\n\nUsage: test-check-harness-paths.py'

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, "check-harness-paths.py")
PRODUCERS = (
    "home-materialize.py",
    "home-settings-sync.py",
    "harness-materialize.py",
    "agents-materialize.py",
    "project-materialize.py",
    "hook-dispatch.py",
)

FIXTURES = {
    "literal.py": 'X = "~/.claude/hooks/x.py"\n',
    "join.py": 'import os\nHOME = "/h"\nH = os.path.join(HOME, ".claude", "hooks")\n',
    "join_root.py": 'import os\nHOME = "/h"\nC = os.path.join(HOME, ".claude")\n',
    "concat.py": 'HOME = "/h"\nS = HOME + "/.claude/scripts"\n',
    "pathlib_shape.py": 'from pathlib import Path\nH = Path.home() / ".claude" / "hooks"\n',
    "fstring.py": 'HOME = "/h"\nS = f"{HOME}/.claude/state"\n',
    "clean.py": "import os\nX = os.path.join(os.getcwd(), 'a')\n",
    "docstring.py": '"""Mentions .claude/hooks in prose only."""\n\ndef f():\n    """Also ~/.claude/scripts here."""\n    return 1\n',
    "comment.py": "# the ~/.claude/hooks dir is a projection\nX = 1\n",
    "harness_paths.py": 'import os\nC = os.path.join(os.path.expanduser("~"), ".claude")\n',
}
FLAGGED = ("literal.py", "join.py", "join_root.py", "concat.py", "pathlib_shape.py", "fstring.py")
NOT_FLAGGED = ("clean.py", "docstring.py", "comment.py", "harness_paths.py")

failures = []


def check(name, cond, detail=""):
    if cond:
        print("ok   " + name)
    else:
        print("FAIL " + name + (": " + detail if detail else ""))
        failures.append(name)


def run(*args):
    return subprocess.run([sys.executable, CHECKER] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


def scratch():
    base = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="check-harness-paths-test-", dir=base)


def test_fixtures():
    root = scratch()
    try:
        for name, body in FIXTURES.items():
            with open(os.path.join(root, name), "w") as f:
                f.write(body)
        out = run(root)
        check("checker exits 3 on findings", out.returncode == 3, "rc=%s %s" % (out.returncode, out.stderr))
        lines = [ln for ln in out.stdout.splitlines() if ":" in ln and ln.split(":")[0].endswith(".py")]
        flagged = {os.path.basename(ln.split(":")[0]) for ln in lines}
        for name in FLAGGED:
            check("flags " + name, name in flagged, out.stdout)
        for name in NOT_FLAGGED:
            check("does not flag " + name, name not in flagged, out.stdout)
        check("summary line", "%d finding(s) in %d file(s)" % (len(lines), len(flagged)) in out.stdout, out.stdout)
        clean_root = scratch()
        try:
            for name in NOT_FLAGGED:
                with open(os.path.join(clean_root, name), "w") as f:
                    f.write(FIXTURES[name])
            out = run(clean_root)
            check("clean tree exits 0", out.returncode == 0, "rc=%s %s" % (out.returncode, out.stdout))
            check("clean tree says 0 findings", "0 finding(s) in 0 file(s)" in out.stdout, out.stdout)
        finally:
            shutil.rmtree(clean_root, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_bad_input():
    out = run(os.path.join(HERE, "no-such-path-anywhere"))
    check("missing path exits 2 with a message", out.returncode == 2 and out.stderr.strip() != "",
          "rc=%s %s" % (out.returncode, out.stderr))
    root = scratch()
    try:
        with open(os.path.join(root, "broken.py"), "w") as f:
            f.write("def (:\n")
        out = run(root)
        check("unparseable file is reported, not a crash",
              out.returncode in (2, 3) and "broken.py" in (out.stdout + out.stderr),
              "rc=%s %s %s" % (out.returncode, out.stdout, out.stderr))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_producers_clean():
    paths = [os.path.join(HERE, name) for name in PRODUCERS]
    out = run(*paths)
    check("the six producers have no hand-built harness paths", out.returncode == 0,
          "rc=%s %s" % (out.returncode, out.stdout[-1500:]))


def main():
    if not os.path.exists(CHECKER):
        check("check-harness-paths.py exists", False, CHECKER)
    else:
        test_fixtures()
        test_bad_input()
        test_producers_clean()
    print("%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
