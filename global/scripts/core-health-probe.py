#!/usr/bin/env python3
'core-health-probe — decide whether this machine\'s core MCP servers are up.\n\nWhy this exists. Every core system in this fleet fails open. A dead LSP degrades to\ngrep, which still returns results. A dead agent-context server degrades to a session\nwith no instructions, no memory and no guardrails, which still answers questions. A\nhalf-done materialization prints "(non-fatal)" and exits 0. In every case the\ndegraded mode is indistinguishable from the working one, so nothing is noticed\nuntil someone types `claude mcp list` by hand.\n\nWhat it does. Diffs the servers `claude mcp list` reports as healthy against the set\n`global/mcp-servers.json` says should be present for this harness and this working\ndirectory, and writes the verdict to ~/.local/state/agent-context/health/mcp.json. It does not\nprint to the session itself: `claude mcp list` opens a real connection to every\nserver (OAuth ones included) and costs seconds, far too slow for the SessionStart\nbudget. preflight-core-health.py spawns this in the background and reports the\nprevious run\'s verdict, which is the right tradeoff -- a server that died mid-session\nis still news next session, and nothing here is time-critical to the second.\n\nMissing vs degraded. Both are reported, deliberately, because they fail differently:\na server absent from the roster never launched (bad path, unbuilt binary, wrong\nmachine), while one that answers "! Connected · tools fetch failed" did launch and is\nlying about being usable -- its tools are absent from the agent\'s roster with\nno error anywhere.\n\nUsage:  core-health-probe.py <cwd>'
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.path.expanduser("~")
MANIFEST = os.path.join(HOME, ".agent-context", "global", "mcp-servers.json")
STATE_DIR = os.path.join(hp.state_dir(HOME), "health")
OUT = os.path.join(STATE_DIR, "mcp.json")




LINE = re.compile(r"^(?P<name>[^:]+(?::[^:]+)*): (?P<rest>.+)$")



CONFIRM_GAP = 5 * 60
SIGHTING_WINDOW = 6 * 3600


def expected_servers(cwd):
    "Servers this harness should have here, per the canonical manifest.\n\n    Mirrors harness-materialize.py's wanted()/scoped_paths(): a server with a\n    `projects` list is project-scoped and is only expected when cwd is inside one of\n    those ($HOME-relative) paths. Reimplemented rather than imported because this\n    runs as a detached background process and must not depend on the materializer\n    being importable or even present."
    try:
        with open(MANIFEST) as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"cannot read {MANIFEST}: {exc}"

    cwd = os.path.realpath(cwd)
    out = {}
    for name, spec in (manifest.get("servers") or {}).items():
        if "claude" not in (spec.get("harnesses") or []):
            continue
        rels = spec.get("projects") or []
        if rels:
            roots = [os.path.realpath(os.path.join(HOME, r)) for r in rels]
            if not any(cwd == r or cwd.startswith(r + os.sep) for r in roots):
                continue
            out[name] = "project"
        else:
            out[name] = "global"
    return out, None


def roster(cwd):
    "Actual per-server health, as the CLI sees it from cwd.\n\n    The cwd is not cosmetic: `claude mcp list` only reports project-scoped servers\n    (.mcp.json) when it runs inside that project. This probe is spawned detached, so\n    it inherits the harness process's cwd rather than the session's -- run it from\n    $HOME and every project-scoped server reads as MISSING, which is a false DEGRADED\n    block in the next session naming systems that are fine."
    
    
    
    
    
    exe = shutil.which("claude") or os.path.join(HOME, ".local", "bin", "claude")
    try:
        proc = subprocess.run(
            [exe, "mcp", "list"],
            capture_output=True, text=True, timeout=180, cwd=cwd,
        )
    except (FileNotFoundError, PermissionError):
        return None, ("the `claude` CLI was found neither on PATH nor at "
                      "~/.local/bin/claude for this hook's environment")
    except NotADirectoryError:
        return None, f"cwd {cwd} is not a directory"
    except subprocess.TimeoutExpired:
        return None, "`claude mcp list` did not finish within 180s"

    found = parse_roster(proc.stdout)
    if not found:
        return None, "`claude mcp list` produced no parseable server lines"
    return found, None


