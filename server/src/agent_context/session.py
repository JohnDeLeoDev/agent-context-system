'Machines and the session bootstrap (get_session_context).\n\nSplit out of fstools.py (build 25); `fstools` re-exports everything so callers and\ntests keep importing `fstools as T`.'
from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import platform
import re
import socket
from pathlib import Path

from . import identity, token_table, write_guard
from . import project_resolve as PR
from . import projects as P
from .audit import (
    _DIGEST_PATH_RE,
    _SEV_RANK,
    _audit_row_text,
    audit_digest,
    list_audit_observations,
)
from .index import _MEM_LEGEND, _index_health, _mem_block
from .machine import get_chezmoi_machine_id, get_machine_uuid
from .shaping import _instr_bootstrap

_UNKNOWN_MACHINE_WARNING = (
    "This session reached the agent-context server without a machine identity, so the server "
    "does not know which machine it runs on. Its relay sent no identity headers. Install or "
    "update the agent-context relay on this machine (agent-context-relay-install), or give it "
    "a per-machine token. A long-running desktop app relay keeps its old code across "
    "sessions and may need restarting.")


_CLIENT_MACHINE_WARNING = "This is a client with no machine identity."


def _unknown_warning() -> str:
    'The warning for an unknown caller. The mobile connector runs no relay, so it gets a\n    short text; every other caller, and any failure to tell, gets the relay text.'
    try:
        who = write_guard.current()
        if who is not None and who.token_id == token_table.OAUTH_ID:
            return _CLIENT_MACHINE_WARNING
    except Exception:
        pass
    return _UNKNOWN_MACHINE_WARNING


def _stale_relay_warning(store, remote) -> str | None:
    'A note for a remote session whose relay runs another source than ls serves now, or None.\n    A relay that sent no ETag runs code older than the header, so it is stale too. A store with\n    no server tree, or any failure to tell, gives no note.'
    if remote is None:
        return None
    try:
        from .relay_source import current_etag
        current = current_etag(Path(store.root) / "server")
    except Exception:
        return None
    if remote.relay_etag == current:
        return None
    return ("This machine's agent-context relay runs older code than ls serves now. A relay "
            "switches onto a new release by itself within seconds (policy); one that has not "
            "predates that or failed to install it (its log says which). Restart the app that "
            "holds the relay open.")


_NO_MACHINE_ERROR = "this session has no machine identity; name the machine explicitly"


