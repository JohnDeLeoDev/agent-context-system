#!/usr/bin/env python3
'Battery for hook-test-run.py and verify-quality-system.py\'s flag parsing.\n\nTHE FAULT. Neither script parses its own argv. hook-test-run.py\'s --hook/--hooks-dir/\n--verbose/--via-dispatch checks are plain `in args` membership tests with no --help\ncase and no rejection of an unrecognized flag, so `hook-test-run.py --help` falls\nthrough to `only = None` and runs every hook\'s full battery -- asking the script what\nit does makes it do the expensive thing. verify-quality-system.py takes sys.argv[1]\nas a step label with zero validation, so `--help` and `--bogus` are both silently\ntreated as "no step matched", every check is skipped, and the script prints\n"ALL PASS -- the mechanism is intact" having verified nothing at all.\n\nEach case runs the real script under a wall-clock cap, so a pre-fix run that free-runs\nthe full battery is caught by the timeout rather than left to finish.\n\nRun: python3 test-runner-flag-parsing.py'
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK_RUNNER = os.path.join(HERE, "hook-test-run.py")
VERIFY = os.path.join(HERE, "verify-quality-system.py")




HOOK_RUNNER_CASE_MARKERS = ("hook-test-run:", " ok (", " FAIL", "case(s),")
VERIFY_CASE_MARKERS = ("every free check", "ALL PASS", "FAILURES above",
                        "\033[32mok\033[0m", "\033[31mFAIL\033[0m", "\033[33mskip\033[0m")

CAP = 8.0          
FAST = 2.0         

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if detail and not cond else ""))


def run_capped(script, args, cap=CAP):
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, script] + list(args),
                           capture_output=True, text=True, timeout=cap)
        return p.returncode, p.stdout, p.stderr, time.time() - t0, False
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"")
        err = (exc.stderr or b"")
        out = out.decode() if isinstance(out, bytes) else out
        err = err.decode() if isinstance(err, bytes) else err
        return None, out, err, time.time() - t0, True


def no_markers(text, markers):
    return [m for m in markers if m in text]


def check_help_like(script, flag, case_markers, label):
    rc, out, err, elapsed, timed_out = run_capped(script, [flag])
    blob = out + err
    check("%s %s: exits 0" % (label, flag), rc == 0,
          "rc=%r timed_out=%s" % (rc, timed_out))
    check("%s %s: returns in under %ss" % (label, flag, FAST), elapsed < FAST,
          "elapsed=%.2fs" % elapsed)
    check("%s %s: prints usage text" % (label, flag), "usage" in blob.lower(),
          "out=%r err=%r" % (out[:200], err[:200]))
    hit = no_markers(blob, case_markers)
    check("%s %s: runs no case" % (label, flag), not hit, "found markers %r" % hit)


def check_bogus(script, case_markers, label):
    rc, out, err, elapsed, timed_out = run_capped(script, ["--bogus"])
    blob = out + err
    check("%s --bogus: exits 2" % label, rc == 2,
          "rc=%r timed_out=%s" % (rc, timed_out))
    check("%s --bogus: returns in under %ss" % (label, FAST), elapsed < FAST,
          "elapsed=%.2fs" % elapsed)
    check("%s --bogus: prints a usage error" % label,
          "usage" in blob.lower() and "--bogus" in blob,
          "out=%r err=%r" % (out[:200], err[:200]))
    hit = no_markers(blob, case_markers)
    check("%s --bogus: runs no case" % label, not hit, "found markers %r" % hit)


def main():
    print("hook-test-run.py / verify-quality-system.py flag parsing\n")

    for flag in ("--help", "-h"):
        check_help_like(HOOK_RUNNER, flag, HOOK_RUNNER_CASE_MARKERS, "hook-test-run.py")
    check_bogus(HOOK_RUNNER, HOOK_RUNNER_CASE_MARKERS, "hook-test-run.py")

    for flag in ("--help", "-h"):
        check_help_like(VERIFY, flag, VERIFY_CASE_MARKERS, "verify-quality-system.py")
    check_bogus(VERIFY, VERIFY_CASE_MARKERS, "verify-quality-system.py")

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
