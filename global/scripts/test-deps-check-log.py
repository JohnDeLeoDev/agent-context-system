#!/usr/bin/env python3
'deps-check.log always matches deps.json.'

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "deps_check_log_under_test", os.environ.get("DEPS_CHECK_PATH") or os.path.join(HERE, "deps-check.py"))
assert _spec is not None and _spec.loader is not None
deps: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deps)

BASE = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-tests")
os.makedirs(BASE, exist_ok=True)
ROOT = tempfile.mkdtemp(dir=BASE, prefix="deps-check-log-")
BIN = os.path.join(ROOT, "bin")
os.makedirs(BIN)

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        failures.append("%s %s" % (label, detail))
        print("FAIL:", label, detail)


def tool(name, body):
    path = os.path.join(BIN, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)


MANIFEST = os.path.join(ROOT, "manifest.toml")
with open(MANIFEST, "w") as fh:
    fh.write('schema = 1\n[machine.box]\nos = "ubuntu"\nroles = ["base"]\n'
             '[role.base]\ntools = ["fake-a"]\n'
             '[tool.fake-a]\ncheck = ["fake-a", "--version"]\nchannel = "manual"\n'
             'fix = "install fake-a"\n')


def run(home, extra=()):
    env = {"HOME": home, "PATH": BIN + os.pathsep + "/usr/bin:/bin"}
    state = os.path.join(home, "state")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = deps.main(["--manifest", MANIFEST, "--machine", "box", "--state-dir", state,
                          "--timeout", "3"] + list(extra), env=env)
    return code, out.getvalue(), state


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


try:
    
    home = tempfile.mkdtemp(dir=ROOT, prefix="home-")
    state = os.path.join(home, "state")
    os.makedirs(state)
    log = os.path.join(state, "deps-check.log")
    with open(log, "w") as fh:
        fh.write("deps-check: box: 1 problem(s)\nfake-a missing on box (role base): install fake-a\n")
    old = time.time() - 2 * 86400
    os.utime(log, (old, old))
    tool("fake-a", 'echo "fake-a 1.2.3"')
    code, out, state = run(home)
    text = read(log)
    check("recorded ok run exits 0", code == 0, "code=%s out=%r" % (code, out))
    check("log no longer holds the old failure", "missing" not in text, repr(text))
    check("log holds this run's verdict", "deps-check: box: all 1 tools ok" in text, repr(text))
    check("log matches what the run printed", text.strip() == out.strip(), "%r vs %r" % (text, out))
    check("log is newer than the old failure", os.path.getmtime(log) > old + 3600)
    check("no temp file left", not os.path.exists(log + ".tmp"))

    
    os.remove(os.path.join(BIN, "fake-a"))
    code, out, state = run(home)
    text = read(log)
    check("recorded failing run exits non-zero", code != 0, "code=%s" % code)
    check("log carries the new failure", "fake-a missing on box" in text, repr(text))

    
    tool("fake-a", 'echo "fake-a 1.2.3"')
    before = read(log)
    code, out, state = run(home, ["--no-record"])
    check("--no-record leaves the log untouched", read(log) == before, repr(read(log)))
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
