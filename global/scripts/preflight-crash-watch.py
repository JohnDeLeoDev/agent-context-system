#!/usr/bin/env python3
"preflight-crash-watch — is the health hook itself still running?\n\nWhy this is a separate file. preflight-core-health is the fleet's only\nalways-visible health signal. The harness swallows a hook's non-zero exit and\nits stderr, so when the hook dies at SessionStart the session starts with no\nDEGRADED block, which is what a healthy machine looks like, and any live\nfinding stays hidden.\n\npreflight now catches its own exceptions and reports them. That closes the\ncommon case and not this one: a syntax error, a bad import, or a file that was\nnever projected leaves no code of its own able to run, so a self-report is\nimpossible by construction. A watchdog living inside the thing it watches is\nsilent on exactly the occasion it exists for.\n\nSo this runs from home-materialize.py, a store script invoked by path that a bad\nprojection cannot break, and it runs before preflight in the chain, so what it reads is the previous\nsession's outcome.\n\nHow it decides. preflight writes `.preflight.stamp` when it finishes. This file\nkeeps its own `.preflight.ticks` — the last few SessionStarts as seen from\nhere. A healthy preflight always stamps after the previous tick. So a stamp\nolder than the oldest kept tick means preflight missed whole sessions, which is\ndetection on the next session rather than after a 36-hour staleness window.\n\nTwo ticks of slack are kept deliberately: sessions overlap (a second one can\nstart while the first is mid-run), and a watchdog that cries wolf on ordinary\nconcurrency is one people learn to ignore.\n\nReports on stdout (home-materialize's output reaches the session) and as a\nhealth record, so that when preflight is alive it folds this into the normal\nDEGRADED block. Silent otherwise. Never exits non-zero: it must not be able to\nbreak the SessionStart chain it is monitoring.\n\nObservations guarded: #227, #309."
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.path.expanduser("~")
STATE = os.path.join(hp.state_dir(HOME), "health")
STAMP = os.path.join(STATE, ".preflight.stamp")
TICKS = os.path.join(STATE, ".preflight.ticks")
SETTINGS = hp.settings_file(HOME)
RECORD = os.path.join(HOME, ".agent-context", "global", "scripts",
                      "health-record.py")

KEEP_TICKS = 3          
MIN_HISTORY = 3600      
TICK_WINDOW = 600       
HOOK_NAME = "preflight-core-health"












SESSION_START_FLAG = "--session-start"





WIRING_STALE = 36 * 3600


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def registered():
    'Is preflight actually wired to SessionStart on this machine?\n\n    If it is not registered, it is not expected to run, and complaining that it\n    did not is noise. Machines legitimately differ here.'
    cfg = read_json(SETTINGS) or {}
    
    
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hook_registry", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-registry.py"))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load hook-registry.py")
    hook_registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook_registry)
    for group in hook_registry.effective_hooks(SETTINGS, cfg).get("SessionStart") or []:
        for h in group.get("hooks") or []:
            if HOOK_NAME in (h.get("command") or ""):
                return True
    return False


def load_ticks():
    rec = read_json(TICKS)
    if isinstance(rec, dict) and isinstance(rec.get("ticks"), list):
        return [t for t in rec["ticks"] if isinstance(t, (int, float))]
    return []


def save_ticks(ticks):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(TICKS, "w") as fh:
            json.dump({"ticks": ticks[-KEEP_TICKS:]}, fh)
    except OSError:
        pass


def verdict(ticks, stamp):
    'None when healthy, else the detail line to report.'
    if stamp and not stamp.get("ok"):
        return ("it CRASHED during the last session and reported: %s"
                % (stamp.get("detail") or "no detail recorded"))

    
    
    if len(ticks) < 2:
        return None

    if not stamp:
        
        
        
        
        if time.time() - min(ticks) < MIN_HISTORY:
            return None
        return ("it has NEVER recorded a completed run, across %d observed "
                "session starts spanning %s. It is registered as a SessionStart "
                "hook, so either it is not being invoked at all, or it dies "
                "before it can write anything -- a syntax error or a failed "
                "import, which it cannot report itself."
                % (len(ticks), human(time.time() - min(ticks))))

    ts = stamp.get("ts") or 0

    
    
    
    
    
    
    
    
    if ts and ticks and ts - max(ticks) > WIRING_STALE:
        return ("its watch is broken rather than the hook itself: preflight "
                "completed %s ago, so sessions are clearly starting, but nothing "
                "has recorded a session start for %s. Until that is fixed this "
                "watchdog cannot tell a missed session from a quiet machine. "
                "Check that the home-materialize entry in home-settings-sync's "
                "MANAGED table still passes %s."
                % (human(time.time() - ts), human(time.time() - max(ticks)),
                   SESSION_START_FLAG))

    missed = sum(1 for t in ticks if ts < t)
    if missed >= len(ticks):
        return ("its last completed run predates the last %d session starts "
                "(%s ago). It is registered but is no longer finishing, so no "
                "core system has been checked since."
                % (missed, human(time.time() - ts)))
    return None


def human(seconds):
    seconds = int(max(seconds, 0))
    if seconds < 3600:
        return "%d minutes" % (seconds // 60)
    if seconds < 86400:
        return "%d hours" % (seconds // 3600)
    return "%d days" % (seconds // 86400)


def report(detail):
    msg = ("preflight-core-health has stopped reporting: %s\n"
           "  This is the hook whose whole job is making a degraded core system "
           "visible, so while it is down every core system is unchecked rather "
           "than healthy.\n"
           "  Most likely a stale or broken projection. Run:\n"
           "    python3 ~/.agent-context/global/scripts/home-materialize.py\n"
           "  then replay it to see the real error:\n"
           "    echo '{\"cwd\":\"'\"$PWD\"'\"}' | python3 ~/.agent-context/global/hooks/"
           "preflight-core-health.py" % detail)
    print("Degraded core systems: watchdog: " + msg)
    
    
    if os.path.exists(RECORD):
        try:
            subprocess.run([sys.executable, RECORD, "preflight-core-health",
                            "--fail", msg], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def main():
    if not registered():
        return 0
    ticks = load_ticks()
    detail = verdict(ticks, read_json(STAMP))
    if detail:
        report(detail)
    elif os.path.exists(RECORD) and ticks:
        
        
        try:
            subprocess.run([sys.executable, RECORD, "preflight-core-health",
                            "--ok"], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    if SESSION_START_FLAG not in sys.argv:
        return 0
    now = int(time.time())
    if not ticks or now - ticks[-1] >= TICK_WINDOW:
        ticks.append(now)
    save_ticks(ticks)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                 
        
        sys.exit(0)
