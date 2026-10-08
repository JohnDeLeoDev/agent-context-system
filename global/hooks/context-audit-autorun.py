#!/usr/bin/env python3
'SessionStart hook: fire the CLOSED-LOOP auto context-audit when it is overdue,\nfleet-wide. FAST + NON-BLOCKING -- all real work happens in a detached headless\n`claude` process; this hook returns immediately so it never delays session start.\n\nThis is powerful autonomous machinery. It is guarded by FOUR independent gates, any\nONE of which stops it:\n  1. KILL SWITCH (default OFF): does nothing unless the enable marker file exists.\n  2. RECURSION GUARD: exits if AGENT_CONTEXT_AUTO_AUDIT_RUN is set (i.e. we are\n     already inside the headless audit session this hook launches).\n  3. LOCAL COOLDOWN: per-machine, exits if launched < 30 min ago.\n  4. OVERDUE + DISTRIBUTED LOCK: only runs if the fleet-shared state says the audit\n     is overdue AND no other machine holds a fresh lock.\n\nEverything fails SAFE: any missing precondition (no $HOME, no jq-equivalent parse, no\nclaude, no store, unreadable state) => do nothing, exit 0. It NEVER blocks or errors\nthe session.\n\nEnv overrides (for operators / tests):\n  AGENT_CONTEXT_AUTO_AUDIT_RUN      set(=1) by the launcher; presence => recursion, exit.\n  AGENT_CONTEXT_AUTO_AUDIT_DAYS     overdue interval in days (default 7).\n  AGENT_CONTEXT_AUTO_AUDIT_CLAUDE   claude binary to launch (default: `claude`). Tests\n                                    point this at a fake to avoid a real headless run.\n  AGENT_CONTEXT_AUTO_AUDIT_NOLAUNCH set => do everything (claim lock, touch cooldown,\n                                    build the command) but SKIP the actual spawn. For\n                                    tests; prints the command it WOULD have run.'
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp  
import store_task  


def emit_suppress():
    print(json.dumps({"suppressOutput": True}))


def _digits_or(value, default):
    'Mirrors the shell\'s `case "$X" in \'\'|*[!0-9]*) X=default ;; esac`: accepted\n    only if every character is an ASCII digit (no sign, no whitespace, not empty).'
    if value is None:
        return default
    s = str(value)
    return int(s) if s != "" and s.isdigit() else default


def main():
    home = os.environ.get("HOME") or ""

    
    if not home:
        emit_suppress()
        return 0

    
    if os.environ.get("AGENT_CONTEXT_AUTO_AUDIT_RUN"):
        emit_suppress()
        return 0

    
    state_dir = os.path.join(hp.state_dir(home), "audit")
    enable_marker = os.path.join(state_dir, "auto-audit-enabled")
    if not os.path.isfile(enable_marker):
        emit_suppress()
        return 0

    
    claude_bin = os.environ.get("AGENT_CONTEXT_AUTO_AUDIT_CLAUDE") or "claude"
    if not shutil.which(claude_bin):
        emit_suppress()
        return 0

    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        emit_suppress()
        return 0

    
    cooldown_file = os.path.join(state_dir, "auto-last-launch")
    cooldown_sec = 30 * 60
    now = int(time.time())
    if os.path.isfile(cooldown_file):
        try:
            last_launch = int(os.path.getmtime(cooldown_file))
        except OSError:
            last_launch = 0
        if now - last_launch < cooldown_sec:
            emit_suppress()
            return 0

    
    
    
    
    days = _digits_or(os.environ.get("AGENT_CONTEXT_AUTO_AUDIT_DAYS"), 7)
    try:
        hostname_short = subprocess.run(
            ["hostname", "-s"], capture_output=True, text=True, timeout=5
        ).stdout.strip() or "unknown"
    except Exception:
        hostname_short = "unknown"
    try:
        claim = store_task.run("store-state", ["claim-audit", "--days", str(days),
                                               "--stale-lock", "3600",
                                               "--host", hostname_short], deadline=15)
    except Exception:
        emit_suppress()
        return 0
    if claim.get("exit") != 0:
        emit_suppress()
        return 0

    
    try:
        open(cooldown_file, "w").close()
    except OSError:
        pass

    
    
    
    
    log = os.path.join(state_dir, "context-auto-%d.log" % now)
    if os.environ.get("AGENT_CONTEXT_AUTO_AUDIT_NOLAUNCH"):
        
        sys.stderr.write(
            "NOLAUNCH would run: AGENT_CONTEXT_AUTO_AUDIT_RUN=1 nohup %s -p "
            '"/context-audit auto" --permission-mode acceptEdits >%s 2>&1 &\n'
            % (claude_bin, log)
        )
    else:
        env = dict(os.environ)
        env["AGENT_CONTEXT_AUTO_AUDIT_RUN"] = "1"
        try:
            with open(log, "ab") as log_fh:
                subprocess.Popen(
                    [claude_bin, "-p", "/context-audit auto",
                     "--permission-mode", "acceptEdits"],
                    stdout=log_fh, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=env, start_new_session=True,
                )
        except OSError:
            pass

    emit_suppress()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            emit_suppress()
        except Exception:
            pass
        sys.exit(0)
