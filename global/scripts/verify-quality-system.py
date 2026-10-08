#!/usr/bin/env python3
'verify-quality-system: run every free check on the agent quality system, in one\ncommand, and exit non-zero if any of them fails.\n\nPorted from shell in Consolidation Phase 4, matching the original\'s behavior exactly,\nincluding its two known quirks: the server-tests skip line prints even when the step\nargument names a different step, and the hook-batteries count includes files whose\nbasename happens to contain "meta" (nothing in this scripts directory does, so it has\nnever mattered in practice).\n\nUsage:\n  verify-quality-system.py                run every step\n  verify-quality-system.py <step>         re-run just one step\'s label while working on\n                                           it (e.g. invariants, hook-cases, hook-batteries)\n  verify-quality-system.py --help | -h    usage only, runs nothing'
import argparse
import glob
import os
import re
import subprocess
import sys

STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.join(os.path.expanduser("~"), ".agent-context"))
S = f"{STORE}/global/scripts"
FAILED = 0


def _parse_args():
    p = argparse.ArgumentParser(
        prog="verify-quality-system.py",
        description="Run the full set of free checks on the agent quality system.",
    )
    p.add_argument("step", nargs="?", default="",
                    help="re-run just this step's label (e.g. invariants, hook-cases, "
                         "hook-batteries, server-tests); omit to run every step")
    return p.parse_args()


ONLY = _parse_args().step

STEP_LINE_PATTERN = re.compile(r"[0-9]+ (invariants|case|passed|observation)")


def _matches_step_pattern(line):
    return STEP_LINE_PATTERN.search(line) is not None


def step(label, cmd):
    global FAILED
    if ONLY not in ("", label):
        return
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = result.stdout.rstrip("\n")
    rc = result.returncode
    lines = out.split("\n")
    if rc == 0:
        matched = [l for l in lines if _matches_step_pattern(l)]
        line = matched[-1] if matched else ""
        if not line:
            nonempty = [l for l in lines if l.strip() != ""]
            line = nonempty[-1] if nonempty else ""
        if not line:
            line = "(silent — clean)"
        line = line.lstrip(" ")[:64]
        print(f"  \033[32mok\033[0m   {label:<26} {line}")
    else:
        print(f"  \033[31mFAIL\033[0m {label:<26} (exit {rc})")
        for l in lines[-12:]:
            print("       " + l)
        FAILED = 1


print("verify-quality-system — every free check. (eval-run is NOT here: it spends money.)")
print()

PY = sys.executable
step("invariants", [PY, f"{S}/invariant-check.py"])
step("allowlists", [PY, f"{S}/invariant-check.py", "--verify"])
step("hook-cases", [PY, f"{S}/hook-test-run.py"])
step("eval-selftest", [PY, f"{S}/eval-selftest.py"])
step("obs-coverage", [PY, f"{S}/observation-coverage.py"])
step("hook-registration", [PY, f"{S}/hook-registration-probe.py"])
step("store-orphans", [PY, f"{S}/store-orphan-probe.py"])
step("eval-candidates", [PY, f"{S}/eval-case-candidates.py", "--days", "7"])
step("chain-masking", [PY, f"{S}/hook-interaction-report.py", "--selftest"])



if ONLY in ("", "hook-batteries"):
    sh_files = sorted(glob.glob(f"{S}/test-*.sh"))
    py_files = sorted(glob.glob(f"{S}/test-*.py"))
    bad = ""
    for f in sh_files + py_files:
        if f.endswith(".meta.toml"):
            continue
        if not os.path.isfile(f):
            continue
        if f.endswith(".py"):
            rc = subprocess.run([PY, f], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        else:
            rc = subprocess.run(["bash", f], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        if rc != 0:
            bad += " " + os.path.basename(f)
    if not bad:
        count = sum(1 for f in sh_files + py_files if "meta" not in os.path.basename(f))
        print(f"  \033[32mok\033[0m   {'hook-batteries':<26} {count} wrapper(s)")
    else:
        print(f"  \033[31mFAIL\033[0m {'hook-batteries':<26}{bad}")
        FAILED = 1




VENV = f"{STORE}/server/.venv/bin/python"
if os.access(VENV, os.X_OK):
    step("server-tests", [VENV, "-m", "pytest", f"{STORE}/server/tests", "-q"])
else:
    print(f"  \033[33mskip\033[0m {'server-tests':<26} no venv at {VENV}")

print()
if FAILED == 0:
    print("ALL PASS — the mechanism is intact.")
else:
    print("FAILURES above. Nothing here is flaky by design: each is deterministic, so a")
    print("red line is a real regression, not noise to re-run.")
sys.exit(FAILED)
