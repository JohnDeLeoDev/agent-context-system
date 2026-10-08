'Inter-agent messaging (policy): who can be addressed, the envelope, and the mailbox.\n\nModeled on Claude Code\'s cross-session messaging, so an agent that knows `ListAgents` and\n`SendMessage` needs nothing new: a row\'s name is the address, ` [ref]` is appended only to\ntell two sessions with one name apart, and a message arrives wrapped as\n`<cross-session-message from="...">`, answered by copying `from` into `to`.\n\nAn address is a session connected to the daemon. Every session on the fleet reaches the one\ndaemon on ls through its bridge, so the daemon\'s connected sessions are every session there\nis, on any machine. The session\'s claim (claims.py) supplies the name and the ref. A name is\nderived: the project, plus the worktree when the session works in one.\n\nThe mailbox holds only what has not reached its session: a message whose recipient was woken\nthrough its bridge (peer_wake.py) is never stored. It lives in the state directory, never in\ngit, so chatter makes no store commits. This module keeps no connection state; server.py\nowns the map from a claim to a connected session.'
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

from . import claims, paths

MAX_MESSAGE_CHARS = 16000
MAX_QUEUED = 100              
MESSAGE_TTL_SECS = 86400.0    
RATE_LIMIT = 30               
RATE_WINDOW_SECS = 60.0

_REF = re.compile(r"^(.*?)\s*\[([^\]\s]+)\]\s*$")
_LOCK = threading.Lock()
_SENDS: dict[str, list[float]] = {}


class Refused(Exception):
    "The send or the address cannot be honored; the message is the tool's answer."


def derive_name(rec: dict) -> str:
    "A session's name: its project (else its directory, else its machine), plus the worktree."
    base = rec.get("project") or os.path.basename(str(rec.get("cwd") or "").rstrip("/")) \
        or rec.get("machine") or "session"
    worktree = rec.get("worktree")
    return f"{base}:{worktree}" if worktree else str(base)


def _agent(rec: dict, machine: str | None, machine_uuid: str | None) -> dict:
    out = {"ref": str(rec.get("session") or "")[:8], "machine": machine or "?",
           "machine_uuid": machine_uuid, "cwd": rec.get("cwd"),
           "project": rec.get("project"), "worktree": rec.get("worktree"),
           "last_seen": rec.get("last_seen") or 0}
    out["name"] = derive_name({**rec, "machine": out["machine"]})
    return out


def roster(owners: list[dict], host_machine: str | None, host_uuid: str | None,
           now: float | None = None) -> list[dict]:
    "One agent per session connected to the daemon, in the order of `owners`.\n\n    An owner is what server.py knows of one MCP session: `sid`, `connected` (its event stream\n    is open), `wake` (the route its bridge declared), `notice` (its bridge takes\n    the `peer_waiting` push for the session's hook), and either `pid` (a session on the\n    daemon's host, named by its claim through the process ancestry) or `session_key` with\n    `machine_uuid` and `machine_id` (a relay session). The claim supplies the name and the\n    ref; a session with no claim yet is named by `cwd`, the directory its bridge declared. A claim expires after thirty idle minutes and a connected session does not, so the\n    last claim seen is kept on the owner (`claim`) and used once the file is gone. Two MCP\n    sessions of one claim (a bridge that reconnected, or two bridges of one harness) are one\n    agent, reached through the first listed one that is connected and, among those, one that\n    declared a wake route (`_reach`)."
    live = claims.read_live(now=now)
    remote = claims.read_remote(now=now)
    chosen = names()
    by_key: dict[str, dict] = {}
    for o in owners:
        if o.get("session_key"):
            short = str(o["session_key"])[:8]
            rec = next((r for r in remote if r.get("session") == short
                        and r.get("machine_uuid") == o.get("machine_uuid")), None)
            machine, machine_uuid = o.get("machine_id"), o.get("machine_uuid")
            fallback = {"session": short, "cwd": o.get("cwd")}
        elif o.get("pid"):
            if "chain" not in o:
                o["chain"] = claims._ancestors(o["pid"], limit=claims.OWNER_DEPTH)
            rec = claims.owner(live, o["pid"], ancestors=o["chain"])
            machine, machine_uuid = host_machine, host_uuid
            fallback = {"session": f"p{o['pid']}", "cwd": o.get("cwd")}
            if rec is not None and str(rec.get("session") or "").startswith("pid"):
                
                
                
                rec = {**rec, "session": f"p{(rec.get('pids') or [o['pid']])[0]}"}
        else:
            continue
        if rec is not None:
            o["claim"] = rec
        agent = _agent(rec or o.get("claim") or fallback, machine, machine_uuid)
        agent["name"] = chosen.get(key(agent)) or agent["name"]
        agent.update(sid=o.get("sid"), wake=o.get("wake"), connected=bool(o.get("connected")),
                     notice=bool(o.get("notice")), sids=[o.get("sid")], turn=o.get("turn"))
        first = by_key.setdefault(key(agent), agent)
        if first is not agent:
            first["sids"].append(o.get("sid"))
            first["turn"] = first["turn"] or agent["turn"]   
            if _reach(agent) > _reach(first):
                first.update(sid=agent["sid"], wake=agent["wake"], notice=agent["notice"],
                             connected=agent["connected"])
    return list(by_key.values())


