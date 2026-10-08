"Server-side guards on the store's write path, keyed on the caller's token.\n\nEvery guard on a store write used to be a hook inside the calling harness. A holder of the\nnetwork token could skip all of them by calling `upsert_hook` over HTTP, and a hook body\nwritten that way is materialized to every machine and run. These checks live where every\nwrite ends: `Store.upsert`, `Store.delete`, and `paths.write_atomic` as a backstop for files\nthat are not entities. No tool can be added later that skips them.\n\nWHO IS CALLING. The ASGI wrapper (server._counting_app) resolves the bearer to a token and\nbinds a `Caller` for the session. No bound caller means a system write (the sync loop, the\njanitor, the autocommit, stdio and in-process use) and is never checked.\n\nCLASSES. `classify` sorts an entity write, `classify_path` a file path:\n  free       memory, docs, skills, commands, project-scope instructions, records\n  protected  hooks, scripts (default deny: `FREE_SCRIPTS` is empty), agent definitions,\n             global and workspace instructions, and the non-entity files whose content runs\n             on every machine or judges the guards themselves (manifests, fingerprints,\n             test locks, AGENTS.md, server/ ...).\nA free write needs the `entity-write` scope, a protected write `protected-write`. Scopes are\nread from the token table at write time, so a revoke or a scope change reaches a session that\nis already open.\n\nMODES (`AGENT_CONTEXT_GUARD`): `observe` (the default, and what a fresh deploy runs) decides,\naudits and blocks nothing; `enforce` blocks; `off` checks nothing and audits with mode `off`.\nAn unrecognized value means `enforce`, never `off`. Only the daemon's host can change it.\n\nAlso enforced for any caller, in every mode: a locked test script cannot change (a lock record\non main is the judge, and the approved unlock removes the record). For any caller: a hook or\nscript body must parse\n(Python as 3.8, the oldest interpreter in the fleet; shell with `bash -n`; parse only, never\nexecuted) and stay under 512 KB; a token may make 60 free writes an hour; five protected-\nclass denials in ten minutes suspend the token and alert user."
from __future__ import annotations

import ast
import contextvars
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from . import paths, token_table, write_audit

log = logging.getLogger("agent_context.write_guard")

FREE_SCRIPTS: frozenset[str] = frozenset()
MAX_BODY_BYTES = 512 * 1024
MODES = ("observe", "enforce", "off")

PROTECTED_FILES = frozenset({
    "AGENTS.md", "hook-dispatch.json", "setup.sh",
    "global/hooks-manifest.json", "global/hook-fingerprints.json",
    "global/mcp-servers.json", "global/settings-seed.json",
})
PROTECTED_PREFIXES = ("server/", "global/test-locks/", "global/node-tools/", "global/deps/",
                      "templates/", ".git/", ".agents/")
_PROTECTED_FILES_LOWER = frozenset(f.lower() for f in PROTECTED_FILES)
_PROTECTED_PREFIXES_LOWER = tuple(p.lower() for p in PROTECTED_PREFIXES)
_CODE_DIRS = ("hooks", "scripts", "agents")

_RATE_WINDOW = 3600.0
_DENIAL_WINDOW = 600.0


class WriteDenied(ValueError):
    'A write the guard refused. A ValueError, so every tool that already turns a bad\n    argument into an `{"error": ...}` reply does the same for this.'


@dataclass(frozen=True)
class Caller:
    token_id: str
    machine_id: str | None = None
    ip: str | None = None
    session_key: str | None = None


_CALLER: contextvars.ContextVar[Caller | None] = contextvars.ContextVar(
    "agent_context_write_caller", default=None)


def bind(caller: Caller | None):
    return _CALLER.set(caller)


def reset(token) -> None:
    _CALLER.reset(token)


def current() -> Caller | None:
    return _CALLER.get()


def mode() -> str:
    raw = (os.environ.get("AGENT_CONTEXT_GUARD") or "").strip().lower()
    if not raw:
        return "observe"
    return raw if raw in MODES else "enforce"


