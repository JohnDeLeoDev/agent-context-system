#!/usr/bin/env python3

'UserPromptSubmit: this machine\'s store is not syncing -- say so every turn.\n\nThe daemon writes ~/.local/state/agent-context/health/store-sync.json with ok:false once a sync\nfault has persisted for _UNHEALTHY_NOTIFY_CYCLES (two cycles, ~10 min).\npreflight-core-health.py reads it into the degraded core systems block, but only\nat SessionStart. A session that was already running when the fault began would never\nhear of it: it would keep working against a store that is not syncing, reading\nnothing other machines write and writing nothing they can read. The fleet view cannot\nreport it either, because the row that would say so travels with the sync that is\nbroken.\n\nSo the same verdict is read here, at the start of every turn, on the machine that is\nbroken. Fails open on every error: a missing file, an unreadable payload or a daemon\nthat cannot be found all mean silence, never a blocked turn.\n\nThe "is the fault over?" test mirrors preflight-core-health.sync_fault_is_over, on\npurpose, with one addition -- AGENT_CONTEXT_STATE_DIR names the daemon\'s state dir\nso the battery can stage daemon.info; the preflight resolves it from HOME alone.'
import importlib.util
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

HOME = hp.home()
HEALTH_DIR = os.environ.get("AGENT_CONTEXT_HEALTH_DIR") or os.path.join(
    hp.state_dir(), "health")

TERSE_JUDGE = os.environ.get("TERSE_JUDGE") or os.path.join(
    hp.scripts_dir(), "terse-judge.py")


def in_loop(cwd):
    "Ralph loop active at or above cwd. Reuses terse-judge's own walk, so there is\n    no second copy of the same directory search."
    try:
        spec = importlib.util.spec_from_file_location("terse_judge", TERSE_JUDGE)
        if spec is None or spec.loader is None:
            raise ImportError(TERSE_JUDGE)
        tj = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tj)
        return tj.in_loop(cwd)
    except Exception:
        return False


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def daemon_state_dir():
    d = os.environ.get("AGENT_CONTEXT_STATE_DIR")
    if d:
        return d
    if sys.platform == "darwin":
        return os.path.join(HOME, "Library", "Application Support", "agent-context")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state")
    return os.path.join(base, "agent-context")


def fault_is_over(rec):
    'Either the daemon has synced since the verdict was written, or it started\n    after it (the claim belongs to a dead generation). Unknown -> not over.'
    info = read_json(os.path.join(daemon_state_dir(), "daemon.info"))
    if not info:
        return False
    ts = rec.get("ts") or 0
    return ((info.get("last_successful_sync") or 0) > ts
            or (info.get("started_at") or 0) > ts)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    event = data.get("hook_event_name") or "UserPromptSubmit"
    if event == "Stop":
        
        
        
        
        
        if data.get("stop_hook_active"):
            return 0
        if not in_loop(data.get("cwd") or os.getcwd()):
            return 0

    
    
    
    
    
    failures = []
    sync_rec = None
    try:
        names = sorted(os.listdir(HEALTH_DIR))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json") or name == "mcp.json":
            continue
        rec = read_json(os.path.join(HEALTH_DIR, name))
        if not rec or rec.get("ok") or hp.stale_daemon_verdict(rec, HOME):
            continue
        if name == "store-sync.json":
            if fault_is_over(rec):
                continue
            sync_rec = rec
        comp = rec.get("component") or name[:-5]
        for f in (rec.get("failures") or []):
            if f:
                failures.append(f"{comp}: {f}")
    if not failures:
        return 0

    
    
    session = (data.get("session_id") or "").strip()
    brief = False
    if session:
        try:
            d = os.path.join(hp.state_dir(HOME), "nudges")
            os.makedirs(d, exist_ok=True)
            stamp = os.path.join(d, "sync-fault-%s" % re.sub(r"[^\w.-]", "_", session))
            brief = os.path.exists(stamp)
            if not brief:
                with open(stamp, "w") as fh:
                    fh.write("1\n")
        except OSError:
            brief = False

    
    
    
    looping = any(f.startswith("agent-context daemon:") for f in failures)
    
    
    
    
    
    coverage_only = all(f.startswith("observation-coverage:") for f in failures)
    if brief:
        msg = ("sync-fault-notice: still degraded on this machine: "
               + "; ".join(f.split(". ")[0][:140] for f in failures[:3])
               + (" (+%d more)" % (len(failures) - 3) if len(failures) > 3 else "")
               + ". Fix it or say so; do not treat the store as healthy.")
    elif looping:
        msg = (
            "sync-fault-notice: the agent-context daemon on this machine is in a "
            "restart loop.\n"
            + "\n".join(failures) + "\n"
            "Store calls can time out and writes may not land until the loop stops.\n"
            "Tell user in your next message. Do not restart the daemon yourself or touch "
            "the store code: something outside the daemon is killing it.\n"
            "The daemon log's 'shutdown on' lines and the supervisor's log name the cause.")
    elif sync_rec is not None:
        msg = (
            "sync-fault-notice: the agent-context store on this machine is not syncing.\n"
            + "\n".join(failures) + "\n"
            "Writes from other machines are missing here, and this session's writes "
            "stay local.\n"
            "Before relying on the store, say it to user in your next message and call "
            "get_health() for the reason and log command.\n"
            "A merge conflict is a human's job (or resolve it from a machine whose sync "
            "works). Do not write the same entity twice hoping one copy lands.")
    elif coverage_only:
        msg = (
            "sync-fault-notice: a recurring observation has nothing enforcing it.\n"
            + "\n".join(failures) + "\n"
            "Sync and invariants are clean; run observation-coverage.py for the full "
            "list.\n"
            "Close one with an invariant-check.py check, a tested hook, or a server test "
            "that names the observation number; if it cannot be mechanized, add it to "
            "EXEMPT with the reason.\n"
            "Tell user in your next message.")
    else:
        msg = (
            "sync-fault-notice: this machine is breaking a store rule.\n"
            + "\n".join(failures) + "\n"
            "Tell user in your next message, with the fix line. Run the fix only when "
            "user asks.\n"
            "Run invariant-check.py for the full finding and its fix line.")
    if event == "Stop":
        if brief:
            
            
            
            return 0
        
        
        json.dump({"decision": "block", "reason": msg}, sys.stdout)
        sys.stdout.write("\n")
        return 0

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
