#!/usr/bin/env python3
"A hook-test-run run must never delete another battery's fixtures.\n\n  [1] a sibling battery's fixture directory survives a full run\n  [2] a fresh run-* directory (another live run) survives\n  [3] a run-* directory untouched for two days (a killed run) is swept\n  [4] the run exits 0 and leaves no directory of its own behind\n\nUsage: test-hook-test-run-fixtures.py"

import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "hook-test-run.py")
FIXTURES = os.path.expanduser("~/.cache/hook-test-fixtures")

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


def main():
    os.makedirs(FIXTURES, exist_ok=True)
    sibling = tempfile.mkdtemp(prefix="sibling-battery-", dir=FIXTURES)
    with open(os.path.join(sibling, "repo-marker"), "w", encoding="utf-8") as fh:
        fh.write("a live fixture of another battery\n")
    live_run = tempfile.mkdtemp(prefix="run-", dir=FIXTURES)
    stale_run = tempfile.mkdtemp(prefix="run-", dir=FIXTURES)
    two_days_ago = time.time() - 2 * 86400
    os.utime(stale_run, (two_days_ago, two_days_ago))
    before = set(os.listdir(FIXTURES))
    try:
        print("running a full hook-test-run (about 30 s)")
        try:
            r = subprocess.run([sys.executable, RUNNER], capture_output=True, text=True,
                               timeout=240, stdin=subprocess.DEVNULL)
            rc, tail = r.returncode, (r.stdout + r.stderr)[-400:]
        except subprocess.TimeoutExpired:
            rc, tail = None, "timed out after 240 s"
        after = set(os.listdir(FIXTURES)) if os.path.isdir(FIXTURES) else set()

        print("[1] a sibling battery's fixture survives")
        check("the sibling directory and its file still exist",
              os.path.isfile(os.path.join(sibling, "repo-marker")))
        print("[2] another live run's directory survives")
        check("a fresh run-* directory still exists", os.path.isdir(live_run))
        print("[3] a killed run's directory is swept")
        check("a run-* directory untouched for two days is gone", not os.path.isdir(stale_run))
        print("[4] the run is clean")
        check("hook-test-run exits 0", rc == 0, "rc %r: %s" % (rc, tail))
        left = sorted(after - before)
        check("the run leaves no directory of its own behind", not left, repr(left))
    finally:
        for path in (sibling, live_run, stale_run):
            shutil.rmtree(path, ignore_errors=True)
    print("\n%d passed, %d failed" % (passed, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
