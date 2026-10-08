'Which machine a session runs on, when the daemon serves many machines.\n\nOne daemon on ls serves every relay, so "this machine" cannot come from the daemon\'s own\nhost. The relay sends its machine identity as connection headers; the ASGI wrapper binds\nit to the session at `initialize`; tools read it back from `current()`.\n\n`machine.get_machine_uuid()` and `get_chezmoi_machine_id()` stay the HOST identity: sync\nand commit code compare against them, and a per-session value leaking in there would make\nthe daemon treat its own machine row as foreign. Tool-level sites use the `session_*`\nfunctions below, which fall back to the host identity when no session identity is bound.'
from __future__ import annotations

import contextvars
import os
import platform
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import machine
from .relay_update import start_etag, valid_etag

HEADER_MACHINE = "x-agent-context-machine"
HEADER_MACHINE_ID = "x-agent-context-machine-id"
HEADER_PLATFORM = "x-agent-context-platform"
HEADER_HOME = "x-agent-context-home"
HEADER_RELAY_ETAG = "x-relay-source-etag"
EVIDENCE_META_KEY = "agent-context/project-evidence"


@dataclass(frozen=True)
class Identity:
    machine_uuid: str
    machine_id: str | None
    platform: str | None
    home: str | None
    session_key: str | None = None
    relay_etag: str | None = None
    
    
    
    helper: bool = False


IDENTITY_FILE = "relay-identity.json"


def publish_local_headers(headers: Mapping[str, str]) -> None:
    "Relay side: leave the identity headers a bridge sends where this machine's hooks and\n    scripts read them (`store_mcp.py`, which runs on the system python and cannot import\n    this package), so their calls name the same machine. Written only when it changed.\n    Never raises: a bridge that cannot write it still connects."
    import json

    from . import paths
    try:
        path = paths.state_dir() / IDENTITY_FILE
        text = json.dumps(dict(sorted(headers.items())), separators=(",", ":"))
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
        paths.write_atomic(path, text, mode=0o600)
    except Exception:   
        pass


_SESSION: contextvars.ContextVar[Identity | None] = contextvars.ContextVar(
    "agent_context_session_identity", default=None)


def local_headers() -> dict[str, str]:
    "Relay side: this machine's identity as HTTP headers."
    out = {HEADER_MACHINE: machine.get_machine_uuid(),
           HEADER_PLATFORM: platform.system().lower(),
           HEADER_HOME: os.environ.get("HOME") or str(Path.home())}
    mid = machine.get_chezmoi_machine_id()
    if mid:
        out[HEADER_MACHINE_ID] = mid
    out[HEADER_RELAY_ETAG] = start_etag(Path(os.environ.get("HOME") or Path.home()))
    return {k: v for k, v in out.items() if v}


def _text(value: bytes | str) -> str:
    return (value.decode("utf-8", "replace") if isinstance(value, bytes) else value).strip()


def from_headers(pairs: Iterable[tuple[bytes | str, bytes | str]]) -> Identity | None:
    "Server side: the identity in a request's headers, or None when it carries none."
    got = {_text(k).lower(): _text(v) for k, v in pairs}
    uuid = got.get(HEADER_MACHINE)
    if not uuid:
        return None
    return Identity(machine_uuid=uuid,
                    machine_id=got.get(HEADER_MACHINE_ID) or None,
                    platform=got.get(HEADER_PLATFORM) or None,
                    home=got.get(HEADER_HOME) or None,
                    relay_etag=valid_etag(got.get(HEADER_RELAY_ETAG, "")) or None)


def bind(identity: Identity | None) -> contextvars.Token[Identity | None]:
    return _SESSION.set(identity)


def reset(token: contextvars.Token[Identity | None]) -> None:
    _SESSION.reset(token)


def current() -> Identity | None:
    return _SESSION.get()


def session_machine_uuid() -> str:
    "The calling session's machine uuid; the daemon host's own when none is bound."
    cur = current()
    return cur.machine_uuid if cur else machine.get_machine_uuid()


def session_machine_id() -> str | None:
    "The calling session's fleet machine id; the daemon host's own when none is bound."
    cur = current()
    return cur.machine_id if cur else machine.get_chezmoi_machine_id()


def refusal(store, identity: Identity) -> dict | None:
    'None when `identity` names a machine in the store; else an error naming the known ones.'
    known = [e for e in store.entities.values() if e.get("type") == "machine"]
    if any(e.get("machine_uuid") == identity.machine_uuid for e in known):
        return None
    names = sorted(str(e.get("machine_id") or e.get("hostname") or e.get("machine_uuid"))
                   for e in known)
    return {"error": (f"unknown machine {identity.machine_uuid!r}: no machine row in the store "
                      f"has that uuid. Known machines: {', '.join(names) or 'none'}"),
            "known_machines": names}


def project_evidence(cwd: str) -> dict:
    "Relay side: what this machine's disk says about `cwd`'s project."
    from . import project_resolve as PR
    from . import projects as P
    marker = P._read_project_marker(PR.get_repo_root(cwd))
    return {"cwd": cwd,
            "marker_id": marker.get("id") if marker else None,
            "remotes": list(PR.get_git_remotes(cwd) or [])}


def add_project_evidence(params: Mapping) -> dict:
    "Relay side: a tools/call `params` with the cwd's evidence added under `_meta`."
    out = dict(params)
    args = params.get("arguments")
    cwd = args.get("cwd") if isinstance(args, Mapping) else None
    if not isinstance(cwd, str) or not cwd:
        return out
    meta = params.get("_meta")
    out["_meta"] = {**(meta if isinstance(meta, Mapping) else {}),
                    EVIDENCE_META_KEY: project_evidence(cwd)}
    return out


def evidence_from_meta(meta: Mapping | object | None) -> dict | None:
    'Server side: the evidence a relay attached to a request, or None.'
    if meta is None:
        return None
    if not isinstance(meta, Mapping):
        dump = getattr(meta, "model_dump", None)
        meta = dump() if callable(dump) else None
    ev = meta.get(EVIDENCE_META_KEY) if isinstance(meta, Mapping) else None
    return dict(ev) if isinstance(ev, Mapping) else None
