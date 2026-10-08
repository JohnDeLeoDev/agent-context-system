
'Where each live session is working, said to every other session on the fleet.\n\nEach session claims where it is, so a session can learn that someone is already working\nin the same place before it starts. The `record-session-claim` hook writes\n`<state-dir>/claims/<session_id>.json` on every turn (cwd, repo, project, worktree,\nlast seen) and on every Edit/Write (the file). This module reads those files, drops\nthe expired ones, and hands the daemon a publishable list that rides in the fleet row\n(`fleet.with_live`), which every machine reads within a sync cycle. Bootstrap then\nsays who else is on this project or in the store, and a store write warns when\nanother live session holds the entity (`store.claim_warning`). Warn, then allow: a\nhand-off has to stay possible, and the stale-write guard still\nrefuses a write that is actually behind.\n\nWhat it is not: an identity. Two sessions on one machine in one directory are\nindistinguishable here, and the bootstrap says so rather than guessing which is the\ncaller. And a claim is only as fresh as the last turn that wrote it, which is why\n`last_seen` is published and the reader subtracts.'
from __future__ import annotations

import contextlib
import json
import os
import threading
import time

from . import paths




TTL_SECS = 1800.0





SEEN_BUCKET_SECS = 300

MAX_FILES = 10


def claims_dir():
    return paths.state_dir() / "claims"


def _publishable(rec, now) -> dict:
    files = [f.get("path") for f in (rec.get("files") or []) if isinstance(f, dict)]
    seen = float(rec.get("last_seen") or 0)
    return {
        
        
        "session": str(rec.get("session") or "")[:8],
        "cwd": rec.get("cwd"),
        "repo": rec.get("repo"),
        "project": rec.get("project"),
        "project_id": rec.get("project_id"),
        "worktree": rec.get("worktree"),
        "last_seen": int(seen // SEEN_BUCKET_SECS * SEEN_BUCKET_SECS),
        "files": [f for f in files if f][-MAX_FILES:],
        
        
        "pids": [p for p in (rec.get("pids") or []) if isinstance(p, int)][:8],
    }


def read_live(now=None, ttl=TTL_SECS) -> list[dict]:
    "This machine's live claims, publishable. Expired files are removed: a claim\n    nobody renews is litter, and the hook that wrote it may never run again (a\n    harness that crashed reaches no SessionEnd)."
    t = time.time() if now is None else now
    out = [_publishable(rec, t) for rec in _live_records(t, ttl)]
    out.sort(key=lambda r: (r.get("last_seen") or 0), reverse=True)
    return out


def owner_record(pid, now=None, ttl=TTL_SECS) -> dict | None:
    'The whole claim of the session that owns `pid` (see `owner`), on this machine: its\n    full session id and cwd, which `read_live` never publishes. For a process that acts for\n    its own session, such as a bridge that wakes it.'
    t = time.time() if now is None else now
    return owner(_live_records(t, ttl), pid)


def _live_records(t, ttl) -> list[dict]:
    "This machine's live claim files as written; expired ones are removed."
    d = claims_dir()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".json"):
            continue
        p = d / name
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or t - float(rec.get("last_seen") or 0) > ttl:
            with contextlib.suppress(OSError):
                p.unlink()
            continue
        if name.startswith(REMOTE_CLAIM_PREFIX):
            continue          
        out.append(rec)
    return out


def fleet_sessions(rows, now=None, ttl=TTL_SECS) -> list[dict]:
    "Every live session on every machine, from fleet rows, each tagged with the\n    machine it runs on. A row's sessions are trusted only while the row itself is\n    fresh enough for its claims to be alive."
    t = time.time() if now is None else now
    out = []
    for row in rows:
        for s in row.get("sessions") or []:
            if not isinstance(s, dict):
                continue
            if t - float(s.get("last_seen") or 0) > ttl + SEEN_BUCKET_SECS:
                continue
            s = dict(s)
            s["machine"] = row.get("machine_id") or row.get("hostname") or "?"
            s["machine_uuid"] = row.get("machine_uuid")
            out.append(s)
    out.sort(key=lambda r: (r.get("last_seen") or 0), reverse=True)
    return out