def parse_roster(stdout):
    '{name: {status, healthy}} from `claude mcp list` output.'
    found = {}
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("Checking "):
            continue
        m = LINE.match(line)
        if not m:
            continue
        rest = m.group("rest")
        if " - " not in rest:
            continue
        status = rest.rsplit(" - ", 1)[1].strip()
        
        
        
        found[m.group("name").strip()] = {
            "status": status,
            "healthy": status.startswith(("✔", "⊘")),
        }
    return found


def needs_auth(status):
    return "needs authentication" in (status or "").lower()


def classify(expected, found, previous=None, now=None):
    'Sort the roster into missing / degraded / unconfirmed / ok.\n\n    An OAuth server reading `! Needs authentication` is usually transient: the token\n    refreshes on the next dial and the status clears. Reporting one sighting told a\n    session that working servers were unauthenticated, from a verdict up to\n    MCP_MAX_AGE old. So a first sighting goes to `unconfirmed`, which the preflight\n    does not report; it becomes `degraded` only when a later probe, at least\n    CONFIRM_GAP after the first sighting, still sees it. Every other fault reports at\n    once.\n\n    The gap stops concurrent or back-to-back probes (several sessions starting at\n    once) from confirming a sighting seconds after it. The previous verdict counts\n    only within SIGHTING_WINDOW: mcp.json is one file for every project, and an old\n    verdict from another session says nothing about the server now.\n    `recheck_after` tells the preflight when the confirming probe is due.'
    now = time.time() if now is None else now
    prev = previous or {}
    first = {}
    if now - (prev.get("ts") or 0) <= SIGHTING_WINDOW:
        for e in prev.get("degraded") or []:
            if needs_auth(e.get("status")):
                first[e.get("name")] = None            
        for e in prev.get("unconfirmed") or []:
            first[e.get("name")] = e.get("first_seen") or prev.get("ts")
    report = {"missing": [], "degraded": [], "unconfirmed": [], "ok": [],
              "recheck_after": None}

    def unhealthy(name, scope, entry):
        rec = {"name": name, "scope": scope, "status": entry["status"]}
        if not needs_auth(entry["status"]):
            report["degraded"].append(rec)
            return
        seen_at = first.get(name, now)
        if name in first and (seen_at is None or now - seen_at >= CONFIRM_GAP):
            report["degraded"].append(rec)
            return
        rec["first_seen"] = seen_at
        report["unconfirmed"].append(rec)
        due = seen_at + CONFIRM_GAP
        if report["recheck_after"] is None or due < report["recheck_after"]:
            report["recheck_after"] = due

    for name, scope in sorted(expected.items()):
        entry = found.get(name)
        if entry is None:
            report["missing"].append({"name": name, "scope": scope})
        elif not entry["healthy"]:
            unhealthy(name, scope, entry)
        else:
            report["ok"].append(name)

    
    
    
    for name, entry in sorted(found.items()):
        if name not in expected and not entry["healthy"]:
            unhealthy(name, "unmanaged", entry)
    return report


def main():
    cwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    if not os.path.isdir(cwd):
        cwd = HOME
    os.makedirs(STATE_DIR, exist_ok=True)

    report = {"ts": int(time.time()), "cwd": cwd, "probe_error": None,
              "missing": [], "degraded": [], "ok": []}

    expected, err = expected_servers(cwd)
    if err or expected is None:
        report["probe_error"] = err
        _write(report)
        return 0

    found, err = roster(cwd)
    if err or found is None:
        report["probe_error"] = err
        _write(report)
        return 0

    report.update(classify(expected, found, _read_previous()))
    _write(report)
    return 0


def _read_previous():
    try:
        with open(OUT) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(report):
    
    tmp = "%s.%d.tmp" % (OUT, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(report, fh, indent=2)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    sys.exit(main())
