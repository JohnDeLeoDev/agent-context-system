#!/usr/bin/env python3
"Battery for check-harness-paths.py's scope rule.\n\nTest files build fake homes on purpose, and several are locked, so the checker skips them\nunless asked: test-*.py, *-cases.py and wrapper-port-*.py are skipped by default, and\n--include-tests scans them again. A test file named on the command line by hand is still\nscanned only with the flag, so the rule is one rule, not a path-depth special case.\n\nFixtures: one file of each skipped kind plus one ordinary file, all holding one hand-built\npath. Default run: only the ordinary file is reported. With --include-tests: all four.\n\nRuns on Python 3.8, the system Python on the Synology nodes.\n\nUsage: test-check-harness-paths-scope.py"

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, "check-harness-paths.py")
BODY = 'H = "~/.claude/hooks/x.py"\n'
SKIPPED = ("test-foo.py", "hook-test-cases.py", "wrapper-port-cases-p1.py")
ORDINARY = "real.py"

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


def flagged(out):
    names = set()
    for line in out.stdout.splitlines():
        head = line.split(":")[0]
        if head.endswith(".py"):
            names.add(os.path.basename(head))
    return names


def main():
    base = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
    os.makedirs(base, exist_ok=True)
    root = tempfile.mkdtemp(prefix="check-scope-test-", dir=base)
    try:
        for name in SKIPPED + (ORDINARY,):
            with open(os.path.join(root, name), "w") as f:
                f.write(BODY)
        out = run(root)
        got = flagged(out)
        check("default run reports the ordinary file", ORDINARY in got, out.stdout)
        for name in SKIPPED:
            check("default run skips " + name, name not in got, out.stdout)
        check("default run counts one finding", "1 finding(s) in 1 file(s)" in out.stdout, out.stdout)
        out = run("--include-tests", root)
        got = flagged(out)
        for name in SKIPPED + (ORDINARY,):
            check("--include-tests reports " + name, name in got, out.stdout)
        check("--include-tests counts four findings", "4 finding(s) in 4 file(s)" in out.stdout, out.stdout)
        out = run(os.path.join(root, "test-foo.py"))
        check("a skipped file named by hand is still skipped", "test-foo.py" not in flagged(out), out.stdout)
        out = run("--include-tests", os.path.join(root, "test-foo.py"))
        check("a skipped file named by hand is scanned with the flag", "test-foo.py" in flagged(out), out.stdout)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
