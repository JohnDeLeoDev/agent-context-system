"Per-machine bearer tokens for the network daemon.\n\nUntil now every machine sent ONE shared token (`AGENT_CONTEXT_TOKEN`), so the daemon could not\ntell callers apart, could not revoke one machine, and trusted the machine headers a relay\nclaims for itself. This module keeps a table of tokens on the daemon's host: one entry per\nmachine, each with its own scopes, stored as a hash. A token looks like\n`acx_<id>_<secret>`; the id names the entry, and only sha256(secret) is kept.\n\nScopes: `read`; `entity-write` (the free class of entities, see write_guard); and\n`protected-write` (hooks, scripts and the other protected classes). The shared token keeps\nworking during the migration as `legacy-shared`. Its scopes come from\n`AGENT_CONTEXT_LEGACY_SCOPES` (default `read,entity-write`, never `protected-write`), so\nthe last step of the migration is to set that to `read`.\n\nThe table lives OUTSIDE the git store (the store is mirrored to GitHub and the NAS nodes),\nby default `~/.config/agent-context/tokens.json`, mode 0600, and is re-read whenever its\nmtime changes. A revoke or a scope change therefore takes effect on the next request with\nno restart. Stdlib plus the MCP auth types only; the one writer is `paths.write_atomic`."
from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.provider import AccessToken

from .flock import LOCK_EX, flock
from .paths import write_atomic

SCOPE_READ = "read"
SCOPE_ENTITY_WRITE = "entity-write"
SCOPE_PROTECTED_WRITE = "protected-write"
SCOPES = (SCOPE_READ, SCOPE_ENTITY_WRITE, SCOPE_PROTECTED_WRITE)

PREFIX = "acx_"
LEGACY_ID = "legacy-shared"
OAUTH_ID = "mobile-oauth"
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


LS_LOCAL_ID = "ls-local"
RESERVED_IDS = frozenset({LEGACY_ID, OAUTH_ID, LS_LOCAL_ID})


@dataclass(frozen=True)
class TokenInfo:
    id: str
    machine_uuid: str | None
    machine_id: str | None
    scopes: tuple[str, ...]
    allowed_ips: tuple[str, ...] = ()


def normalize_ip(text: object, names: bool = True) -> str | None:
    'The standard text form of an IP address given in a common spelling (brackets and a\n    numeric port, an IPv4-mapped IPv6 form, and `localhost` when `names` allows it), or None\n    when it is anything else, including trailing junk, a zone id or a non-numeric port.'
    if not isinstance(text, str):
        return None
    value = text.strip().lower()
    if not value:
        return None
    host = value
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host, rest = value[1:end], value[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
    elif value.count(":") == 1:
        host, _, port = value.partition(":")
        if not port.isdigit():
            return None
    if names and host == "localhost":
        return "127.0.0.1"
    if "%" in host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def _entry_ips(entry: dict) -> tuple[str, ...]:
    'The addresses an entry is bound to. An entry that has the field but names no valid\n    address (empty, null, a string, junk) is bound to an address nothing can have, never\n    unrestricted.'
    if "allowed_ips" not in entry:
        return ()
    raw = entry["allowed_ips"]
    ips = [normalize_ip(item, names=False) for item in raw] if isinstance(raw, list) else []
    good = tuple(dict.fromkeys(ip for ip in ips if ip))
    return good or ("-",)


def table_path() -> Path:
    override = os.environ.get("AGENT_CONTEXT_TOKEN_TABLE")
    if override:
        return Path(override)
    return Path.home() / ".config" / "agent-context" / "tokens.json"


def auth_required() -> bool:
    'True when the daemon must authenticate: a shared token is set, or a table exists.'
    return bool(os.environ.get("AGENT_CONTEXT_TOKEN")) or table_path().exists()


def _legacy_scopes() -> tuple[str, ...]:
    raw = os.environ.get("AGENT_CONTEXT_LEGACY_SCOPES")
    if raw is None:
        return (SCOPE_READ, SCOPE_ENTITY_WRITE)
    
    return tuple(s for s in (p.strip() for p in raw.split(",")) if s in (SCOPE_READ, SCOPE_ENTITY_WRITE))


def _legacy_info() -> TokenInfo | None:
    if not os.environ.get("AGENT_CONTEXT_TOKEN"):
        return None
    return TokenInfo(LEGACY_ID, None, None, _legacy_scopes())


def oauth_info(client_id: str) -> TokenInfo:
    'The identity of a token the OAuth connector issued: read only until step 2.'
    return TokenInfo(OAUTH_ID, None, None, (SCOPE_READ,))




_LOCK = threading.Lock()
_CACHE: dict[str, tuple[tuple[int, int], list[dict]]] = {}


def _entries() -> list[dict]:
    path = table_path()
    try:
        st = path.stat()
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _CACHE.get(str(path))
        if hit and hit[0] == key:
            return hit[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = [e for e in data.get("tokens", []) if isinstance(e, dict)]
        except (OSError, ValueError, AttributeError):
            entries = []
        _CACHE[str(path)] = (key, entries)
        return entries


def _live(entry: dict) -> bool:
    if entry.get("revoked"):
        return False
    expires = entry.get("expires")
    return not (expires is not None and float(expires) <= time.time())


def _info(entry: dict) -> TokenInfo:
    scopes = tuple(s for s in entry.get("scopes", []) if s in SCOPES)
    return TokenInfo(str(entry["id"]), entry.get("machine_uuid"), entry.get("machine_id"), scopes,
                     _entry_ips(entry))


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify(token: str) -> TokenInfo | None:
    'The identity a bearer token stands for, or None when it is unknown, wrong, revoked\n    or expired.'
    if not token:
        return None
    legacy = os.environ.get("AGENT_CONTEXT_TOKEN", "")
    if legacy and not token.startswith(PREFIX) and hmac.compare_digest(token.encode(), legacy.encode()):
        return _legacy_info()
    if not token.startswith(PREFIX):
        return None
    token_id, sep, secret = token[len(PREFIX):].partition("_")
    if not (token_id and sep and secret):
        return None
    for entry in _entries():
        if entry.get("id") != token_id:
            continue
        stored = str(entry.get("sha256", ""))
        if _live(entry) and hmac.compare_digest(_hash(secret), stored):
            return _info(entry)
        return None
    return None


def lookup(token_id: str) -> TokenInfo | None:
    "The CURRENT state of a token by id, read from the table now. None when it is\n    unknown, revoked or expired. Guards call this on every write, so a revoke ends an open\n    session's ability to write."
    if token_id == LEGACY_ID:
        return _legacy_info()
    if token_id == OAUTH_ID:
        return oauth_info("")
    for entry in _entries():
        if entry.get("id") == token_id:
            return _info(entry) if _live(entry) else None
    return None





@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flock(fd, LOCK_EX)
        yield
    finally:
        os.close(fd)   


def _load_raw(path: Path) -> dict:
    "The table's contents for a writer. A missing file is an empty table. A file that is\n    there but cannot be read or parsed is an error, so no writer replaces it and drops every\n    token in it."
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"tokens": []}
    except OSError as exc:
        raise ValueError(f"token table {path} is not valid ({exc.strerror or 'unreadable'}); "
                         "nothing was changed") from None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), list):
        raise ValueError(f"token table {path} is not valid JSON of the form "
                         '{"tokens": [...]}; fix or restore it (nothing was changed)')
    return data