def startup_notice() -> str | None:
    m = mode()
    if m == "off":
        return ("write guard is OFF (AGENT_CONTEXT_GUARD=off): no write from any token is "
                "checked. Use it only to recover from a guard bug.")
    if m == "observe":
        return ("write guard is in observe mode: decisions are audited as would-deny and "
                "nothing is blocked. Set AGENT_CONTEXT_GUARD=enforce to block.")
    return None





def classify(kind: str, scope: str | None, key: str) -> str:
    if kind in ("hook", "agent_definition"):
        return "protected"
    if kind == "script":
        return "free" if key in FREE_SCRIPTS else "protected"
    if kind == "instruction":
        s = scope or "global"
        return "protected" if s == "global" or s.startswith("ws:") else "free"
    return "free"


def classify_path(rel: str) -> bool:
    'True when a store-relative path is in the protected class.'
    
    rel = rel.replace(os.sep, "/").lstrip("/").lower()
    if rel == ".git" or rel in _PROTECTED_FILES_LOWER or rel.startswith(_PROTECTED_PREFIXES_LOWER):
        return True
    parts = rel.split("/")
    if parts[0] == "global" and len(parts) >= 3:
        scope_kind, sub = "global", parts[1]
    elif parts[0] in ("workspaces", "projects") and len(parts) >= 4:
        scope_kind, sub = parts[0], parts[2]
    else:
        return False
    if sub in _CODE_DIRS:
        return True
    return sub == "instructions" and scope_kind in ("global", "workspaces")


_ROOTS: set[str] = set()



DRY_RUN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "agent_context_write_dry_run", default=False)


def register_root(root) -> None:
    'A store root whose files the path backstop watches (called by ContextStore).'
    _ROOTS.add(os.path.realpath(str(root)))


def unregister_root(root) -> None:
    _ROOTS.discard(os.path.realpath(str(root)))


def registered_root_count() -> int:
    return len(_ROOTS)


def _relative_to_a_root(path: str) -> str | None:
    real = os.path.realpath(path)
    for root in _ROOTS:
        if real == root or real.startswith(root + os.sep):
            return os.path.relpath(real, root)
    return None




_STATE_LOCK = threading.Lock()
_WRITES: dict[str, deque[float]] = defaultdict(deque)
_DENIALS: dict[str, deque[float]] = defaultdict(deque)
_AUDIT: write_audit.WriteAudit | None = None


def _now() -> float:
    return time.time()


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def reset_state() -> None:
    'Forget the in-memory windows and the audit writer. Suspensions live in a file and stay.'
    global _AUDIT
    with _STATE_LOCK:
        _WRITES.clear()
        _DENIALS.clear()
        _AUDIT = None


def _suspension_file() -> Path:
    return Path(paths.state_dir()) / "suspended-tokens.json"