def _pid_for_port_linux(port, proc="/proc") -> int | None:
    want = f"{int(port):04X}"
    inodes = set()
    for table in ("tcp", "tcp6"):
        try:
            with open(f"{proc}/net/{table}", encoding="utf-8", errors="replace") as fh:
                next(fh, None)
                for line in fh:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    local, state, inode = parts[1], parts[3], parts[9]
                    if state == "01" and local.rsplit(":", 1)[-1] == want:
                        inodes.add(inode)
        except OSError:
            continue
    if not inodes:
        return None
    targets = {f"socket:[{i}]" for i in inodes}
    try:
        pids = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        return None
    for pid in pids:
        fd_dir = f"{proc}/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                with contextlib.suppress(OSError):
                    if os.readlink(f"{fd_dir}/{fd}") in targets:
                        return int(pid)
        except OSError:
            continue
    return None


def _pid_for_port_lsof(port) -> int | None:
    import subprocess
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:ESTABLISHED",
                              "-Fpn"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pid = None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid:
            
            
            local = line[1:].split("->", 1)[0]
            if local.rsplit(":", 1)[-1] == str(int(port)) and pid.isdigit():
                return int(pid)
    return None


def peer_pid(port) -> int | None:
    'The pid of the process holding the client end of a connection whose client\n    port is `port`, or None when it cannot be told (a remote client, no tools).'
    if not port:
        return None
    try:
        if os.path.isdir("/proc/net"):
            return _pid_for_port_linux(port)
        return _pid_for_port_lsof(port)
    except Exception:
        return None


def _ancestors(pid, limit=8, proc="/proc") -> list[int]:
    "`pid`'s own ancestors, nearest first: the same walk record-session-claim's\n    `ancestor_pids` does for the hook process itself, run here against an arbitrary\n    pid. Best-effort: empty on any error.\n\n    Needed for a relay's stdio MCP process: the hook records the\n    ancestry of its own ppid (the Claude process), never the pid of anything\n    Claude later spawns, so a relay pid is never IN a hook claim's `pids` -- but\n    the relay's parent (the Claude process) is, and that parent is this pid's own\n    nearest ancestor."
    parents = {}
    try:
        for name in os.listdir(proc):
            if not name.isdigit():
                continue
            try:
                with open(f"{proc}/{name}/stat", encoding="utf-8", errors="replace") as fh:
                    rest = fh.read().rsplit(")", 1)[1].split()
                parents[int(name)] = int(rest[1])
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return []
    chain, p = [], parents.get(pid, 0)
    while p > 1 and len(chain) < limit:
        chain.append(p)
        p = parents.get(p, 0)
    return chain






OWNER_DEPTH = 2


def owner(sessions, pid, ancestors=None) -> dict | None:
    "The claim that owns `pid`: the one recording `pid` itself, else the one recording\n    its nearest ancestor within OWNER_DEPTH (a relay pid resolves to the hook\n    claim of the Claude process that spawned it). When several claims record the same\n    process (a resumed or cleared session keeps its Claude process), the newest wins.\n\n    A claim's chain runs up past its harness to what holds every session on the machine (a\n    tmux server, a terminal). A claim that shares only such a process with `pid` is another\n    session's: `_elsewhere`."
    if not pid:
        return None
    sessions = list(sessions)
    if ancestors is None:
        ancestors = _ancestors(pid, limit=OWNER_DEPTH)
    lineage = [pid, *list(ancestors)[:OWNER_DEPTH]]
    for p in lineage:
        hits = [s for s in sessions if p in (s.get("pids") or [])
                and not _elsewhere(s["pids"], p, lineage)]
        if hits:
            return max(hits, key=lambda s: float(s.get("started") or 0))
    return None


def _elsewhere(chain, shared, lineage) -> bool:
    ' elsewhere.'
    return any(q not in lineage and _alive(q) for q in chain[:chain.index(shared)])


def _alive(pid) -> bool:
    if os.name == "nt":          
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError, OverflowError):
        return False
    return True


def caller(sessions, pid) -> dict | None:
    "The calling session's claim (see `owner`), or None."
    return owner(sessions, pid)











SERVER_CLAIM_PREFIX = "server-"


