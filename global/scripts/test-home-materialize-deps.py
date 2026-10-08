#!/usr/bin/env python3
'Tests for spawn_deps_check in home-materialize.py (fleet dependency management, phase A).\n\nSession start must not wait on the dependency checker. These cases pin that the spawn is gated\nby the age of deps.json, detached, silent, and unaffected by a checker that is slow, missing or\ncrashing. Fake checkers are small Python scripts in a directory under ~/.cache, because /tmp is\nmounted noexec on the Synology nodes.'

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
_spec = importlib.util.spec_from_file_location("home_materialize_under_test",
                                               os.path.join(HERE, "home-materialize.py"))
assert _spec is not None and _spec.loader is not None
hm: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hm)

BASE = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-tests")
os.makedirs(BASE, exist_ok=True)
ROOT = tempfile.mkdtemp(dir=BASE, prefix="hm-deps-")
passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        failures.append("%s %s" % (label, detail))
        print("FAIL:", label, detail)


def fresh_state():
    return tempfile.mkdtemp(dir=ROOT, prefix="state-")


def checker(body):
    path = os.path.join(tempfile.mkdtemp(dir=ROOT, prefix="script-"), "deps-check.py")
    with open(path, "w") as fh:
        fh.write(body)
    return path


def call(**kwargs):
    '(result, stdout, stderr, seconds) of one spawn_deps_check call.'
    out, err = io.StringIO(), io.StringIO()
    began = time.monotonic()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = hm.spawn_deps_check(**kwargs)
    return result, out.getvalue(), err.getvalue(), time.monotonic() - began


def wait_for(path, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return False




launched = []


def fake_popen(argv, **kwargs):
    launched.append((argv, kwargs))


script = checker("print('x')\n")

state = fresh_state()
report = os.path.join(state, "deps.json")
with open(report, "w") as fh:
    fh.write("{}")
result, out, err, _ = call(state_dir=state, script=script, popen=fake_popen)
check("a fresh deps.json means no spawn", result is False and launched == [], str(launched))

os.utime(report, (time.time() - 13 * 3600, time.time() - 13 * 3600))
result, out, err, _ = call(state_dir=state, script=script, popen=fake_popen)
check("a deps.json older than 12 hours means a spawn", result is True and len(launched) == 1)
argv, kwargs = launched[0]
check("the checker runs under this interpreter", argv == [sys.executable, script], str(argv))
check("the child gets its own session", kwargs.get("start_new_session") is True)
check("the child has no stdin and does not share the parent's output",
      kwargs.get("stdin") is not None and kwargs.get("stdout") is not sys.stdout)
check("the log lives under the state dir", os.path.isfile(os.path.join(state, "deps-check.log")))

launched.clear()
old = time.time() - 11 * 3600
os.utime(report, (old, old))
result, _, _, _ = call(state_dir=state, script=script, popen=fake_popen)
check("11 hours old is still fresh", result is False and launched == [])

launched.clear()
result, _, _, _ = call(state_dir=fresh_state(), script=script, popen=fake_popen)
check("no deps.json at all means a spawn", result is True and len(launched) == 1)

launched.clear()
result, _, _, _ = call(state_dir=fresh_state(), script=os.path.join(ROOT, "absent.py"), popen=fake_popen)
check("a missing checker means no spawn", result is False and launched == [])


def exploding_popen(argv, **kwargs):
    raise OSError("no such interpreter")


result, out, err, _ = call(state_dir=fresh_state(), script=script, popen=exploding_popen)
check("a failing spawn is swallowed", result is False and out == "" and err == "")

blocker = os.path.join(ROOT, "state-is-a-file")
with open(blocker, "w") as fh:
    fh.write("x")
result, out, err, _ = call(state_dir=os.path.join(blocker, "sub"), script=script, popen=fake_popen)
check("an unwritable state dir is swallowed", result is False and out == "" and err == "")



launched.clear()
state = fresh_state()
result, _, _, _ = call(state_dir=state, script=script, popen=fake_popen)
check("first call with no report spawns", result is True and len(launched) == 1)
result, _, _, _ = call(state_dir=state, script=script, popen=fake_popen)
check("a second call right after does not respawn", result is False and len(launched) == 1)
log_path = os.path.join(state, "deps-check.log")
two_hours = time.time() - 2 * 3600
os.utime(log_path, (two_hours, two_hours))
result, _, _, _ = call(state_dir=state, script=script, popen=fake_popen)
check("a log older than an hour allows another try", result is True and len(launched) == 2)



sid_file = os.path.join(ROOT, "child-sid")
slow = checker("import os, time\nopen(%r, 'w').write(str(os.getsid(0)))\ntime.sleep(20)\n" % sid_file)
result, out, err, seconds = call(state_dir=fresh_state(), script=slow)
check("a slow checker does not delay the caller", result is True and seconds < 1.5, "%.2fs" % seconds)
check("a slow checker leaves the caller's output empty", out == "" and err == "")
check("the child started", wait_for(sid_file))
if os.path.exists(sid_file):
    with open(sid_file) as fh:
        check("the child runs in its own session", int(fh.read()) != os.getsid(0))

crash = checker("import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
state = fresh_state()
result, out, err, seconds = call(state_dir=state, script=crash)
check("a crashing checker does not affect the caller", result is True and out == "" and err == "" and seconds < 1.5)
end = time.monotonic() + 5
logged = ""
while time.monotonic() < end and "boom" not in logged:
    time.sleep(0.05)
    with open(os.path.join(state, "deps-check.log")) as fh:
        logged = fh.read()
check("the crash output goes to the log", "boom" in logged, logged)


with open(os.path.join(HERE, "home-materialize.py")) as fh:
    source = fh.read()
check("main() calls the spawn once", source.count("    spawn_deps_check()\n") == 1)
check("the spawn comes before the closing summary",
      source.index("    spawn_deps_check()\n") < source.index("home-materialize: ~/.claude {skills"))

shutil.rmtree(ROOT, ignore_errors=True)
print("%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
