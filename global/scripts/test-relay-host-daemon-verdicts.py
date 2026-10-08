#!/usr/bin/env python3
'test-relay-host-daemon-verdicts: a relay host reports no verdict a retired daemon left.'
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(HERE, "..", "hooks")
sys.path.insert(0, HERE)
import harness_paths as hp  

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("ok  " if cond else "FAIL", name, "" if cond else ": " + str(detail)))


def fake_home(relay, checkout):
    home = os.path.join(tempfile.mkdtemp(prefix="relay-verdicts-"), "home")
    os.makedirs(home)
    if relay:
        os.makedirs(os.path.join(home, ".local", "share", "agent-context", "relay", "current"))
    if checkout:
        os.makedirs(os.path.join(home, ".agent-context", ".git"))
    health = os.path.join(home, ".local", "state", "agent-context", "health")
    os.makedirs(health)
    for name, comp in (("store-sync", "agent-context sync"), ("invariants", "invariants"),
                       ("daemon", "agent-context daemon"), ("deps", "deps")):
        with open(os.path.join(health, name + ".json"), "w") as fh:
            json.dump({"component": comp, "ok": False, "ts": 1, "failures": [comp + " broke"]}, fh)
    return home, health


def preflight_findings(home, health):
    spec = importlib.util.spec_from_file_location(
        "preflight_core_health", os.path.join(HOOKS, "preflight-core-health.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HOME = home
    mod.STATE = health
    return sorted(c for c, _ in mod.generic_findings())


def notice(home):
    env = dict(os.environ, HOME=home)
    env.pop("AGENT_CONTEXT_HEALTH_DIR", None)
    env.pop("AGENT_CONTEXT_STATE_DIR", None)
    done = subprocess.run([sys.executable, os.path.join(HOOKS, "sync-fault-notice.py")],
                          input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
                          capture_output=True, text=True, env=env, timeout=30)
    return done.stdout


def main():
    homes = []
    try:
        relay_home, relay_health = fake_home(relay=True, checkout=False)
        daemon_home, daemon_health = fake_home(relay=False, checkout=True)
        both_home, _ = fake_home(relay=True, checkout=True)
        bare_home, _ = fake_home(relay=False, checkout=False)
        homes = [relay_home, daemon_home, both_home, bare_home]

        check("relay release and no checkout is a relay host", hp.is_relay_host(relay_home))
        check("a store checkout is never a relay host", not hp.is_relay_host(daemon_home))
        check("a checkout beside a relay release is not a relay host", not hp.is_relay_host(both_home))
        check("neither relay nor checkout is not a relay host", not hp.is_relay_host(bare_home))

        got = preflight_findings(relay_home, relay_health)
        check("preflight on a relay host reports only the non-daemon verdict", got == ["deps"], got)
        got = preflight_findings(daemon_home, daemon_health)
        check("preflight on a daemon host reports every verdict",
              got == ["agent-context daemon", "agent-context sync", "deps", "invariants"], got)

        out = notice(relay_home)
        check("sync-fault-notice on a relay host omits daemon verdicts",
              "deps broke" in out and "sync broke" not in out and "invariants broke" not in out
              and "daemon broke" not in out, out)
        out = notice(daemon_home)
        check("sync-fault-notice on a daemon host reports the sync fault", "sync broke" in out, out)
    finally:
        for h in homes:
            shutil.rmtree(os.path.dirname(h), ignore_errors=True)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
