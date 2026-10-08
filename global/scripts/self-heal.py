#!/usr/bin/env python3
'self-heal — repair the core-system faults that CAN be repaired deterministically.\n\nThe probes made failure visible. This closes the other half: where a fault has one\nobvious, idempotent, cheap repair, run it instead of telling a human to.\n\nTHREE RULES, and each of them is the whole point:\n\n1. RECIPES ARE HARDCODED HERE. A repair command is NEVER read from a verdict file.\n   Verdicts are written by background probes into a state directory; treating their\n   contents as executable would turn every probe into an arbitrary-code-execution\n   path, and "the health checker ran something a JSON file told it to" is a far\n   worse outcome than any fault it might fix. Verdict files select a recipe by key;\n   they never supply one.\n\n2. NEVER HEAL SILENTLY. A repair that quietly fixes things recreates the exact\n   problem this system was built to end — state changing with nobody the wiser.\n   Every attempt is recorded and surfaced in the session\'s DEGRADED/HEALED block,\n   successes included. "It broke and fixed itself twice a day for a month" is\n   something user should get to find out.\n\n3. CHEAP AND IDEMPOTENT ONLY. Anything slow, CPU-hungry or side-effecting stays\n   MANUAL and is surfaced as an exact command instead. Rebuilding an iOS app to\n   repopulate DerivedData is the motivating example: it is the correct repair, it\n   takes minutes and pins a core, and starting it behind someone\'s back because\n   they opened a session in that directory is not acceptable. The boundary is\n   deliberate, not a limitation.\n\nTHROTTLED per fault key, because a permanently broken thing must not retry on every\nsession — that turns a visible fault into a background CPU leak, which is just the\noriginal disease with extra steps.\n\nUsage:  self-heal.py            # attempt due repairs, print a JSON summary\n        self-heal.py --dry-run  # report what it would attempt, run nothing'
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.path.expanduser("~")
STATE = os.path.join(hp.state_dir(HOME), "health")
STORE = os.path.join(HOME, ".agent-context")
SCRIPTS = os.path.join(STORE, "global", "scripts")
LOG = os.path.join(STATE, "heal-log.json")

RETRY_AFTER = 6 * 3600   








TIMEOUT = 45




RECIPES = {
    "agent-context-server": (
        "re-sync the agent-context server venv",
        ["uv", "sync", "--directory", os.path.join(STORE, "server")],
        
        
        
        
        "idempotent; writes only into the server's own .venv",
    ),
    "hook-registration": (
        "re-run home-settings-sync to restore hook registration",
        [sys.executable, os.path.join(SCRIPTS, "home-settings-sync.py")],
        
        
        
        "idempotent; the SessionStart path already runs it once per session",
    ),
    "settings-sync": (
        "retry the settings sync that failed during materialization",
        [sys.executable, os.path.join(SCRIPTS, "home-settings-sync.py")],
        "idempotent; same script, second attempt",
    ),
    "harness-materialize": (
        "retry projecting store config into the other harnesses",
        [sys.executable, os.path.join(SCRIPTS, "harness-materialize.py")],
        "idempotent; rewrites generated config from the store, no other side effects",
    ),
    "mirror-converge": (
        "reconverge git remotes, auth and signing",
        [os.path.join(HOME, ".local", "bin", "git-mirror-converge"), "--quick"],
        
        
        
        "config-only; creates no commits and pushes nothing",
    ),
}




MANUAL = {
    "lsp-no-build-logs":
        "needs a full build (minutes of CPU) — the command is in the finding itself",
    "lsp-bridge-incompatible":
        "needs a decision about which C# server to run, not a repair",
    "mcp-unmanaged-plugin":
        "a harness plugin the store does not own",
}


