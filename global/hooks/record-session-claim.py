#!/usr/bin/env python3
"record-session-claim -- where THIS session is working, for every other session to see.\n\n    <state-dir>/claims/<session_id>.json\n    {session, cwd, repo, project, project_id, worktree, files[{path, ts}],\n     started, last_seen, transcript, turn}\n\nUserPromptSubmit renews the claim and its cwd; PostToolUse(Edit|Write) adds the file;\nStop marks the turn `idle` (any other event marks it `busy`); SessionEnd removes the file. The daemon reads the directory (server claims.py), drops\nanything older than thirty minutes, and publishes the rest in this machine's fleet row.\n\nNEVER SPEAKS. On UserPromptSubmit plain stdout becomes model context, so this prints\nnothing at all. NEVER BLOCKS: exit 0 on every path, and any error is swallowed --\na claim is diagnostics, and diagnostics must not be able to cost a turn."
import json
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
MAX_FILES = 20


def state_dir():
    d = os.environ.get("AGENT_CONTEXT_STATE_DIR")
    if d:
        return d
    if sys.platform == "darwin":
        return os.path.join(HOME, "Library", "Application Support", "agent-context")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state")
    return os.path.join(base, "agent-context")


def git_root(path):
    'Nearest ancestor holding .git (a FILE in a linked worktree), or None.'
    d = os.path.abspath(path or "")
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def project_of(root):
    '(id, display_name) from the project marker project-materialize writes.'
    if not root:
        return None, None
    try:
        with open(os.path.join(root, ".agents", "project-id"), encoding="utf-8") as fh:
            txt = fh.read()
    except OSError:
        return None, None
    pid = re.search(r'^id\s*=\s*"([^"]+)"', txt, re.M)
    name = re.search(r'^display_name\s*=\s*"([^"]+)"', txt, re.M)
    return (pid.group(1) if pid else None), (name.group(1) if name else None)


def worktree_of(path):
    m = re.search(r"/\.(?:agents|claude)/worktrees/([^/]+)", path or "")
    return m.group(1) if m else None


CLIENT_PID_ENV = "AGENT_CONTEXT_HOOK_CLIENT_PID"   


def ancestor_pids(limit=8):
    "This hook's ancestors, nearest first: [shell, claude, terminal, ...].\n\n    The daemon serves every session on one port and sees a connection, not a\n    session -- but the connection's peer is a PROCESS, and one of these is it (the\n    Claude process that spawned this hook). Recording the chain lets the server say\n    exactly which claim is the caller (claims.py), which is what tells two sessions\n    in one directory on one machine apart. /proc where it exists, one `ps` call\n    otherwise; never more than one subprocess."
    parents = {}
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", encoding="utf-8", errors="replace") as fh:
                    rest = fh.read().rsplit(")", 1)[1].split()
                parents[int(name)] = int(rest[1])
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass
    if not parents:
        try:
            import subprocess
            out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                                 text=True, timeout=5).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    parents[int(parts[0])] = int(parts[1])
        except Exception:
            return []
    
    
    
    client = os.environ.get(CLIENT_PID_ENV, "")
    chain, pid = [], (parents.get(int(client), 0) if client.isdigit() else os.getppid())
    while pid > 1 and len(chain) < limit:
        chain.append(pid)
        pid = parents.get(pid, 0)
    return chain


RECENT_EDIT_SECS = 1800     


def current_worktree(cwd, files, now):
    "The worktree the session is in: its cwd's, else that of its newest recent edit (a\n    session can edit a worktree by absolute path from the main checkout), else None."
    here = worktree_of(cwd)
    if here:
        return here
    for f in reversed(files or []):
        if not isinstance(f, dict) or now - float(f.get("ts") or 0) > RECENT_EDIT_SECS:
            continue
        there = worktree_of(f.get("path"))
        if there:
            return there
    return None


def poke_daemon(state, last_poke_at, now):
    'Ask the daemon to publish now, at most once a minute per session: a claim that\n    changed is worth a push, a claim that changed again ten seconds later is not.'
    if now - float(last_poke_at or 0) < 60:
        return last_poke_at
    try:
        with open(os.path.join(state, "sync-requested"), "a", encoding="utf-8"):
            pass
        os.utime(os.path.join(state, "sync-requested"), None)
    except OSError:
        return last_poke_at
    return now


def main():
    data = json.load(sys.stdin)
    session = str(data.get("session_id") or "").strip()
    if not session:
        return 0
    event = data.get("hook_event_name") or ""
    d = os.path.join(state_dir(), "claims")
    path = os.path.join(d, re.sub(r"[^\w.-]", "_", session) + ".json")

    if event == "SessionEnd":
        try:
            os.remove(path)
        except OSError:
            pass
        return 0

    os.makedirs(d, exist_ok=True)
    rec = {}
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh) or {}
    except (OSError, ValueError):
        rec = {}
    now = time.time()
    cwd = data.get("cwd") or rec.get("cwd") or os.getcwd()
    root = git_root(cwd)
    pid, pname = project_of(root)
    store = os.path.join(HOME, ".agent-context")
    if not pname and (cwd == store or cwd.startswith(store + os.sep)):
        pname = "agent-context"
    
    before = (rec.get("cwd"), rec.get("worktree"), rec.get("project"),
              tuple(f.get("path") for f in (rec.get("files") or []) if isinstance(f, dict)))
    rec.update({
        "session": session, "cwd": cwd, "repo": root,
        "project": pname, "project_id": pid,
        "last_seen": now,
        "transcript": data.get("transcript_path") or rec.get("transcript"),
    })
    rec.setdefault("started", now)
    
    
    
    rec["turn"] = "idle" if event == "Stop" else "busy"
    
    
    if not rec.get("pids") or not rec.get("pids_via"):
        rec["pids"] = ancestor_pids()
        rec["pids_via"] = "client" if os.environ.get(CLIENT_PID_ENV) else "parent"

    if event == "PostToolUse":
        fp = (data.get("tool_input") or {}).get("file_path")
        if fp:
            fp = os.path.abspath(fp)
            files = [f for f in (rec.get("files") or [])
                     if isinstance(f, dict) and f.get("path") != fp]
            files.append({"path": fp, "ts": int(now)})
            rec["files"] = files[-MAX_FILES:]

    
    
    
    rec["worktree"] = current_worktree(cwd, rec.get("files"), now)

    after = (rec.get("cwd"), rec.get("worktree"), rec.get("project"),
             tuple(f.get("path") for f in (rec.get("files") or []) if isinstance(f, dict)))
    if after != before:
        rec["poked_at"] = poke_daemon(state_dir(), rec.get("poked_at"), now)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, separators=(",", ":"))
    os.replace(tmp, path)
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
