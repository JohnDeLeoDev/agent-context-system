#!/usr/bin/env python3
"SessionStart: when a session opens INSIDE a registered project, launch a DETACHED\nbackground agent that verifies THAT project's memory BODIES against the current\ncode and corrects high-confidence stale claims (queues the rest). NON-BLOCKING: all\nreal work happens in a detached headless `claude`; this hook returns immediately so\nit never holds up the user's session.\n\nGuarded by independent gates (any ONE stops it); everything fails SAFE (=> exit 0):\n  1. KILL SWITCH (default OFF): needs ~/.local/state/agent-context/mem-verify/enabled.\n  2. RECURSION GUARD: exits if PROJECT_MEMORY_VERIFY_RUN is set (we're the launched run).\n  3. IN A PROJECT: exits unless cwd sits inside a tree containing a .agents/ dir.\n  4. LOCAL COOLDOWN: per-project, exits if launched < 60 min ago.\n  5. OVERDUE + DISTRIBUTED LOCK: per-project state; only if overdue (default 3 days)\n     and no other machine holds a fresh lock.\n\nTest seams: PROJECT_MEMORY_VERIFY_NOLAUNCH (claim lock + print cmd, don't spawn),\nPROJECT_MEMORY_VERIFY_CLAUDE (fake binary), PROJECT_MEMORY_VERIFY_DAYS (overdue days),\nPROJECT_MEMORY_VERIFY_CEILING (stop the .agents search at a directory)."
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp


def emit():
    print(json.dumps({"suppressOutput": True}))


def _digits_or(value, default):
    if value is None:
        return default
    s = str(value)
    return int(s) if s != "" and s.isdigit() else default


def main():
    home = os.environ.get("HOME") or ""
    if not home:
        emit()
        return 0
    if os.environ.get("PROJECT_MEMORY_VERIFY_RUN"):     
        emit()
        return 0

    state_dir = os.path.join(hp.state_dir(home), "mem-verify")
    if not os.path.isfile(os.path.join(state_dir, "enabled")):   
        emit()
        return 0

    claude_bin = os.environ.get("PROJECT_MEMORY_VERIFY_CLAUDE") or "claude"
    if not shutil.which(claude_bin):
        emit()
        return 0

    
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    cwd = data.get("cwd") or os.environ.get("PWD") or ""
    if not cwd or not os.path.isdir(cwd):
        emit()
        return 0

    
    
    
    
    ceiling = os.environ.get("PROJECT_MEMORY_VERIFY_CEILING") or ""
    d = cwd
    root = ""
    while d and d != "/" and d != home:
        if os.path.isdir(os.path.join(d, ".agents")):
            root = d
            break
        if ceiling and d == ceiling:
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    if not root:            
        emit()
        return 0

    
    key = ""
    project_id_file = os.path.join(root, ".agents", "project-id")
    if os.path.isfile(project_id_file):
        try:
            with open(project_id_file, encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            key = re.sub(r"[^A-Za-z0-9._-]", "", raw)[:64]
        except OSError:
            key = ""
    if not key:
        key = re.sub(r"[^A-Za-z0-9._-]", "", os.path.basename(root))
    if not key:
        emit()
        return 0
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        emit()
        return 0
    state_path = os.path.join(state_dir, "proj-%s.json" % key)
    if not os.path.isfile(state_path):
        try:
            with open(state_path, "w", encoding="utf-8") as fh:
                fh.write('{"last_run":0,"running_since":null,"running_host":null}\n')
        except OSError:
            pass

    now = int(time.time())

    
    cool = os.path.join(state_dir, "cool-%s" % key)
    if os.path.isfile(cool):
        try:
            last = int(os.path.getmtime(cool))
        except OSError:
            last = 0
        if now - last < 3600:
            emit()
            return 0

    
    days = _digits_or(os.environ.get("PROJECT_MEMORY_VERIFY_DAYS"), 3)
    interval = days * 86400
    stale = 3600
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    last_run = _digits_or(state.get("last_run"), 0)
    if now - last_run <= interval:
        emit()
        return 0
    rs = state.get("running_since")
    if rs is not None and str(rs) != "":
        rs_i = _digits_or(rs, 0)
        if rs_i > 0 and now - rs_i <= stale:
            emit()
            return 0

    
    try:
        h = subprocess.run(["hostname", "-s"], capture_output=True, text=True,
                            timeout=5).stdout.strip() or "unknown"
    except Exception:
        h = "unknown"
    tmp = "%s.tmp.%d" % (state_path, os.getpid())
    state["running_since"] = now
    state["running_host"] = h
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, state_path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        emit()
        return 0
    try:
        open(cool, "w").close()
    except OSError:
        pass

    
    log = os.path.join(state_dir, "run-%s-%d.log" % (key, now))
    if os.environ.get("PROJECT_MEMORY_VERIFY_NOLAUNCH"):
        sys.stderr.write('NOLAUNCH would run: (cwd=%s state=%s) %s -p '
                          '"/verify-project-memory"\n' % (root, state_path, claude_bin))
    else:
        env = dict(os.environ)
        env["PROJECT_MEMORY_VERIFY_RUN"] = "1"
        env["PROJECT_MEMORY_VERIFY_STATE"] = state_path
        try:
            with open(log, "ab") as log_fh:
                subprocess.Popen(
                    [claude_bin, "-p", "/verify-project-memory",
                     "--permission-mode", "acceptEdits"],
                    stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    cwd=root, env=env, start_new_session=True,
                )
        except OSError:
            pass

    emit()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            emit()
        except Exception:
            pass
        sys.exit(0)