def load(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def history():
    return load(LOG, {}) or {}


def save_history(hist):
    os.makedirs(STATE, exist_ok=True)
    tmp = LOG + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(hist, fh, indent=2)
    os.replace(tmp, LOG)


def due(hist, key):
    last = (hist.get(key) or {}).get("last_attempt", 0)
    return time.time() - last >= RETRY_AFTER


def faults():
    "Map the probes' verdicts onto recipe keys.\n\n    Only faults whose repair is UNAMBIGUOUS are mapped. A degraded MCP server the\n    store does not own (a harness plugin) has no store-side repair, and pretending\n    otherwise would produce a heal that never heals."
    keys = []

    mcp = load(os.path.join(STATE, "mcp.json"), {}) or {}
    for entry in (mcp.get("missing") or []) + (mcp.get("degraded") or []):
        if entry.get("name") == "agent-context":
            keys.append("agent-context-server")

    if os.path.exists(os.path.join(STATE, "hooks.json")):
        keys.append("hook-registration")

    for component in ("settings-sync", "harness-materialize", "mirror-converge"):
        rec = load(os.path.join(STATE, "%s.json" % component))
        if rec and not rec.get("ok"):
            keys.append(component)

    
    return list(dict.fromkeys(keys))


def attempt(key, dry_run=False):
    desc, argv, _why = RECIPES[key]
    if dry_run:
        return {"key": key, "description": desc, "would_run": " ".join(argv)}

    if not os.path.exists(argv[0]) and "/" in argv[0]:
        return {"key": key, "description": desc, "ok": False,
                "detail": "the repair tool is not present at %s" % argv[0]}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return {"key": key, "description": desc, "ok": False,
                "detail": "%s is not installed on this machine" % argv[0]}
    except subprocess.TimeoutExpired:
        return {"key": key, "description": desc, "ok": False,
                "detail": "the repair exceeded %ds and was abandoned" % TIMEOUT}

    ok = proc.returncode == 0
    if ok:
        
        
        
        
        
        stale = os.path.join(STATE, "%s.json" % key)
        if key in ("settings-sync", "harness-materialize", "mirror-converge") \
                and os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                pass
        if key == "hook-registration":
            try:
                os.remove(os.path.join(STATE, "hooks.json"))
            except OSError:
                pass
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return {"key": key, "description": desc, "ok": ok,
            "detail": ("repaired" if ok else
                       "the repair itself failed (rc=%d): %s"
                       % (proc.returncode, "; ".join(tail[-2:]) or "no output"))}


def main():
    
    
    
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    unknown = [a for a in args if a not in ("--dry-run", "-n")]
    if unknown:
        print("self-heal: unknown flag(s): %s" % " ".join(unknown), file=sys.stderr)
        print("self-heal: a bare run ATTEMPTS REPAIRS. Refusing rather than guessing.",
              file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    hist = history()
    results = []

    keys = faults()
    for i, key in enumerate(keys, 1):
        if key not in RECIPES:
            continue
        if not dry_run and not due(hist, key):
            prior = hist.get(key) or {}
            results.append({
                "key": key, "skipped": True,
                "detail": "last repair attempt %dh ago %s; not retrying yet"
                          % ((time.time() - prior.get("last_attempt", 0)) // 3600,
                             "succeeded" if prior.get("ok") else "failed"),
            })
            continue

        if not dry_run:
            print("self-heal: [%d/%d] %s starting" % (i, len(keys), key),
                  file=sys.stderr, flush=True)
        t0 = time.time()
        res = attempt(key, dry_run)
        if not dry_run:
            print("self-heal: [%d/%d] %s %s in %ds"
                  % (i, len(keys), key, "ok" if res.get("ok") else "FAIL",
                     time.time() - t0), file=sys.stderr, flush=True)
        results.append(res)
        if not dry_run:
            entry = hist.setdefault(key, {})
            entry["last_attempt"] = int(time.time())
            entry["ok"] = res.get("ok", False)
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry["detail"] = res.get("detail", "")

    if not dry_run:
        save_history(hist)
    print(json.dumps({"ts": int(time.time()), "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