def ensure_server_claim(pid, cwd=None, project=None, project_id=None, file=None,
                        now=None) -> dict | None:
    if not pid:
        return None
    t = time.time() if now is None else now
    d = claims_dir()
    mine = d / f"{SERVER_CLAIM_PREFIX}{pid}.json"
    hook_claim = owner([s for s in read_live(now=t)
                        if not str(s.get("session", "")).startswith("pid")], pid)
    if hook_claim is not None:
        with contextlib.suppress(OSError):
            mine.unlink()                      
        return None
    rec = {}
    with contextlib.suppress(OSError, ValueError):
        rec = json.loads(mine.read_text(encoding="utf-8")) or {}
    rec.setdefault("session", f"pid{pid}")
    rec.setdefault("started", t)
    rec["pids"] = [int(pid)]
    rec["source"] = "server"
    rec["last_seen"] = t
    if cwd:
        rec["cwd"] = cwd
        rec["worktree"] = _worktree_of(cwd) or rec.get("worktree")
    if project:
        rec["project"] = project
    if project_id:
        rec["project_id"] = project_id
    if file:
        files = [f for f in (rec.get("files") or [])
                 if isinstance(f, dict) and f.get("path") != file]
        files.append({"path": file, "ts": int(t)})
        rec["files"] = files[-20:]
    try:
        from .paths import write_atomic
        d.mkdir(parents=True, exist_ok=True)
        write_atomic(mine, json.dumps(rec, separators=(",", ":")))
    except OSError:
        return None
    return rec


def _worktree_of(path):
    marker = "/.claude/worktrees/"
    i = (path or "").find(marker)
    if i < 0:
        return None
    rest = path[i + len(marker):]
    return rest.split("/", 1)[0] or None


def _in_store(path) -> bool:
    return ("/.agent-context" in (path or "") and "/.claude/worktrees/" not in (path or "")) \
        or "/.agent-context/" in (path or "")


def relevant(sessions, cwd, project_id=None, project_name=None, this_uuid=None,
             store_root=None, caller_session=None):
    "The sessions a caller at `cwd` should hear about: the same project (by id,\n    else by name), or both in the store. When the caller is KNOWN (`caller_session`,\n    from the connection's peer pid) its own claim is dropped exactly and every other\n    session is named, a second one in the same directory included. When it is not,\n    a session on this machine in this directory is most likely the caller and is\n    dropped when it is the only one there."
    def under_root(path):
        return bool(store_root and (
            path == store_root or (path or "").startswith(str(store_root).rstrip("/") + "/")))

    def in_store(s):
        
        
        return (s.get("project") == "agent-context" or _in_store(s.get("cwd"))
                or (s.get("machine_uuid") == this_uuid and under_root(s.get("cwd"))))

    here = _in_store(cwd) or under_root(cwd)
    same_dir = [s for s in sessions
                if s.get("machine_uuid") == this_uuid and s.get("cwd") == cwd]
    out = []
    for s in sessions:
        if caller_session:
            if s.get("machine_uuid") == this_uuid and s.get("session") == caller_session:
                continue
        elif s in same_dir and len(same_dir) == 1:
            continue
        if (project_id and s.get("project_id") == project_id) or (project_name and s.get("project") == project_name) or (here and in_store(s)):
            out.append(s)
    return out


def describe(s, now=None) -> str:
    t = time.time() if now is None else now
    age = max(0, int(t - float(s.get("last_seen") or 0)))
    mins = age // 60
    seen = "just now" if mins < 1 else f"{mins} min ago"
    where = s.get("worktree")
    where = f"worktree {where}" if where else (s.get("cwd") or "?")
    line = f"{s.get('machine')} · {s.get('project') or 'no project'} · {where} · seen {seen}"
    files = [os.path.basename(f) for f in (s.get("files") or [])]
    if files:
        line += " · editing " + ", ".join(files[-5:])
    return line


def store_paths_touched(sessions) -> dict:
    '{store-relative path: [session dicts]} for every store file a live session\n    has edited by hand, keyed the way the store keys its entities.'
    out = {}
    for s in sessions:
        for f in s.get("files") or []:
            marker = "/.agent-context/"
            i = (f or "").find(marker)
            if i < 0:
                continue
            rel = f[i + len(marker):]
            if rel.startswith(".claude/worktrees/"):
                rel = rel.split("/", 3)[-1] if rel.count("/") >= 3 else rel
            out.setdefault(rel, []).append(s)
    return out











REMOTE_CLAIM_PREFIX = "remote-"


RELAY_SEEN_BUCKET_SECS = 3600


def _relay_seen_path():
    return paths.state_dir() / "relay-seen.json"


def _relay_etag_path():
    return paths.state_dir() / "relay-etags.json"


def relay_etags() -> dict:
    '{machine_uuid: the relay source ETag its last session start sent, or None when it sent\n    none}. A machine that never reached ls is absent. An unreadable file is no record.'
    try:
        data = json.loads(_relay_etag_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): (v if isinstance(v, str) else None) for k, v in data.items()} \
        if isinstance(data, dict) else {}


_RELAY_ETAG_LOCK = threading.Lock()


