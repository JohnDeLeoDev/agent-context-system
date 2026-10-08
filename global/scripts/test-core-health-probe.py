#!/usr/bin/env python3
"Test battery for core-health-probe's verdict: what counts as a degraded MCP server.\n\n  * a claude.ai connector user disabled for the project (`⊘ Disabled for this project`)\n    was reported as degraded. Deliberately off is a choice, not a fault;\n  * an OAuth server's single `! Needs authentication` sighting was reported for hours\n    while the server answered normally. That status is usually transient and clears on\n    the next dial, so it is held back until a second consecutive probe agrees, and the\n    preflight re-probes at once rather than waiting out MCP_MAX_AGE for that second look.\n\nRun: python3 test-core-health-probe.py"
import importlib.util
import json
import os
import sys
import tempfile
import time

GLOBAL = os.path.join(os.path.expanduser("~"), ".agent-context", "global")
PROBE = os.path.join(GLOBAL, "scripts", "core-health-probe.py")
HOOK = os.path.join(GLOBAL, "hooks", "preflight-core-health.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if detail and not cond else ""))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load spec for %s from %s" % (name, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LIST_OUTPUT = """Checking MCP server health...

agent-context: /home/user/.local/bin/agent-context-mcp - ✔ Connected
plugin:firebase:firebase: npx -y firebase-tools mcp - ! Connected · tools fetch failed
claude.ai Fastmail: https://example.invalid/mcp - ⊘ Disabled for this project (re-enable via /mcp)
example-workspace-admin-prod: https://admin.example/mcp - ! Needs authentication
"""


def names(entries):
    return sorted(e["name"] for e in entries)


def probe_cases(mod):
    print("core-health-probe -- verdict classification\n")

    found = mod.parse_roster(LIST_OUTPUT)
    check("parses every server line, colons in names included",
          sorted(found) == ["agent-context", "claude.ai Fastmail",
                            "example-workspace-admin-prod", "plugin:firebase:firebase"],
          "got %s" % sorted(found))

    
    check("a ⊘ disabled server is healthy",
          found.get("claude.ai Fastmail", {}).get("healthy") is True)
    check("tools fetch failed is still unhealthy",
          found.get("plugin:firebase:firebase", {}).get("healthy") is False)

    expected = {"agent-context": "global", "example-workspace-admin-prod": "project"}
    gap, window = mod.CONFIRM_GAP, mod.SIGHTING_WINDOW
    t0 = 1_000_000

    def run(found_, previous, now):
        rep = mod.classify(expected, found_, previous, now)
        rep["ts"] = now               
        return rep

    
    rep = run(found, None, t0)
    check("first needs-auth sighting is unconfirmed, not degraded",
          names(rep["unconfirmed"]) == ["example-workspace-admin-prod"]
          and "example-workspace-admin-prod" not in names(rep["degraded"]),
          "got %s" % rep)
    check("first sighting records when it was first seen",
          rep["unconfirmed"] and rep["unconfirmed"][0].get("first_seen") == t0,
          "got %s" % rep["unconfirmed"])
    check("an unconfirmed sighting asks for a re-check after the gap",
          rep.get("recheck_after") == t0 + gap, "got %s" % rep.get("recheck_after"))
    check("a real fault is degraded on first sighting",
          names(rep["degraded"]) == ["plugin:firebase:firebase"], "got %s" % rep)
    check("disabled connector is in neither list",
          "claude.ai Fastmail" not in names(rep["degraded"] + rep["unconfirmed"]))
    check("healthy manifest server is ok", rep["ok"] == ["agent-context"],
          "got %s" % rep["ok"])

    
    
    
    soon = run(found, rep, t0 + 30)
    check("a second sighting inside the gap stays unconfirmed",
          names(soon["unconfirmed"]) == ["example-workspace-admin-prod"]
          and "example-workspace-admin-prod" not in names(soon["degraded"]), "got %s" % soon)
    check("the gap counts from the FIRST sighting, carried forward",
          soon["unconfirmed"] and soon["unconfirmed"][0].get("first_seen") == t0,
          "got %s" % soon["unconfirmed"])

    
    rep2 = run(found, soon, t0 + gap)
    check("a second sighting after the gap is degraded",
          "example-workspace-admin-prod" in names(rep2["degraded"]) and not rep2["unconfirmed"],
          "got %s" % rep2)
    check("nothing unconfirmed means no re-check request",
          rep2.get("recheck_after") is None, "got %s" % rep2.get("recheck_after"))
    rep3 = run(found, rep2, t0 + gap + 3600)
    check("a confirmed needs-auth stays degraded on the next probe",
          "example-workspace-admin-prod" in names(rep3["degraded"]), "got %s" % rep3)

    
    
    
    old = run(found, rep, t0 + window + 1)
    check("a sighting older than the window starts over",
          names(old["unconfirmed"]) == ["example-workspace-admin-prod"]
          and old["unconfirmed"][0].get("first_seen") == t0 + window + 1,
          "got %s" % old)
    stale_confirmed = run(found, rep2, t0 + gap + window + 1)
    check("a confirmed sighting older than the window starts over",
          names(stale_confirmed["unconfirmed"]) == ["example-workspace-admin-prod"],
          "got %s" % stale_confirmed)

    
    healed = mod.parse_roster(LIST_OUTPUT.replace("! Needs authentication",
                                                  "✔ Connected"))
    rep4 = run(healed, rep, t0 + gap)
    check("a cleared sighting reports ok",
          "example-workspace-admin-prod" in rep4["ok"] and not rep4["unconfirmed"],
          "got %s" % rep4)
    rep5 = run(found, rep4, t0 + 2 * gap)
    check("needs-auth after a clean probe starts over as unconfirmed",
          names(rep5["unconfirmed"]) == ["example-workspace-admin-prod"], "got %s" % rep5)

    
    rep6 = run(found, {"probe_error": "timed out", "ts": t0}, t0 + gap)
    check("a previous probe error does not confirm a sighting",
          names(rep6["unconfirmed"]) == ["example-workspace-admin-prod"], "got %s" % rep6)

    
    rep7 = run(found, None, t0)
    check("a disabled unmanaged connector is not degraded",
          "claude.ai Fastmail" not in names(rep7["degraded"]), "got %s" % rep7)


def preflight_cases():
    
    
    print("\npreflight-core-health -- an unconfirmed sighting is re-probed now\n")

    def spawned(report):
        mod = load_module("preflight_core_health_spawn", HOOK)
        mod.STATE = tempfile.mkdtemp(prefix="probe-spawn-test-")
        with open(os.path.join(mod.STATE, "mcp.json"), "w") as fh:
            json.dump(report, fh)
        calls = []
        mod.detach = lambda script, *args: calls.append(script)
        mod.HOOKPROBE = os.path.join(mod.STATE, "absent")
        mod.spawn_probes(mod.STATE)
        return mod.PROBE in calls

    now = time.time()
    fresh = {"ts": now - 60, "missing": [], "degraded": [], "ok": ["a"],
             "recheck_after": None}
    pending = dict(fresh, unconfirmed=[{"name": "x", "scope": "unmanaged",
                                        "status": "! Needs authentication",
                                        "first_seen": now - 60}])
    check("a fresh clean verdict is not re-probed", not spawned(fresh))
    check("an unconfirmed sighting past its re-check time IS re-probed",
          spawned(dict(pending, recheck_after=now - 1)))
    check("an unconfirmed sighting before its re-check time is not re-probed",
          not spawned(dict(pending, recheck_after=now + 240)))
    check("an old verdict is re-probed",
          spawned(dict(fresh, ts=now - 7 * 3600)))


def main():
    for path in (PROBE, HOOK):
        if not os.path.exists(path):
            print("X not found: %s" % path)
            return 1
    try:
        probe_cases(load_module("core_health_probe", PROBE))
    except AttributeError as exc:
        check("probe exposes parse_roster and classify", False, str(exc))
    preflight_cases()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED: %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
