#!/usr/bin/env python3
'Tests for the settings seed that replaces the global-settings-json pseudo-script.\n\nConsolidation Phase 4, batch B2 (store doc consolidation/plan.md): "global-settings-json\nbecomes a JSON data file read by home-settings-sync.py". Until now the seed was JSON filed\nas a shell script entity, read only by init-global.py on a fresh machine, and it carried\na stale hook list and a statusline path nothing kept current. home-settings-sync.py runs at\nevery session start, so it seeds a machine with no settings.json from\nglobal/settings-seed.json and then merges the managed values the same run.\n\nEvery case runs home-settings-sync.py with HOME inside a temp dir; the store is read,\nnever written, except in the case that points AGENT_CONTEXT_STORE at an empty temp store.'

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SYNC = os.path.join(HERE, "home-settings-sync.py")
STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
SEED = os.path.join(STORE, "global", "settings-seed.json")

tmp = tempfile.mkdtemp(prefix="settings-seed-test-")
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


def run_sync(home, store=STORE):
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "AGENT_CONTEXT_STORE": store}
    return subprocess.run([sys.executable, SYNC], capture_output=True, text=True, env=env)


def settings_of(home):
    path = os.path.join(home, ".claude", "settings.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_strings(v)


print("settings seed")


seed = None
try:
    with open(SEED, encoding="utf-8") as fh:
        seed = json.load(fh)
except (OSError, ValueError) as exc:
    check("global/settings-seed.json exists and parses as JSON", False, str(exc))
else:
    check("global/settings-seed.json exists and parses as JSON", True)
leftovers = [f for f in os.listdir(os.path.join(STORE, "global", "scripts"))
             if f.startswith("global-settings-json.")]
check("the global-settings-json script entity is retired", not leftovers, "found %s" % leftovers)
if seed is not None:
    check("the seed carries no hook list (home-settings-sync owns hook wiring)",
          "hooks" not in seed, "hooks key present")
    check("the seed carries no schema-mismatch comment",
          "__comment_kind_mismatch" not in seed, "comment key present")


home = os.path.join(tmp, "fresh")
os.makedirs(os.path.join(home, ".claude"))
proc = run_sync(home)
fresh = settings_of(home)
check("a machine with no settings.json gets one", fresh is not None,
      "rc %d, stderr %r" % (proc.returncode, proc.stderr.strip()[-200:]))
check("the run exits 0", proc.returncode == 0, "rc %d" % proc.returncode)
if fresh is not None:
    strings = list(walk_strings(fresh))
    check("no __HOME__ token survives the seed",
          not any("__HOME__" in s for s in strings), "token left in settings")
    dirs = (fresh.get("permissions") or {}).get("additionalDirectories") or []
    check("home-relative seed paths point into this HOME",
          bool(dirs) and all(d.startswith(home) for d in dirs), "additionalDirectories %r" % dirs)
    check("the managed values are merged in the same run",
          fresh.get("outputStyle") == "Concise", "outputStyle %r" % fresh.get("outputStyle"))


home = os.path.join(tmp, "existing")
os.makedirs(os.path.join(home, ".claude"))
with open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"theme": "dark-machine-local", "env": {}}, fh)
proc = run_sync(home)
kept = settings_of(home) or {}
check("an existing machine-local value survives", kept.get("theme") == "dark-machine-local",
      "theme %r" % kept.get("theme"))
check("an existing settings.json gains no seed-only keys",
      "extraKnownMarketplaces" not in kept, "seed keys copied into an existing file")


empty_store = os.path.join(tmp, "empty-store")
os.makedirs(os.path.join(empty_store, "global"))
home = os.path.join(tmp, "noseed")
os.makedirs(os.path.join(home, ".claude"))
proc = run_sync(home, store=empty_store)
check("with no seed file, no settings.json is invented", settings_of(home) is None,
      "settings.json was written")
check("with no seed file, the run still exits 0", proc.returncode == 0, "rc %d" % proc.returncode)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
