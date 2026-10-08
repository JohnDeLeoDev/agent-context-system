'One deploy gate at a time per code fingerprint, shared by the daemon and every bridge.\n\nThe daemon\'s self-redeploy (a thread in the daemon) and each session\'s bridge process\n(`ensure_daemon` -> `_gated_bounce`) used to run the whole suite for the same code, and a bridge\nwhose gate finished first restarted the service through systemd while the daemon\'s own gate was\nstill running. `single_flight` is the one door both go through:\n\n- `gate.lock`: an flock around every gate run. The kernel drops it when its holder dies, so a\n  killed owner needs no stale-lock recovery: the next party takes the lock and runs.\n- `gate-owner.json`: who is running (pid, role, fingerprint, start), present only while it runs.\n- `gate-result.json`: the verdict for the newest fingerprint, reused by every other party instead of\n  a second suite: a pass for that exact fingerprint, or a real failure inside the hold.\n\nAn interrupted run (detail starting `interrupted:`) is never recorded: it says nothing about the code.\nThe records live in the state dir with mode 0600 and are ignored when group or world writable, owned\nby another user, a symlink, or malformed: an unreadable record means "run the gate", never "skip it".'
from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .flock import LOCK_EX, LOCK_NB, flock
from .paths import write_atomic

log = logging.getLogger("agent-context")

ROLE_DAEMON = "daemon"
ROLE_BRIDGE = "bridge"

LOCK_NAME = "gate.lock"
OWNER_NAME = "gate-owner.json"
RESULT_NAME = "gate-result.json"

INTERRUPTED_PREFIX = "interrupted:"
_CLAIM_WAIT_SECS = 5.0
CLAIM_TTL_SECS = 600.0


DAEMON_PATIENCE_SECS = 21600.0


@dataclass(frozen=True)
class Outcome:
    'What a caller learns from `single_flight`.\n\n    ok: True pass, False a real failure, None no verdict (interrupted, stale, timed out).\n    source: ran | recorded | waited | stale | timeout | unshared.'
    ok: bool | None
    detail: str
    source: str
    waited_secs: float
    owner_role: str
    owner_pid: int
    duration: float


def _short(fingerprint: str) -> str:
    return fingerprint[:8]


def _own(st: os.stat_result) -> bool:
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022