def _is_loopback(address: str) -> bool:
    'True when `address` is any spelling of a loopback host: the names, 127/8, ::1, an\n    IPv4-mapped IPv6 form, with or without brackets, a port or a zone.'
    text = address.strip().lower()
    if text == "localhost":
        return True
    if text.startswith("["):
        text = text[1:text.find("]")] if "]" in text else text[1:]
    elif text.count(":") == 1:
        text = text.split(":")[0]            
    text = text.split("%")[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def unknown_caller() -> bool:
    'True for a caller on the network (a non-loopback address) that has a valid bearer but no\n    bound machine identity. Such a caller is not the daemon host and must not be answered as\n    it. Loopback callers and calls with no bound Caller (local stdio, tests) are the host. Any\n    failure to tell means the host, the behavior before this check existed.'
    try:
        if identity.current() is not None:
            return False
        who = write_guard.current()
        ip = who.ip if who is not None else None
        return bool(ip) and not _is_loopback(ip)
    except Exception:
        return False


def _ensure_machine(store):
    "The calling machine's row. For a remote caller (a session identity is bound) that\n    is the row of the machine it runs on, read-only: this process is the daemon host, and\n    a row written here would describe the wrong machine. None when the store has no such\n    row (the ASGI wrapper refuses that case before any tool runs)."
    cur = identity.current()
    if cur is not None:
        return next((e for e in store.entities.values()
                     if e["type"] == "machine" and e.get("machine_uuid") == cur.machine_uuid), None)
    uid = get_machine_uuid()
    mid = get_chezmoi_machine_id()
    for e in store.entities.values():
        if e["type"] == "machine" and e.get("machine_uuid") == uid:
            if mid and e.get("machine_id") != mid:
                
                from .store import emit_toml
                with store.lock:
                    e["machine_id"] = mid
                    path = os.path.join(store.root, "machines", uid + ".toml")
                    row = {k: v for k, v in e.items()
                           if k not in ("scope", "uuid", "_path", "search_blob")}
                    store._write_atomic(path, emit_toml(row))
                    store._arm_commit(path)
            return e
    from .store import emit_toml, stable_uuid
    meta = {"type": "machine", "machine_uuid": uid, "hostname": socket.gethostname(),
            "platform": platform.system().lower(), "home_dir": str(Path.home()),
            "display_name": socket.gethostname().split(".")[0],
            **({"machine_id": mid} if mid else {})}
    path = os.path.join(store.root, "machines", uid + ".toml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    store._write_atomic(path, emit_toml(meta))
    store._arm_commit(path)
    meta["scope"] = "global"
    meta["uuid"] = stable_uuid("machine", "global", uid + ".toml")
    with store.lock:        
        store._index(meta, path)
    return meta


def list_machines(store):
    cur = None if unknown_caller() else identity.session_machine_uuid()
    out = []
    for e in store.entities.values():
        if e["type"] != "machine":
            continue
        out.append({"machine_uuid": e.get("machine_uuid"), "machine_id": e.get("machine_id"),
                    "hostname": e.get("hostname"),
                    "display_name": e.get("display_name"), "platform": e.get("platform"),
                    "home_dir": e.get("home_dir"), "is_current": e.get("machine_uuid") == cur})
    return out


def set_machine_display_name(store, display_name, machine=None):
    if not machine and unknown_caller():
        return {"error": _NO_MACHINE_ERROR}
    with store.lock:
        from .store import emit_toml
        target = machine or identity.session_machine_uuid()
        for e in store.entities.values():
            if e["type"] == "machine" and (e.get("machine_uuid") == target
                                           or e.get("display_name") == target or e.get("hostname") == target):
                e["display_name"] = display_name
                meta = {k: v for k, v in e.items() if not k.startswith("_") and k not in ("uuid", "scope")}
                store._write_atomic(e["_path"], emit_toml(meta))
                store._arm_commit(e["_path"])
                return {"machine_uuid": e.get("machine_uuid"), "display_name": display_name}
        return {"error": "machine not found"}


def set_machine_sleeps(store, sleeps, machine=None):
    'Record whether a machine SLEEPS, so fleet health stops reporting its silence.'
    return _set_machine_flag(store, "sleeps", sleeps, machine)


def set_machine_relay_only(store, relay_only, machine=None):
    'Record that a machine reaches the store through ls and runs no daemon of its own.\n\n    Such a machine publishes no status row, so its silence is the normal state and fleet\n    health must not report it. Declared, not detected, for the same reason as `sleeps`: a\n    relay-only machine and a dead daemon both stop publishing.'
    return _set_machine_flag(store, "relay_only", relay_only, machine)


def _set_machine_flag(store, key, value, machine):
    "Write one boolean fact onto a machine row. The machine is named by uuid, fleet id,\n    hostname or display name, and defaults to the caller's."
    if not machine and unknown_caller():
        return {"error": _NO_MACHINE_ERROR}
    with store.lock:
        from .store import emit_toml
        target = machine or identity.session_machine_uuid()
        for e in store.entities.values():
            if e["type"] == "machine" and (e.get("machine_uuid") == target
                                           or e.get("display_name") == target
                                           or e.get("hostname") == target
                                           or e.get("machine_id") == target):
                e[key] = bool(value)
                meta = {k: v for k, v in e.items() if not k.startswith("_") and k not in ("uuid", "scope")}
                store._write_atomic(e["_path"], emit_toml(meta))
                store._arm_commit(e["_path"])
                return {"machine_uuid": e.get("machine_uuid"),
                        "machine_id": e.get("machine_id"), key: bool(value)}
        return {"error": "machine not found"}


MACHINE_ACTIONS = ("set_display_name", "set_sleeps", "set_relay_only", "register_path",
                   "translate_paths")


def machine_admin(store, action, display_name=None, sleeps=None, machine=None,
                  cwd=None, project=None, from_machine=None, relay_only=None):
    'The per-machine setters behind one tool: each action runs the function it replaced,\n    unchanged. A bad action or a missing argument returns an error and writes nothing.'
    from . import integrity  
    if action == "set_display_name":
        if not isinstance(display_name, str) or not display_name.strip():
            return {"error": "set_machine needs display_name (a non-blank string)"}
        return set_machine_display_name(store, display_name, machine)
    if action == "set_sleeps":
        if not isinstance(sleeps, bool):
            return {"error": "set_machine needs sleeps (true or false)"}
        return set_machine_sleeps(store, sleeps, machine)
    if action == "set_relay_only":
        if not isinstance(relay_only, bool):
            return {"error": "set_machine needs relay_only (true or false)"}
        return set_machine_relay_only(store, relay_only, machine)
    if action == "register_path":
        if identity.current() is not None or unknown_caller():
            return {"error": "register_path writes a marker into a checkout on the daemon host's "
                    "disk, which is not this machine's. Write `.agents/project-id` in the "
                    "checkout locally, or use upsert_project(..., local_path=...) there."}
        if not cwd:
            return {"error": "register_path needs cwd"}
        return integrity.register_machine_path(store, cwd, project)
    if action == "translate_paths":
        return integrity.translate_project_paths(store, from_machine)
    return {"error": f"unknown action {action!r}; valid actions: {', '.join(MACHINE_ACTIONS)}"}


_BODY_STATUS = re.compile(r"^status:\s*([A-Za-z_-]+)\s*$", re.MULTILINE)


def _request_status(e):
    ' request status.'
    if e.get("status"):
        return str(e["status"])
    body = str(e.get("body") or "")
    if body.startswith("---\n"):
        end = body.find("\n---", 4)
        head = body[4:end] if end != -1 else ""
        m = _BODY_STATUS.search(head)
        if m:
            return m.group(1).lower()
    return "open"


def _inbox_items(store, path_prefix, scope):
    'Lean {path, title, status} rows for OPEN/ACKNOWLEDGED inbox docs under\n    path_prefix at the given scope. Skips done/declined and anything archived;\n    bodies are intentionally omitted to keep the bootstrap payload small.'
    out = []
    for e in store.entities.values():
        if e.get("type") != "doc" or e.get("scope") != scope:
            continue
        path = str(e.get("path", ""))
        if not path.startswith(path_prefix) or "archive/" in path:
            continue
        status = _request_status(e)
        if status not in ("open", "acknowledged"):
            continue
        out.append({"path": path, "title": e.get("title"), "status": status})
    return sorted(out, key=lambda i: i["path"])


_AUDIT_ROWS_MAX = 10
_AUDIT_BOOTSTRAP_SEVERITIES = ("blocker", "high")


def _neg_date(d):
    'Sort key that puts NEWER dates first while sorting ascending overall (so it\n    composes with the severity rank in one tuple).'
    return tuple(-c for c in str(d or "")[:10].encode())


def _audit_bootstrap(store, pname):
    "Open observations this session can actually act on.\n\n    Rows are capped at _AUDIT_ROWS_MAX newest-first; anything beyond the cap, and every\n    other project's open items, is reported as a count with the call that fetches them.\n    Never silently truncated — a hidden backlog is how observations rot."
    every = list_audit_observations(store, None, status="open")
    mine = [o for o in every
            if o.get("scope") == "universal" or (pname and o.get("project") == pname)]
    
    
    
    mine.sort(key=lambda o: (_SEV_RANK.get(o.get("severity") or "normal", 2),
                             _neg_date(o.get("observed_date"))))
    
    
    
    
    
    
    urgent = [o for o in mine if (o.get("severity") or "normal") in _AUDIT_BOOTSTRAP_SEVERITIES]
    shown = urgent[:_AUDIT_ROWS_MAX]
    hidden_urgent = len(urgent) - len(shown)
    routine = len(mine) - len(urgent)
    
    
    
    out: dict[str, object] = {"open_audit_observations": "\n".join(_audit_row_text(o) for o in shown)}
    elsewhere = len(every) - len(mine)
    if hidden_urgent or routine or elsewhere:
        out["open_audit_observations_more"] = {
            "older_in_scope": hidden_urgent, "routine_in_scope": routine,
            "other_projects": elsewhere,
            "fetch": 'list_audit_observations(status="open", compact=True)'}
    return out


def _unmatched_repo_warning(cwd, store_root=None):
    ' unmatched repo warning.'
    from .project_resolve import get_repo_root
    root = get_repo_root(cwd)
    if not root:
        return None
    if store_root:
        with contextlib.suppress(OSError):
            if os.path.realpath(root) == os.path.realpath(store_root):
                return None
    remotes = PR.get_git_remotes(cwd)
    if not remotes:
        return None
    return (f"cwd is inside git checkout {root} with remote(s) {', '.join(remotes[:3])} "
            f"but no store project matched, so project-scoped instructions and memory are "
            f"not loaded. Fix: register_path(cwd=cwd, project=<display_name>) for a known "
            f"project, or upsert_project(canonical_remote, display_name) for a new one.")


def get_session_context(store, cwd, caller_pid=None, evidence=None):
    remote = identity.current()      
    unknown = unknown_caller()       
    if unknown:
        machine = {"machine_uuid": None, "machine_id": None, "hostname": None,
                   "display_name": None, "platform": None, "home_dir": None}
    else:
        machine = _ensure_machine(store)
    if machine is None:
        return (identity.refusal(store, remote) if remote else None) or {"error": "machine not found"}
    proj = P.resolve_project(store, cwd, evidence=evidence) if evidence else P.resolve_project(store, cwd)
    pname = proj.get("display_name") if "error" not in proj else None
    
    project_warning = (None if pname or remote or unknown
                       else _unmatched_repo_warning(cwd, store.root))
    
    
    wsname = None if pname or remote or unknown else P.resolve_workspace_root(store, cwd)
    
    
    
    
    
    host = machine.get("machine_id") or machine.get("hostname") or ""
    short = host.split(".")[0]
    inbox = [] if unknown else _inbox_items(store, f"inbox/machines/{host}/", "global")
    if short and short != host:
        seen = {i["path"] for i in inbox}
        inbox += [i for i in _inbox_items(store, f"inbox/machines/{short}/", "global")
                  if i["path"] not in seen]
    if pname:
        inbox += _inbox_items(store, "inbox/", f"project:{pname}")
    
    
    inbox = [i for i in inbox if not _DIGEST_PATH_RE.match(i["path"])]
    digest = {"rows": []} if unknown else audit_digest(store, short or host)
    if digest["rows"]:
        inbox.append({"path": digest["path"], "title": digest["title"], "status": "open",
                      "generated": True})
    
    
    
    mems = store.list("memory", pname, workspace=wsname)
    memory_index = {"legend": _MEM_LEGEND,
                    "global": _mem_block([e for e in mems if e.get("scope") == "global"])}
    if pname:
        memory_index["project"] = _mem_block([e for e in mems if e.get("scope") != "global"])
    elif wsname:
        memory_index["workspace"] = _mem_block([e for e in mems
                                                if e.get("scope") == f"ws:{wsname}"])
    out = {
        "machine": {"machine_uuid": machine.get("machine_uuid"), "machine_id": machine.get("machine_id"),
                    "hostname": machine.get("hostname"),
                    "display_name": machine.get("display_name"), "platform": machine.get("platform"),
                    "home_dir": machine.get("home_dir"),
                    **({"unknown": True} if unknown else {})},
        **({"machine_warning": _unknown_warning()} if unknown else
           {"machine_warning": w} if (w := _stale_relay_warning(store, remote)) else {}),
        "project": (None if "error" in proj else proj),
        **({"workspace": wsname} if wsname else {}),
        **({"project_warning": project_warning} if project_warning else {}),
        
        
        
        "instructions": [_instr_bootstrap(e) for e in
                         store.get_instructions(pname, load_behavior="always",
                                                workspace=wsname)],
        "memory_index": memory_index,
        
        
        **_audit_bootstrap(store, pname),
        "inbox": inbox,
    }
    health = _index_health(store, pname)
    if health:   
        out["index_health"] = health
    
    
    
    
    
    
    
    rows = []
    with contextlib.suppress(Exception):
        from . import fleet
        
        
        
        store._maybe_fetch_for_guard()
        rows = fleet.read_all(store.root)
        fleet.add_early_reachability(rows)
        probs = fleet.problems(rows)
        if probs:
            out["fleet_health"] = probs
    
    
    
    
    
    
    with contextlib.suppress(Exception):
        from . import claims
        pid = proj.get("id") if "error" not in proj else None
        
        
        if unknown:
            
            me = None
            caller_pid = None
        elif remote is not None:
            
            
            claims.note_relay_seen(remote.machine_uuid)      
            
            me = None if remote.helper else claims.ensure_remote_claim(
                remote, cwd=cwd, project=pname, project_id=pid)
            if me:
                me = {**me, "session": str(me["session"])[:8]}   
            caller_pid = None
        else:
            me = claims.caller(claims.read_live(), caller_pid)
        if caller_pid and not me:
            
            
            me = claims.ensure_server_claim(caller_pid, cwd=cwd, project=pname,
                                            project_id=pid)
        others = claims.relevant(claims.fleet_sessions(rows), cwd, project_id=pid,
                                 project_name=pname,
                                 this_uuid=str(machine.get("machine_uuid")),
                                 store_root=store.root,
                                 caller_session=(me or {}).get("session"))
        if others:
            out["active_sessions"] = {
                "note": ("other live sessions on this project or in the store, fleet-wide "
                         "(a claim is at most about a minute old). Do not repeat their "
                         "work: read what they edited before editing it, and say so to "
                         "user if you are both on the same task."
                         + ("" if me else " A session on this machine in this directory "
                                          "may be you.")),
                "sessions": [claims.describe(s) for s in others[:8]],
            }
    
    
    
    
    
    
    with contextlib.suppress(Exception):
        if remote is not None or unknown:
            raise LookupError("the daemon host's health is not this caller's machine")
        from . import daemon, fleet
        h = daemon.get_health()
        if h.get("verdict") not in (None, "healthy", "starting"):
            looping = h.get("verdict") == "restart-loop"
            out["daemon_health"] = {
                "verdict": h.get("verdict"),
                "reason": (f"the daemon has started {h.get('starts_last_hour')} times in "
                           f"the last hour" if looping else h.get("last_sync_error")),
                "log_hint": h.get("log_hint"),
                "note": (fleet.restart_loop_message("session")
                         if looping else
                         ("this machine's store is NOT syncing: what other machines wrote "
                          "is not here, and what this session writes is not reaching them. "
                          "Say so, and fix or hand off before relying on the store.")),
            }
    return out










_RENDERED_KEYS = {"machine", "machine_warning", "project", "workspace", "project_warning",
                  "instructions", "memory_index", "open_audit_observations",
                  "open_audit_observations_more", "inbox", "index_health", "fleet_health",
                  "active_sessions", "daemon_health"}


def _inline(v) -> str:
    'One value on one line: scalars as-is, containers as compact JSON.'
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), default=str, ensure_ascii=False)
    return str(v)