def _reach(agent: dict) -> tuple[bool, bool]:
    "How well one MCP session of an agent can be reached: connected first, then able to\n    wake. A harness may start two bridges for one session (pi does: its extension's, which\n    declares the pi route, and one a package starts from another harness's MCP config, which\n    declares none), and the message must go down the one that starts a turn."
    return bool(agent.get("connected")), bool(agent.get("wake"))









MAX_NAME_CHARS = 64
_NAME_BAD = re.compile(r"[\[\]\x00-\x1f]| · ")
MAX_NAMES = 500


def names_path():
    return paths.state_dir() / "agent-names.json"


def names() -> dict[str, str]:
    'Mailbox key -> the name that session chose.'
    try:
        data = json.loads(names_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def set_name(agent_key: str, name: str) -> str:
    'Record the name a session chose and return it; an empty name goes back to the derived\n    one. Raises Refused for a name that could not be an address.'
    name = " ".join((name or "").split())
    if len(name) > MAX_NAME_CHARS or _NAME_BAD.search(name):
        raise Refused(f"a name is at most {MAX_NAME_CHARS} characters, with no square bracket "
                      "and no ` · `: those mark the ref and the columns of a list_agents row")
    with _LOCK:
        chosen = names()
        chosen.pop(agent_key, None)
        if name:
            chosen[agent_key] = name
        while len(chosen) > MAX_NAMES:              
            chosen.pop(next(iter(chosen)))
        paths.write_atomic(names_path(), json.dumps(chosen, separators=(",", ":")), mode=0o600)
    return name







MAX_WATCHES = 500
WATCH_TTL_SECS = 24 * 3600


def watches_path():
    return paths.state_dir() / "idle-watches.json"


def _watches(now: float) -> dict[str, list[dict]]:
    try:
        data = json.loads(watches_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, rows in data.items():
        kept = [r for r in rows if isinstance(r, dict) and isinstance(r.get("sender"), str)
                and now - float(r.get("at") or 0) < WATCH_TTL_SECS] if isinstance(rows, list) else []
        if kept:
            out[str(k)] = kept
    return out


def _write_watches(table: dict[str, list[dict]]) -> None:
    while sum(len(rows) for rows in table.values()) > MAX_WATCHES:
        table.pop(next(iter(table)))                
    paths.write_atomic(watches_path(), json.dumps(table, separators=(",", ":")), mode=0o600)


def watch_idle(watched_key: str, sender_key: str, message_id: str, now: float | None = None) -> None:
    'Record that `sender_key` asked to hear when the session `watched_key` next ends a turn.'
    t = time.time() if now is None else now
    with _LOCK:
        table = _watches(t)
        table.setdefault(watched_key, []).append({"sender": sender_key, "id": message_id, "at": t})
        _write_watches(table)


def take_watchers(watched_key: str, now: float | None = None) -> list[dict]:
    'The requests waiting on `watched_key`, removed: each notice is sent once.'
    t = time.time() if now is None else now
    with _LOCK:
        table = _watches(t)
        rows = table.pop(watched_key, [])
        if rows:
            _write_watches(table)
    return rows


def key(agent: dict) -> str:
    'The mailbox key of a session: its machine and its ref.'
    raw = f"{agent.get('machine_uuid') or agent.get('machine')}.{agent.get('ref')}"
    return "".join(c for c in raw if c.isalnum() or c in "-_.")


def wakeable(agent: dict) -> bool:
    "True when a push to this session's bridge can wake it now. A Codex session is woken\n    on its thread, and it has none (so no hook claim, and a `p<pid>` ref) before its first\n    prompt: until then its messages wait in the mailbox."
    if not agent.get("wake"):
        return False
    return not (agent["wake"] == "codex" and str(agent.get("ref") or "").startswith("p"))


def label(agent: dict) -> str:
    return f"{agent['name']} [{agent['ref']}]"


def describe(agent: dict, now: float | None = None) -> str:
    "One `list_agents` row. It leads with `name [ref]`, as Claude Code's rows do."
    parts = [label(agent), str(agent["machine"])]
    if agent.get("cwd"):
        parts.append(str(agent["cwd"]))
    if agent.get("last_seen"):          
        t = time.time() if now is None else now
        mins = max(0, int(t - float(agent["last_seen"]))) // 60
        parts.append("seen just now" if mins < 1 else f"seen {mins} min ago")
    if agent.get("turn"):               
        parts.append(str(agent["turn"]))
    return " · ".join(parts)


def resolve(everyone: list[dict], to: str, sender: dict | None = None) -> dict:
    'The one session `to` names: a bare name, `name [ref]`, or a ref alone.'
    to = (to or "").strip()
    if not to:
        raise Refused("`to` is empty: pass a name from list_agents")
    others = [a for a in everyone if sender is None or key(a) != key(sender)]
    m = _REF.match(to)
    if m:
        name, ref = m.group(1), m.group(2)
        hits = [a for a in others if a["ref"] == ref and (not name or a["name"] == name)]
    else:
        hits = [a for a in others if a["name"] == to] or [a for a in others if a["ref"] == to]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        if sender is not None and any(key(a) == key(sender) for a in everyone
                                      if to in (a["name"], a["ref"], label(a))):
            raise Refused(f"{to!r} is this session: a session does not message itself")
        raise Refused(f"no live session found with the name {to!r}. Call list_agents for the "
                      "current names. For a target with no live session, file a request "
                      "(get_doc(\"inter-agent-requests.md\")).")
    raise Refused(f"{len(hits)} live sessions are named {to!r}; send to one of: "
                  + ", ".join(label(a) for a in hits))


VIA = "agent-context"


def envelope(sender_label: str, text: str, channel: str | None = None) -> str:
    'What the recipient reads. Copying `from` into `to` answers it. `via` is the one\n    attribute Claude Code\'s envelope lacks: it tells the redirect hook that `from` is a store\n    address, to be answered with send_message and not the native SendMessage. A message that\n    went to a channel names it: `to="#name"` answers everyone in it.'
    safe = sender_label.replace("&", "&amp;").replace('"', "&quot;")
    where = "" if channel is None else ' channel="#%s"' % channel.replace(
        "&", "&amp;").replace('"', "&quot;")
    return (f'<cross-session-message from="{safe}" via="{VIA}"{where}>\n{text}\n'
            "</cross-session-message>")










ALL = "all"
MAX_CHANNELS = 200
MAX_HISTORY = 50                  
HISTORY_TTL_SECS = 86400.0
MEMBER_TTL_SECS = 86400.0         
_CHANNEL = re.compile(r"^#(.*)$", re.S)


def channel_name(to: str | None) -> str | None:
    "The channel `to` names (`#name`), None when `to` is a session's address. Raises\n    Refused for a name no channel can have."
    m = _CHANNEL.match((to or "").strip())
    if not m:
        return None
    name = " ".join(m.group(1).split())
    if not name or len(name) > MAX_NAME_CHARS or "#" in name or _NAME_BAD.search(name):
        raise Refused(f"a channel is `#name`: at most {MAX_NAME_CHARS} characters, with no "
                      "`#`, no square bracket and no ` · ` in the name")
    return name


def channel_arg(raw: str) -> str:
    'The channel a `join`, `leave` or `channel` argument names, with or without its `#`.'
    name = channel_name("#" + (raw or "").strip().lstrip("#"))
    assert name is not None
    return name


def channels_path():
    return paths.state_dir() / "channels.json"


def _channels() -> dict[str, dict]:
    'Folded name -> {"name": as first written, "members": {mailbox key: last seen live}}.'
    try:
        data = json.loads(channels_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: {"name": str(v.get("name") or k),
                "members": {m: float(at) for m, at in v["members"].items()
                            if isinstance(at, (int, float))}}
            for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get("members"), dict)}


def _write_channels(table: dict[str, dict], live: set[str], now: float) -> None:
    'Save who joined what. A member that is live is stamped; one gone for a day is dropped,\n    and so is a channel nobody is left in.'
    for k in list(table):
        members = table[k]["members"]
        for m in list(members):
            if m in live:
                members[m] = now
            elif now - members[m] > MEMBER_TTL_SECS:
                del members[m]
        if not members:
            del table[k]
    while len(table) > MAX_CHANNELS:
        table.pop(next(iter(table)))                    
    paths.write_atomic(channels_path(), json.dumps(table, separators=(",", ":")), mode=0o600)


def _live_keys(everyone: list[dict]) -> set[str]:
    return {key(a) for a in everyone if a.get("connected")}


def join(name: str, agent: dict, everyone: list[dict], now: float | None = None) -> None:
    t = time.time() if now is None else now
    with _LOCK:
        table = _channels()
        table.setdefault(name.casefold(), {"name": name, "members": {}})["members"][key(agent)] = t
        _write_channels(table, _live_keys(everyone) | {key(agent)}, t)


def leave(name: str, agent: dict, everyone: list[dict], now: float | None = None) -> bool:
    'Take the session out of a channel it joined. False when it had not joined it.'
    t = time.time() if now is None else now
    with _LOCK:
        table = _channels()
        had = table.get(name.casefold(), {"members": {}})["members"].pop(key(agent), None)
        if had is not None:
            _write_channels(table, _live_keys(everyone), t)
    return had is not None


def _automatic(agent: dict, folded: str, projects: frozenset[str]) -> bool:
    
    
    
    cwd = str(agent.get("cwd") or "")
    project = agent.get("project") or os.path.basename(cwd.rstrip("/\\"))
    if folded in (ALL, str(project or "").casefold(), str(agent.get("machine") or "").casefold()):
        return True
    
    
    
    return folded in projects and folded in {p.casefold() for p in re.split(r"[/\\]", cwd) if p}


def members(everyone: list[dict], name: str, projects: frozenset[str] = frozenset()) -> list[dict]:
    "The live sessions in a channel: those it holds by rule, and those that joined.\n    `projects` is every project's name, folded."
    folded = name.casefold()
    joined = _channels().get(folded, {"members": {}})["members"]
    return [a for a in everyone if a.get("connected")
            and (_automatic(a, folded, projects) or key(a) in joined)]


def joined_channels(everyone: list[dict]) -> str | None:
    'One line naming each channel a live session joined, None when there is none.'
    live = _live_keys(everyone)
    rows = [(c["name"], len(live & set(c["members"]))) for c in _channels().values()]
    rows = sorted((name, n) for name, n in rows if n)
    if not rows:
        return None
    return "Joined channels: " + ", ".join(f"#{name} ({n})" for name, n in rows)


def history_dir():
    return paths.state_dir() / "channel-history"


def _history_path(name: str):
    import hashlib
    folded = name.casefold()
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in folded)[:40]
    return history_dir() / f"{safe}-{hashlib.sha256(folded.encode('utf-8')).hexdigest()[:12]}.json"