def _read_json(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not _own(os.fstat(fd)):
            return None
        with os.fdopen(fd, "r", closefd=False) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    return data if isinstance(data, dict) else None


def _write_json(path: Path, obj: dict) -> None:
    
    
    
    
    write_atomic(path, json.dumps(obj), mode=0o600)


def _is_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_result(state_dir: Path, fingerprint: str) -> dict | None:
    'The recorded verdict for exactly this fingerprint, or None (missing, malformed, another\n    fingerprint, or a file this user should not trust).'
    rec = _read_json(Path(state_dir) / RESULT_NAME)
    if rec is None or rec.get("fingerprint") != fingerprint:
        return None
    if not isinstance(rec.get("ok"), bool) or not _is_num(rec.get("finished_at")):
        return None
    if not isinstance(rec.get("owner_role"), str) or not isinstance(rec.get("owner_pid"), int):
        return None
    if not isinstance(rec.get("detail", ""), str) or not _is_num(rec.get("duration", 0.0)):
        return None
    return rec


def _usable(rec: dict | None, retry_secs: float) -> bool:
    if rec is None:
        return False
    if rec["ok"]:
        return True
    return time.time() - float(rec["finished_at"]) < retry_secs


def _try_flock(fd: int) -> bool:
    try:
        flock(fd, LOCK_EX | LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _fresh(current_fingerprint: Callable[[], str]) -> str | None:
    try:
        return current_fingerprint()
    except Exception:
        return None


def _adopt(rec: dict, fingerprint: str, role: str, source: str, waited: float,
           current_fingerprint: Callable[[], str]) -> Outcome:
    owner = f"{rec['owner_role']}"
    log.info("agent-context: gate_adopt role=%s pid=%d fp=%s from=%s pid=%d ok=%s waited=%.1fs",
             role, os.getpid(), _short(fingerprint), owner, rec["owner_pid"], rec["ok"], waited)
    ok: bool | None = rec["ok"]
    detail = str(rec.get("detail", ""))
    src = source
    if ok and _fresh(current_fingerprint) != fingerprint:
        ok, src, detail = None, "stale", "the code changed after the gate ran"
    return Outcome(ok, detail, src, waited, owner, int(rec["owner_pid"]),
                   float(rec.get("duration", 0.0)))


def _owner_label(state_dir: Path) -> str:
    rec = _read_json(state_dir / OWNER_NAME)
    if rec and isinstance(rec.get("role"), str) and isinstance(rec.get("pid"), int):
        try:
            os.kill(rec["pid"], 0)      
        except ProcessLookupError:
            return "unknown:0"
        except OSError:
            pass
        return f"{rec['role']}:{rec['pid']}"
    return "unknown:0"


def _run(state_dir: Path | None, fingerprint: str, role: str, run: Callable[[], tuple[bool, str]],
         waited: float, current_fingerprint: Callable[[], str]) -> Outcome:
    pid = os.getpid()
    log.info("agent-context: gate_start role=%s pid=%d fp=%s waited=%.1fs",
             role, pid, _short(fingerprint), waited)
    owner_file = state_dir / OWNER_NAME if state_dir is not None else None
    if owner_file is not None:
        with contextlib.suppress(OSError):
            _write_json(owner_file, {"pid": pid, "role": role, "fingerprint": fingerprint,
                                     "started_at": time.time()})
    started = time.monotonic()
    try:
        ok, detail = run()
    finally:
        if owner_file is not None:
            with contextlib.suppress(OSError):
                owner_file.unlink()
    duration = time.monotonic() - started
    interrupted = not ok and detail.startswith(INTERRUPTED_PREFIX)
    log.info("agent-context: gate_end role=%s pid=%d fp=%s ok=%s duration=%.1fs waited=%.1fs%s",
             role, pid, _short(fingerprint), None if interrupted else ok, duration, waited,
             " interrupted" if interrupted else "")
    if interrupted:
        return Outcome(None, detail, "ran", waited, role, pid, duration)
    changed = _fresh(current_fingerprint) != fingerprint
    if changed and not ok:
        
        
        return Outcome(None, "the code changed while the gate ran", "stale", waited, role, pid,
                       duration)
    if state_dir is not None:
        with contextlib.suppress(OSError):
            _write_json(state_dir / RESULT_NAME, {
                "fingerprint": fingerprint, "ok": bool(ok), "detail": detail,
                "finished_at": time.time(), "owner_pid": pid, "owner_role": role,
                "duration": duration})
    if ok and changed:
        return Outcome(None, "the code changed after the gate ran", "stale", waited, role, pid,
                       duration)
    return Outcome(bool(ok), detail, "ran" if state_dir is not None else "unshared", waited,
                   role, pid, duration)


def single_flight(state_dir: Path, fingerprint: str, role: str,
                  run: Callable[[], tuple[bool, str]], *,
                  current_fingerprint: Callable[[], str], timeout: float, retry_secs: float,
                  wait_grace: float = 30.0, poll: float = 0.25) -> Outcome:
    "Run `run()` unless another party already holds, or has finished, this fingerprint's gate.\n\n    `current_fingerprint` is read again before a pass is handed back, so code that changed after the\n    gate is never adopted. A caller that cannot use the state dir runs the gate unshared: the old\n    behavior, never a skipped suite."
    state = Path(state_dir)
    began = time.monotonic()
    rec = read_result(state, fingerprint)
    if _usable(rec, retry_secs):
        assert rec is not None
        return _adopt(rec, fingerprint, role, "recorded", 0.0, current_fingerprint)
    try:
        state.mkdir(parents=True, exist_ok=True)
        fd = os.open(state / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return _run(None, fingerprint, role, run, 0.0, current_fingerprint)
    try:
        deadline = began + timeout + wait_grace
        announced = False
        while not _try_flock(fd):
            rec = read_result(state, fingerprint)
            if _usable(rec, retry_secs):
                assert rec is not None
                return _adopt(rec, fingerprint, role, "waited", time.monotonic() - began,
                              current_fingerprint)
            if not announced:
                announced = True
                log.info("agent-context: gate_wait role=%s pid=%d fp=%s behind=%s",
                         role, os.getpid(), _short(fingerprint), _owner_label(state))
            if time.monotonic() >= deadline:
                return Outcome(None, "gave up waiting for another gate to finish", "timeout",
                               time.monotonic() - began, "", 0, 0.0)
            time.sleep(poll)
        waited = time.monotonic() - began
        rec = read_result(state, fingerprint)
        if _usable(rec, retry_secs):
            assert rec is not None
            return _adopt(rec, fingerprint, role, "waited" if announced else "recorded", waited,
                          current_fingerprint)
        return _run(state, fingerprint, role, run, waited, current_fingerprint)
    finally:
        os.close(fd)                        


def claim_bounce(state_dir: Path, fingerprint: str) -> bool:
    'True for exactly one caller per recorded pass: the one that may restart the daemon.'
    state = Path(state_dir)
    try:
        fd = os.open(state / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        deadline = time.monotonic() + _CLAIM_WAIT_SECS
        while not _try_flock(fd):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        rec = read_result(state, fingerprint)
        if rec is None or not rec["ok"]:
            return False
        claimed = rec.get("bounced_at")
        
        
        if isinstance(claimed, (int, float)) and time.time() - claimed < CLAIM_TTL_SECS:
            return False
        rec["bounced_at"] = time.time()
        rec["bounced_by"] = os.getpid()
        try:
            _write_json(state / RESULT_NAME, rec)
        except OSError:
            return False
        return True
    finally:
        os.close(fd)


def bridge_should_defer(*, daemon_self_deploys: bool, code_age_secs: float,
                        grace_secs: float = 300.0) -> bool:
    'A daemon that redeploys itself gets one sync cycle to adopt fresh code before a bridge gates.'
    if code_age_secs < 0:               
        return False
    return daemon_self_deploys and code_age_secs < grace_secs


def bridge_may_restart(*, daemon_self_deploys: bool, code_age_secs: float = 0.0,
                       patience_secs: float = DAEMON_PATIENCE_SECS) -> bool:
    'A bridge restarts the daemon only where no self-deploy loop will adopt the recorded pass,\n    or where that loop has left a passed build unadopted for `patience_secs`.'
    return not daemon_self_deploys or code_age_secs >= patience_secs
