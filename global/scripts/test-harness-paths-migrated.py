#!/usr/bin/env python3
"Battery for the harness_paths migration of the store's scripts and hooks (chunk 2b).\n\n  1. check-harness-paths.py finds no hand-built harness path in global/scripts or\n     global/hooks (test files skipped by the checker's default rule).\n  2. Every hook that imports harness_paths resolves it: each global/hooks/*.py runs with an\n     empty JSON payload on stdin, and none dies with an import failure that names\n     harness_paths or an undefined `hp`. A hook that loads but takes no action is fine\n     here; a hook that cannot load fails open in the dispatcher and loses its guard\n     without a sound, which is the failure this case exists to catch.\n  3. The same holds from a projected layout: hooks/ and scripts/ side by side in a fresh\n     directory, as ~/.claude has them.\n\nRuns on Python 3.8, the system Python on the Synology nodes.\n\nUsage: test-harness-paths-migrated.py"

import glob
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_HOOKS = os.path.join(os.path.dirname(HERE), "hooks")
CHECKER = os.path.join(HERE, "check-harness-paths.py")
BAD = ("No module named 'harness_paths'", "name 'hp' is not defined")

failures = []


def check(name, cond, detail=""):
    if cond:
        print("ok   " + name)
    else:
        print("FAIL " + name + (": " + detail if detail else ""))
        failures.append(name)


def run_hook(path, cwd):
    try:
        return subprocess.run([sys.executable, path], input="{}", stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, cwd=cwd, timeout=30)
    except subprocess.TimeoutExpired:
        return None


def smoke(hooks_dir, label):
    bad = []
    ran = 0
    for path in sorted(glob.glob(os.path.join(hooks_dir, "*.py"))):
        out = run_hook(path, os.path.dirname(hooks_dir))
        if out is None:
            continue
        ran += 1
        text = out.stderr + out.stdout
        if any(marker in text for marker in BAD):
            bad.append(os.path.basename(path))
    check(label + ": hooks ran", ran > 0, "ran %d" % ran)
    check(label + ": no hook fails to resolve harness_paths", not bad, ", ".join(bad))


def main():
    out = subprocess.run([sys.executable, CHECKER, os.path.join(HERE), STORE_HOOKS], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, universal_newlines=True)
    tail = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr
    check("no hand-built harness path in global/scripts or global/hooks", out.returncode == 0,
          "rc=%s %s" % (out.returncode, tail))
    smoke(STORE_HOOKS, "store layout")
    base = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
    os.makedirs(base, exist_ok=True)
    root = tempfile.mkdtemp(prefix="migrated-test-", dir=base)
    try:
        shutil.copytree(STORE_HOOKS, os.path.join(root, "hooks"),
                        ignore=shutil.ignore_patterns("*.meta.toml", "__pycache__"))
        shutil.copytree(HERE, os.path.join(root, "scripts"),
                        ignore=shutil.ignore_patterns("test-*", "*.meta.toml", "__pycache__"))
        smoke(os.path.join(root, "hooks"), "projected layout")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