def _save(path: Path, data: dict) -> None:
    write_atomic(path, json.dumps(data, indent=1, sort_keys=True) + "\n", 0o600)


def issue(table: str | Path, *, id: str, machine_uuid: str | None = None,
          machine_id: str | None = None, scopes: list[str] | tuple[str, ...],
          expires: float | None = None, allowed_ips: list[str] | None = None,
          allow_ls_local: bool = False) -> str:
    "Add an entry and return the plaintext token. This is the ONLY time the secret exists\n    outside the caller's hands; the table keeps its sha256."
    if not _ID.fullmatch(id):
        raise ValueError(f"token id {id!r} must be lowercase letters, digits and hyphens")
    if id in RESERVED_IDS and not (allow_ls_local and id == LS_LOCAL_ID):
        raise ValueError(f"token id {id!r} is reserved for the server's own callers; "
                         "pick another id")
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError(f"unknown scope {bad[0]!r}; one of {', '.join(SCOPES)}")
    canonical_ips: list[str] | None = None
    if allowed_ips is not None:
        if not isinstance(allowed_ips, (list, tuple)) or not allowed_ips:
            raise ValueError("allowed_ips must be a non-empty list of IP addresses; "
                             "leave it out for no restriction")
        found = [normalize_ip(item) for item in allowed_ips]
        if any(ip is None or (isinstance(item, str) and "/" in item)
               for ip, item in zip(found, allowed_ips)):
            raise ValueError("every allowed_ips entry must be a single IP address")
        canonical_ips = list(dict.fromkeys(ip for ip in found if ip))
    path = Path(table)
    secret = secrets.token_urlsafe(32)
    with _locked(path):
        data = _load_raw(path)
        if any(e.get("id") == id for e in data["tokens"]):
            raise ValueError(f"token id {id!r} already exists; revoke it and pick a new id")
        entry = {
            "id": id, "machine_uuid": machine_uuid, "machine_id": machine_id,
            "scopes": list(scopes), "sha256": _hash(secret), "created": time.time(),
            "expires": expires, "revoked": False}
        if canonical_ips is not None:
            entry["allowed_ips"] = canonical_ips
        data["tokens"].append(entry)
        _save(path, data)
    return f"{PREFIX}{id}_{secret}"


def set_expiry(table: str | Path, id: str, expires: float) -> float:
    "Shorten an entry's life to `expires` and return the expiry it now has. An entry that\n    already ends sooner keeps its earlier time; this never extends a token."
    path = Path(table)
    with _locked(path):
        data = _load_raw(path)
        for entry in data["tokens"]:
            if entry.get("id") == id:
                current = entry.get("expires")
                entry["expires"] = expires if current is None else min(float(current), expires)
                _save(path, data)
                return float(entry["expires"])
    raise ValueError(f"no token with id {id!r}")


def list_entries(table: str | Path) -> list[dict]:
    "The table's entries as stored (they include each hash, so callers must not print them\n    whole). A missing or unreadable table has none."
    return [dict(e) for e in _load_raw(Path(table))["tokens"] if isinstance(e, dict)]


def revoke(table: str | Path, id: str) -> None:
    path = Path(table)
    with _locked(path):
        data = _load_raw(path)
        for entry in data["tokens"]:
            if entry.get("id") == id:
                entry["revoked"] = True
                entry["revoked_at"] = time.time()
                _save(path, data)
                return
    raise ValueError(f"no token with id {id!r}")





class TableTokenVerifier:
    'Replaces the single-token verifier: resolves a bearer through `verify`.'

    async def verify_token(self, token: str) -> AccessToken | None:
        info = verify(token)
        if info is None:
            return None
        return AccessToken(token=token, client_id=info.id, scopes=list(info.scopes),
                           expires_at=None)