def _suspended() -> dict:
    try:
        data = json.loads(_suspension_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_suspended(token_id: str) -> bool:
    return token_id in _suspended()


def suspend(token_id: str, reason: str) -> None:
    data = _suspended()
    data[token_id] = {"at": _now(), "reason": reason}
    paths.write_atomic(_suspension_file(), json.dumps(data, indent=1, sort_keys=True) + "\n")


def unsuspend(token_id: str) -> None:
    data = _suspended()
    if data.pop(token_id, None) is not None:
        paths.write_atomic(_suspension_file(), json.dumps(data, indent=1, sort_keys=True) + "\n")


def alert(text: str) -> None:
    'alert.'
    log.warning("write-guard alert: %s", text)
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    cmd = os.environ.get("AGENT_CONTEXT_ALERT_CMD", "notify")
    exe = shutil.which(cmd)
    if not exe:
        return
    try:
        subprocess.Popen([exe, "-t", "agent-context write guard", text], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log.warning("write-guard: could not run the alert command: %s", exc)


def _register_denial(token_id: str) -> None:
    'Count a protected-class scope denial; suspend the token when there are too many.'
    limit = _int_env("AGENT_CONTEXT_DENIALS_BEFORE_SUSPEND", 5)
    now = _now()
    with _STATE_LOCK:
        window = _DENIALS[token_id]
        window.append(now)
        while window and now - window[0] >= _DENIAL_WINDOW:
            window.popleft()
        tripped = len(window) >= limit
        if tripped:
            window.clear()
    if tripped:
        suspend(token_id, f"{limit} protected-class write denials in {int(_DENIAL_WINDOW // 60)} minutes")
        alert(f"token {token_id} suspended: {limit} denied protected writes in "
              f"{int(_DENIAL_WINDOW // 60)} minutes")


def _rate_verdict(bucket: str) -> str | None:
    limit = _int_env("AGENT_CONTEXT_FREE_WRITES_PER_HOUR", 60)
    now = _now()
    with _STATE_LOCK:
        window = _WRITES[bucket]
        while window and now - window[0] >= _RATE_WINDOW:
            window.popleft()
        if len(window) >= limit:
            return f"rate limit: token {bucket!r} already made {limit} free writes this hour"
        if not DRY_RUN.get():
            window.append(now)
    return None





def _bucket(caller: Caller) -> str:
    "The key for rate windows, denial counts and suspension. The shared token and the OAuth\n    token stand for many machines, so each machine gets its own bucket; a table token is one\n    machine's already."
    if caller.token_id in (token_table.LEGACY_ID, token_table.OAUTH_ID):
        return f"{caller.token_id}:{caller.machine_id or '-'}"
    return caller.token_id


def _scope_verdict(caller: Caller, klass: str) -> tuple[bool, str, bool]:
    "(allowed, reason, counts toward suspension) for the caller's token and a write class."
    info = token_table.lookup(caller.token_id)
    if info is None:
        return False, (f"write denied: token {caller.token_id!r} is revoked, expired or "
                       "unknown (no longer valid)"), False
    if is_suspended(_bucket(caller)):
        return False, (f"write denied: token {caller.token_id!r} is suspended after repeated "
                       "protected-write attempts; user can lift it"), False
    need = token_table.SCOPE_PROTECTED_WRITE if klass == "protected" else token_table.SCOPE_ENTITY_WRITE
    if need in info.scopes:
        return True, "", False
    return False, (f"write denied: token {caller.token_id!r} lacks the {need!r} scope this "
                   f"{klass}-class write needs (it has: {', '.join(info.scopes) or 'none'})"), klass == "protected"


def _sha(text: str | None) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest() if text else ""


def _code_language(language: str | None, body: str) -> str | None:
    'The language to parse a hook or script body as: its shebang wins over the field,\n    because an executable body runs under its shebang.'
    first = body.split("\n", 1)[0]
    if first.startswith("#!"):
        if "python" in first:
            return "py"
        if any(word in first for word in ("bash", "/sh", " sh")):
            return "sh"
    lang = language or "sh"
    if lang in ("py", "python", "python3"):
        return "py"
    if lang in ("sh", "bash"):
        return "sh"
    return None


def _body_problem(kind: str, language: str | None, body: str | None) -> str | None:
    if kind not in ("hook", "script") or body is None:
        return None
    try:
        size = len(body.encode("utf-8"))
    except UnicodeEncodeError:
        return "write denied: the body is not valid UTF-8 text"
    if size > MAX_BODY_BYTES:
        return f"write denied: the body is too large (size limit {MAX_BODY_BYTES} bytes)"
    lang = _code_language(language, body)
    if lang == "py":
        try:
            ast.parse(body, feature_version=(3, 8))
        except (SyntaxError, ValueError) as exc:
            where = f" at line {exc.lineno}" if isinstance(exc, SyntaxError) and exc.lineno else ""
            return f"write denied: Python syntax error (checked as 3.8): {getattr(exc, 'msg', exc)}{where}"
    elif lang == "sh":
        try:
            proc = subprocess.run(["bash", "-n"], input=body, text=True, capture_output=True,
                                  timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None                  
        if proc.returncode != 0:
            return f"write denied: shell syntax error: {(proc.stderr or '').strip()[:200]}"
    return None


def _lock_record(root: str, rel: str) -> dict | None:
    locks = Path(root) / "global" / "test-locks"
    try:
        records = sorted(locks.glob("*.json"))
    except OSError:
        return None
    for record in records:
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("path") == rel:
            return data
    return None


def _lock_problem(root: str, kind: str, key: str, scope: str | None, language: str | None,
                  op: str, body: str | None, before_language: str | None = None) -> str | None:
    if kind not in ("script", "hook"):
        return None
    from .store import EXT
    s = scope or "global"
    base = ("projects/" + s[len("project:"):] if s.startswith("project:")
            else "workspaces/" + s[len("ws:"):] if s.startswith("ws:") else "global")
    folder = "scripts" if kind == "script" else "hooks"

    def rel_for(lang: str | None) -> str:
        return f"{base}/{folder}/{key}.{EXT.get(lang or 'sh', lang or 'sh')}"

    rel = rel_for(language)
    
    
    if before_language and rel_for(before_language) != rel:
        old = rel_for(before_language)
        if _lock_record(root, old) is not None:
            return f"write denied: {old} is a locked test; changing its language would remove it"
    data = _lock_record(root, rel)
    if data is None:
        return None
    if op == "delete":
        return f"write denied: {rel} is a locked test; only user's approved unlock lifts it" \
            + _unlock_ask(root, rel)
    if body is None:
        return None
    locked = data.get("sha256")
    if locked in (_sha(body), _sha(body.rstrip("\n") + "\n")):
        return None
    return f"write denied: {rel} is a locked test and this body differs from the lock" \
        + _unlock_ask(root, rel)


def _unlock_ask(root: str, rel: str) -> str:
    'The approval question that lifts a lock (the block-locked-test-edit wording).'
    path = os.path.join(root, rel)
    return ("\n\nThe tests were agreed before implementation and seen failing. Change the code "
            "so it passes them. If the test itself is wrong, tell user which assertion is wrong "
            "and why, then ask with one AskUserQuestion, header \"Approval\", options "
            "\"Approve\" and \"Deny\", question:\n"
            f"    Unlock the locked test {path}? [approval:test-unlock:{path}:0]")


def _key_problem(key: str) -> str | None:
    'A key that names a path outside its own directory, or an absolute one.'
    normal = key.replace("\\", "/")
    if normal.startswith("/") or ".." in normal.split("/"):
        return f"write denied: path traversal or an absolute path in the key {key[:80]!r}"
    return None


def _audit_entry(caller: Caller, m: str, decision: str, reason: str, **fields) -> None:
    global _AUDIT
    with _STATE_LOCK:
        if _AUDIT is None:
            _AUDIT = write_audit.WriteAudit()
        audit = _AUDIT
    fields = {k: (v[:512] + "…" if isinstance(v, str) and len(v) > 512 else v)
              for k, v in fields.items()}
    reason = reason[:512] + "…" if len(reason) > 512 else reason
    audit.record({"token_id": caller.token_id, "dry_run": DRY_RUN.get(), "machine_id": caller.machine_id,
                  "session_key": caller.session_key, "ip": caller.ip, "mode": m,
                  "decision": decision, "reason": reason, **fields})


def record_refusal(token_id: str | None, machine_id: str | None, ip: str | None,
                   session_key: str | None, reason: str) -> None:
    'Audit a request the ASGI wrapper refused before it reached a tool. Never raises.'
    try:
        _audit_entry(Caller(token_id or "-", machine_id, ip, session_key), mode(), "deny", reason,
                     op="request", kind="request", key="", scope="", **{"class": ""},
                     before_sha="", after_sha="", body_len=0)
    except Exception as exc:
        log.error("write-guard: could not audit a refused request: %s", exc)


def record_session(op: str, token_id: str, machine_id: str | None, ip: str | None,
                   user_agent: str, header_names: list[str], session_id_len: int,
                   relay_etag: str | None = None) -> None:
    'Audit a session initialize (`session-init`) or a later request of a still-unbound\n    session (`unbound-request`). Header NAMES only, never values. Logging, not a decision:\n    never raises.'
    bound = machine_id is not None
    try:
        _audit_entry(Caller(token_id, machine_id, ip, None), mode(), "observed",
                     "machine identity bound" if bound else "no machine identity headers",
                     op=op, kind="session", key="", scope="", **{"class": ""},
                     before_sha="", after_sha="", body_len=0, user_agent=user_agent[:300],
                     headers=list(header_names), bound=bound, session_id_len=session_id_len,
                     relay_etag=relay_etag)
    except Exception as exc:
        log.error("write-guard: could not audit a session line: %s", exc)


def check_entity(root, op: str, kind: str, key: str, scope: str | None, *,
                 body: str | None = None, before: dict | None = None,
                 language: str | None = None) -> None:
    'Raise WriteDenied when the bound caller may not make this entity write.'
    caller = current()
    if caller is None:
        return
    m = mode()
    klass = classify(kind, scope, key)
    before_body = None
    if before:
        before_body = before.get("script_body" if kind in ("hook", "script") else "body")
    fields = {"op": op, "kind": kind, "key": key, "scope": scope or "global", "class": klass,
              "before_sha": _sha(before_body),
              "after_sha": "" if op == "delete" else _sha(body if body is not None else before_body),
              "body_len": len(body or "")}
    if m == "off":
        _safe_audit(caller, m, "allow", "guard off", fields)
        return
    problem = _key_problem(key)
    if problem:
        _finish_denial(caller, m, problem, False, fields)
        return
    
    
    lock = _lock_problem(str(root), kind, key, scope, language, op, body,
                         (before or {}).get("language"))
    if lock:
        _safe_audit(caller, m, "deny", lock, fields)
        raise WriteDenied(lock)
    allowed, reason, counts = _scope_verdict(caller, klass)
    if allowed:
        reason = ((None if op == "delete" else _body_problem(kind, language, body))
                  or (_rate_verdict(_bucket(caller)) if klass == "free" else None) or "")
        allowed = not reason
    if allowed:
        if not _safe_audit(caller, m, "allow", "", fields) and klass == "protected" and m == "enforce":
            raise WriteDenied("write denied: the audit log could not be written, so a "
                              "protected write is refused")
        return
    _finish_denial(caller, m, reason, counts, fields)


def check_path(path: str) -> None:
    'Backstop in `paths.write_atomic`: a protected file needs `protected-write`.'
    caller = current()
    if caller is None:
        return
    m = mode()
    if m == "off":
        return
    rel = _relative_to_a_root(path)
    if rel is None or not classify_path(rel):
        return
    allowed, reason, counts = _scope_verdict(caller, "protected")
    if allowed:
        return
    fields = {"op": "write", "kind": "path", "key": rel, "scope": "", "class": "protected",
              "before_sha": "", "after_sha": "", "body_len": 0}
    _finish_denial(caller, m, reason, counts, fields)


def _finish_denial(caller: Caller, m: str, reason: str, counts: bool, fields: dict) -> None:
    enforcing = m == "enforce"
    _safe_audit(caller, m, "deny" if enforcing else "would-deny", reason, fields)
    if not enforcing:
        return
    if counts:
        _register_denial(_bucket(caller))
    raise WriteDenied(reason)


def _safe_audit(caller: Caller, m: str, decision: str, reason: str, fields: dict) -> bool:
    try:
        _audit_entry(caller, m, decision, reason, **fields)
        return True
    except Exception as exc:
        log.error("write-guard: audit write failed: %s", exc)
        return False


paths.set_write_check(check_path)
