#!/usr/bin/env python3
'Test battery: a degraded MCP verdict is re-probed before preflight repeats it.\n\nRun: python3 test-preflight-mcp-reprobe.py'
import importlib.util
import json
import os
import sys
import tempfile
import time

HOOK = os.path.join(os.path.expanduser("~"), ".agent-context", "global", "hooks",
                    "preflight-core-health.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if detail and not cond else ""))


def load_hook():
    spec = importlib.util.spec_from_file_location("preflight_core_health", HOOK)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load spec from %s" % HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CLEAN = {"probe_error": None, "missing": [], "degraded": []}
FASTMAIL = {"name": "claude.ai Fastmail", "scope": "unmanaged",
            "status": "⊘ Disabled for this project"}


def bed(mod, cached, after=None, age_hours=1):
    'One cached mcp.json; `after` is what the stubbed re-probe writes, or None for a\n    re-probe that wrote nothing (it timed out). Returns (calls, findings).'
    root = tempfile.mkdtemp(prefix="preflight-mcp-")
    setattr(mod, "STATE", os.path.join(root, "health"))
    os.makedirs(mod.STATE)
    setattr(mod, "HOOKPROBE", os.path.join(root, "absent.py"))
    calls = {"sync": 0, "detached": []}

    def write(rep, age):
        with open(os.path.join(mod.STATE, "mcp.json"), "w") as fh:
            json.dump(dict(rep, ts=time.time() - age * 3600), fh)

    write(cached, age_hours)

    def reprobe(cwd):
        calls["sync"] += 1
        if after is not None:
            write(after, 0)

    setattr(mod, "reprobe_now", reprobe)
    setattr(mod, "detach", lambda script, *a: calls["detached"].append(script))
    mod.spawn_probes(root)
    found = [w for w, _ in mod.mcp_findings(time.time() - 30 * 86400)]
    return calls, found


def main():
    if not os.path.exists(HOOK):
        print("X hook not found at %s (run home-materialize.py)" % HOOK)
        return 1
    mod = load_hook()
    print("preflight-core-health -- degraded MCP verdict is re-probed\n")

    calls, found = bed(mod, dict(CLEAN, degraded=[FASTMAIL]), after=CLEAN)
    check("a fresh verdict naming a degraded server is re-probed in the foreground",
          calls["sync"] == 1 and mod.PROBE not in calls["detached"], "calls=%s" % calls)
    check("a fault the re-probe no longer sees is not reported",
          found == [], "got %s" % found)

    calls, _ = bed(mod, dict(CLEAN, missing=[{"name": "csharp-lsp", "scope": "project"}]),
                   after=CLEAN)
    check("a verdict naming a missing server is re-probed too",
          calls["sync"] == 1, "calls=%s" % calls)

    calls, found = bed(mod, dict(CLEAN, degraded=[FASTMAIL]),
                       after=dict(CLEAN, degraded=[FASTMAIL]))
    check("a fault the re-probe confirms IS reported",
          found == ["MCP claude.ai Fastmail"], "got %s" % found)

    calls, found = bed(mod, dict(CLEAN, degraded=[FASTMAIL]), after=None)
    check("a re-probe that wrote nothing leaves the cached fault reported",
          found == ["MCP claude.ai Fastmail"], "got %s" % found)

    calls, _ = bed(mod, CLEAN)
    check("a fresh clean verdict costs no probe at all",
          calls["sync"] == 0 and mod.PROBE not in calls["detached"], "calls=%s" % calls)

    calls, _ = bed(mod, CLEAN, age_hours=7)
    check("an old clean verdict is refreshed in the background, as before",
          calls["sync"] == 0 and mod.PROBE in calls["detached"], "calls=%s" % calls)

    
    
    check("the re-probe bound plus the heal budget stays under the 120s hook timeout",
          getattr(mod, "REPROBE_TIMEOUT", 999) + mod.HEAL_TIMEOUT < 120,
          "REPROBE_TIMEOUT=%s HEAL_TIMEOUT=%s"
          % (getattr(mod, "REPROBE_TIMEOUT", None), mod.HEAL_TIMEOUT))

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED: %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