def history(name: str, now: float | None = None) -> list[dict]:
    'A channel\'s posts of the past day, oldest first: `{"id", "text", "ts"}`.'
    t = time.time() if now is None else now
    try:
        data = json.loads(_history_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [m for m in data if isinstance(m, dict) and isinstance(m.get("text"), str)
            and t - float(m.get("ts") or 0) < HISTORY_TTL_SECS]


def record(name: str, message_id: str, text: str, now: float | None = None) -> None:
    "Keep one enveloped post in its channel's history."
    t = time.time() if now is None else now
    path = _history_path(name)
    with _LOCK:
        posts = history(name, t)
        posts.append({"id": message_id, "text": text, "ts": t})
        path.parent.mkdir(parents=True, exist_ok=True)
        paths.write_atomic(path, json.dumps(posts[-MAX_HISTORY:], separators=(",", ":")),
                           mode=0o600)


NOTICE_FROM = "agent-context"


def notice(text: str) -> str:
    "One of the daemon's own notices (a failed wake, an idle session), in the envelope a\n    session already knows. `from` names the store, which is no address: there is nothing to\n    reply to."
    return envelope(NOTICE_FROM, text)


def check_send(sender_key: str, text: str, now: float | None = None) -> None:
    'Refuse an oversized message or a sender past its rate; else count the send.'
    if len(text) > MAX_MESSAGE_CHARS:
        raise Refused(f"message is {len(text)} characters; the cap is {MAX_MESSAGE_CHARS}. "
                      "Put the content in a file or a store doc and send its path.")
    t = time.time() if now is None else now
    with _LOCK:
        recent = [s for s in _SENDS.get(sender_key, []) if t - s < RATE_WINDOW_SECS]
        if len(recent) >= RATE_LIMIT:
            _SENDS[sender_key] = recent
            raise Refused(f"rate limit: {RATE_LIMIT} messages per {int(RATE_WINDOW_SECS)} s "
                          "from one session")
        recent.append(t)
        _SENDS[sender_key] = recent
        for k in [k for k, v in _SENDS.items() if not v or t - v[-1] >= RATE_WINDOW_SECS]:
            if k != sender_key:
                del _SENDS[k]


def new_id() -> str:
    return uuid.uuid4().hex[:12]




def mailbox_dir():
    return paths.state_dir() / "mailboxes"


def _read(path, now: float) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [m for m in data if isinstance(m, dict)
            and now - float(m.get("ts") or 0) < MESSAGE_TTL_SECS]


def enqueue(recipient_key: str, message_id: str, text: str, now: float | None = None) -> int:
    'Hold an enveloped message for a session that could not be woken. Returns how many\n    wait for it. Raises Refused when its mailbox is full.'
    t = time.time() if now is None else now
    path = mailbox_dir() / f"{recipient_key}.json"
    with _LOCK:
        queued = _read(path, t)
        if len(queued) >= MAX_QUEUED:
            raise Refused(f"that session already has {MAX_QUEUED} unread messages; "
                          "it has to read them first")
        queued.append({"id": message_id, "text": text, "ts": t})
        path.parent.mkdir(parents=True, exist_ok=True)
        paths.write_atomic(path, json.dumps(queued, separators=(",", ":")), mode=0o600)
        return len(queued)


def drain(recipient_key: str, now: float | None = None) -> list[dict]:
    'Every message waiting for a session, oldest first, removed from the mailbox.'
    t = time.time() if now is None else now
    path = mailbox_dir() / f"{recipient_key}.json"
    with _LOCK:
        queued = _read(path, t)
        if queued:
            paths.write_atomic(path, "[]", mode=0o600)
        return queued


def waiting(recipient_key: str, now: float | None = None) -> int:
    t = time.time() if now is None else now
    with _LOCK:
        return len(_read(mailbox_dir() / f"{recipient_key}.json", t))
