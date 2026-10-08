#!/usr/bin/env python3
'deps-check.py on a machine that has the script but no manifest (fleet dependency management).\n\nScratch directories live under ~/.cache because /tmp is mounted noexec on the Synology nodes.'

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-tests")
os.makedirs(BASE, exist_ok=True)
ROOT = os.path.realpath(tempfile.mkdtemp(dir=BASE, prefix="deps-absent-"))
passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        failures.append("%s %s" % (label, detail))
        print("FAIL:", label, detail)




home = os.path.join(ROOT, "home")
scripts = os.path.join(home, ".agent-context", "global", "scripts")
os.makedirs(scripts)
for name in sorted(os.listdir(HERE)):
    if name.endswith(".py") and not name.startswith("test-"):
        shutil.copy(os.path.join(HERE, name), os.path.join(scripts, name))
os.makedirs(os.path.join(home, ".config", "chezmoi"))
with open(os.path.join(home, ".config", "chezmoi", "chezmoi.toml"), "w") as fh:
    fh.write('[data]\n    machine_id = "laptop"\n')

state = os.path.join(home, ".local", "state", "agent-context")
env = dict(os.environ, HOME=home)
done = subprocess.run([sys.executable, os.path.join(scripts, "deps-check.py")],
                      capture_output=True, text=True, env=env, cwd=home)
check("no manifest: exit 2", done.returncode == 2, str(done.returncode))
check("no manifest: one line on stderr naming the manifest",
      done.stderr.count("\n") == 1 and "manifest" in done.stderr and "Traceback" not in done.stderr,
      done.stderr)
check("no manifest: nothing on stdout", done.stdout == "", done.stdout)
check("no manifest: no deps.json is written", not os.path.exists(os.path.join(state, "deps.json")))
check("no manifest: no health record is written, so the banner stays silent",
      not os.path.exists(os.path.join(state, "health", "deps.json")))


os.makedirs(os.path.join(home, ".agent-context", "global", "deps"))
with open(os.path.join(home, ".agent-context", "global", "deps", "manifest.toml"), "w") as fh:
    fh.write('schema = 1\n[machine.laptop]\nos = "mac"\nroles = ["base"]\n[role.base]\ntools = ["sh"]\n'
             '[tool.sh]\ncheck = ["sh"]\nexists_only = true\nchannel = "manual"\nfix = "install sh"\n')
done = subprocess.run([sys.executable, os.path.join(scripts, "deps-check.py")],
                      capture_output=True, text=True, env=env, cwd=home)
check("with the manifest present the same layout runs clean", done.returncode == 0
      and "all 1 tools ok" in done.stdout, done.stdout + done.stderr)
check("with the manifest present deps.json is written", os.path.isfile(os.path.join(state, "deps.json")))

shutil.rmtree(ROOT, ignore_errors=True)
print("%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