def _kv_lines(d: dict) -> list[str]:
    return [f"- {k}: {_inline(v)}" for k, v in d.items() if v not in (None, "", [], {})]


def _generic(v) -> list[str]:
    if isinstance(v, dict):
        return _kv_lines(v)
    if isinstance(v, list):
        return [f"- {_inline(x)}" for x in v]
    return [str(v)]


def _mem_scope_lines(label: str, block: dict) -> list[str]:
    out = [f"### {label}"]
    if block.get("rows"):
        out.append(block["rows"])
    if block.get("lazy"):
        out.append(f"lazy: {block['lazy']}")
    if block.get("over_budget"):
        out.append(f"over_budget: {block['over_budget']}")
    return out


def render_session_context(ctx: dict) -> str:
    'The bootstrap dict as markdown: one `##` section per part, instruction bodies\n    verbatim, the memory index and audit rows as the same compact text rows. A key this\n    renderer does not know is still emitted under its own heading, so a field added to\n    get_session_context can never be silently dropped from the session.'
    out: list[str] = ["# Session context"]

    m = ctx.get("machine") or {}
    ident = " · ".join(f"{k} {m[k]}" for k in ("machine_id", "hostname", "platform",
                                               "machine_uuid", "home_dir") if m.get(k))
    out += ["", "## Machine", f"{m.get('display_name') or '(unknown)'}: {ident}".rstrip(": ")]
    if m.get("unknown"):
        out.append("unknown: true")
    if ctx.get("machine_warning"):
        out.append(f"WARNING: {ctx['machine_warning']}")

    out += ["", "## Project"]
    proj = ctx.get("project")
    out += _kv_lines(proj) if proj else ["none"]
    if ctx.get("workspace"):
        out.append(f"workspace root: {ctx['workspace']}")
    if ctx.get("project_warning"):
        out.append(f"WARNING: {ctx['project_warning']}")

    for ins in ctx.get("instructions") or []:
        where = f" (project {ins['project']})" if ins.get("project") else ""
        out += ["", f"## Instruction: {ins.get('title')}{where}", "", ins.get("body") or ""]

    mi = ctx.get("memory_index") or {}
    out += ["", "## Memory index", mi.get("legend") or ""]
    for scope in ("global", "workspace", "project"):
        if scope in mi:
            label = scope if scope != "project" else f"project {(proj or {}).get('display_name', '')}".strip()
            out += ["", *_mem_scope_lines(label, mi[scope])]

    rows = ctx.get("open_audit_observations")
    more = ctx.get("open_audit_observations_more")
    if rows or more:
        out += ["", "## Open audit observations"]
        if rows:
            out.append(rows)
        if more:
            fetch = more.get("fetch")
            counts = ", ".join(f"{k} {v}" for k, v in more.items() if k != "fetch")
            out.append(f"more: {counts}" + (f". Fetch: {fetch}" if fetch else ""))

    if ctx.get("inbox"):
        out += ["", "## Inbox"]
        for i in ctx["inbox"]:
            out.append(f"- {i.get('path')}: {i.get('title')} ({i.get('status')}"
                       + (", generated" if i.get("generated") else "") + ")")

    for key, title in (("index_health", "Index health"), ("fleet_health", "Fleet health")):
        if ctx.get(key):
            out += ["", f"## {title}", *_generic(ctx[key])]

    act = ctx.get("active_sessions")
    if act:
        out += ["", "## Active sessions", act.get("note") or ""]
        out += [f"- {s}" for s in act.get("sessions") or []]

    dh = ctx.get("daemon_health")
    if dh:
        out += ["", "## Daemon health", f"verdict: {dh.get('verdict')}"]
        if dh.get("reason"):
            out.append(f"reason: {dh['reason']}")
        if dh.get("log_hint"):
            out.append(f"log: {dh['log_hint']}")
        if dh.get("note"):
            out.append(dh["note"])

    for key, v in ctx.items():
        if key not in _RENDERED_KEYS and v not in (None, "", [], {}):
            out += ["", f"## {key}", *_generic(v)]

    return "\n".join(out) + "\n"