def _machine_row_exists(store, machine_uuid) -> bool:
    with store.lock:
        return any(e.get("type") == "machine" and e.get("machine_uuid") == machine_uuid
                   for e in store.entities.values())


def note_relay_etag(machine_uuid, etag, store) -> bool:
    "Remember the relay source ETag a machine's session start reported. Only a machine with a\n    row in `store` is recorded. Written only when it changed. True when it wrote."
    if not machine_uuid or not _machine_row_exists(store, machine_uuid):
        return False
    with _RELAY_ETAG_LOCK:
        known = relay_etags()
        if str(machine_uuid) in known and known[str(machine_uuid)] == etag:
            return False
        known[str(machine_uuid)] = etag
        try:
            from .paths import write_atomic
            write_atomic(_relay_etag_path(),
                         json.dumps(known, separators=(",", ":"), sort_keys=True), mode=0o600)
        except OSError:
            return False
    return True


def _stamp(v) -> int | None:
    'An epoch-seconds stamp, or None for anything else (a bool, NaN, an infinity, a string).'
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return int(v)
    except (ValueError, OverflowError):
        return None


def relay_seen() -> dict:
    '{machine_uuid: epoch seconds} of the last session each remote machine started, to the\n    hour. Kept apart from the claims, which expire after half an hour, so a relay-only machine\n    that is idle overnight still shows when it was last here. An unreadable file is no record.'
    try:
        data = json.loads(_relay_seen_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): n for k, v in data.items() if (n := _stamp(v)) is not None}


def note_relay_seen(machine_uuid, now=None) -> bool:
    'Remember that a session of this machine reached ls. The stamp is bucketed to the hour\n    and rewritten at most once per bucket, so a busy machine costs one small write an hour.\n    True when it wrote.'
    if not machine_uuid:
        return False
    t = time.time() if now is None else now
    bucket = int(t // RELAY_SEEN_BUCKET_SECS * RELAY_SEEN_BUCKET_SECS)
    seen = relay_seen()
    if seen.get(str(machine_uuid), -1) >= bucket:
        return False
    seen[str(machine_uuid)] = bucket
    try:
        from .paths import write_atomic
        write_atomic(_relay_seen_path(), json.dumps(seen, separators=(",", ":"), sort_keys=True))
    except OSError:
        return False
    return True


def _remote_path(session_key):
    safe = "".join(c for c in str(session_key) if c.isalnum() or c in "-_")
    return claims_dir() / f"{REMOTE_CLAIM_PREFIX}{safe}.json"


def ensure_remote_claim(identity, cwd=None, project=None, project_id=None, now=None) -> dict | None:
    'Record (or renew) the live session `identity` names, under its own machine.'
    if identity is None or not identity.session_key or not identity.machine_uuid:
        return None
    t = time.time() if now is None else now
    p = _remote_path(identity.session_key)
    rec = {}
    with contextlib.suppress(OSError, ValueError):
        rec = json.loads(p.read_text(encoding="utf-8")) or {}
    rec.setdefault("session", identity.session_key)
    rec.setdefault("started", t)
    rec.update({"machine_uuid": identity.machine_uuid, "machine_id": identity.machine_id,
                "source": "remote", "last_seen": t})
    if cwd:
        rec["cwd"] = cwd
    if project:
        rec["project"] = project
    if project_id:
        rec["project_id"] = project_id
    try:
        from .paths import write_atomic
        claims_dir().mkdir(parents=True, exist_ok=True)
        write_atomic(p, json.dumps(rec, separators=(",", ":")))
    except OSError:
        return None
    return rec


def read_remote(machine_uuid=None, now=None, ttl=TTL_SECS) -> list[dict]:
    'Live claims of remote sessions, publishable and tagged with their machine: those\n    of `machine_uuid`, or of every remote machine when it is None. Expired files are removed.'
    t = time.time() if now is None else now
    d = claims_dir()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for name in names:
        if not (name.startswith(REMOTE_CLAIM_PREFIX) and name.endswith(".json")):
            continue
        try:
            rec = json.loads((d / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if t - float(rec.get("last_seen") or 0) > ttl:
            with contextlib.suppress(OSError):
                (d / name).unlink()
            continue
        if machine_uuid is not None and rec.get("machine_uuid") != machine_uuid:
            continue
        out.append({**_publishable(rec, t), "machine_uuid": rec.get("machine_uuid"),
                    "machine_id": rec.get("machine_id")})
    out.sort(key=lambda r: (r.get("last_seen") or 0), reverse=True)
    return out
