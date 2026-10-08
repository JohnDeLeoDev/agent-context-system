
'Single shared-daemon support for agent-context — portable, zero per-machine setup.\n\nEvery client still launches agent-context over stdio (the default install config). But\ninstead of each launch operating the file tree + git independently (concurrent\nwriters racing the working tree and `.git`), the stdio process:\n\n  1. ensures ONE shared HTTP daemon is running on this machine — starting it\n     detached on first use, race-safe via a file lock; and\n  2. transparently bridges its own stdio MCP stream to that daemon.\n\nResult: exactly one agent-context process per machine no matter how many\nsessions/terminals are open — so all writes and git operations serialize through\none owner of the working tree. It requires NO launchd, config, or setup; the\nbehavior travels with the code. If anything here fails, ``server.main`` falls back\nto direct in-process stdio serving, so this layer can never break agent-context.'

import atexit
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import anyio
import httpx
from anyio import to_thread
from mcp.client.streamable_http import streamablehttp_client

from . import fleet, gate_lock, gate_record, paths, peer_wake, relay_stdio, relay_swap
from .flock import LOCK_EX, LOCK_NB, flock
from .relay_stdio import stdio_server

if TYPE_CHECKING:
    from mcp.shared.message import SessionMessage

log = logging.getLogger("agent-context")




_log_dir = paths.log_dir
_state_dir = paths.state_dir
_health_dir = paths.health_dir

HOST = os.environ.get("AGENT_CONTEXT_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_CONTEXT_PORT", "8765"))
URL = f"http://{HOST}:{PORT}/mcp"


def _host() -> str:
    return os.environ.get("AGENT_CONTEXT_HOST") or "127.0.0.1"


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_remote() -> bool:
    'True when AGENT_CONTEXT_HOST names a daemon on another machine (the ls model).\n\n    Loopback is judged as the OS does: `localhost` in any case with an optional trailing\n    dot, or any 127.0.0.0/8 or ::1 address.'
    host = _host()
    if host.lower().rstrip(".") == "localhost":
        return False
    ip = _ip(host)
    return not (ip is not None and ip.is_loopback)


def _remote_host_problem(host: str) -> str | None:
    'Why `host` cannot be a remote destination, or None. Catches a scheme, port or path\n    pasted into the variable and the 0.0.0.0 bind address, which would otherwise build a\n    malformed URL that only fails at connect time.'
    ip = _ip(host)
    if ip is not None:
        return "is a bind address, not a destination" if ip.is_unspecified else None
    if "/" in host or ":" in host:
        return "must be a bare hostname: no scheme, port or path (use AGENT_CONTEXT_PORT for a port)"
    return None


def mcp_url() -> str:
    "The daemon's MCP endpoint, read from the environment on every call.\n\n    A remote host is fronted by TLS on 443 (nginx), so it gets https and no port unless\n    AGENT_CONTEXT_PORT is set explicitly; loopback keeps plain http on the daemon port."
    host = _host()
    ip = _ip(host)
    if ip is not None and ip.version == 6:
        host = f"[{host}]"
    port = os.environ.get("AGENT_CONTEXT_PORT")
    if is_remote() and not port:
        return f"https://{host}/mcp"
    return f"http://{host}:{port or 8765}/mcp"


def _port_open() -> bool:
    with contextlib.suppress(OSError), socket.create_connection((HOST, PORT), timeout=0.4):
        return True
    return False


def daemon_answers(url: str, timeout: float) -> bool:
    'True when something answers HTTP at `url` inside `timeout`, whatever the status: a live\n    daemon refuses a bare GET on /mcp (401 without a token, 405 or 406 otherwise), and any of\n    those proves it is up. Only the headers are awaited, so an SSE stream is never read. Used\n    to tell a slow daemon from a down one after a start-time fetch timeout.'
    try:
        with httpx.stream("GET", url, timeout=timeout):
            return True
    except Exception:
        return False


def _wait_port(up: bool, secs: float) -> bool:
    'Poll until the port is (up=True) open / (up=False) closed. True on success.'
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if _port_open() == up:
            return True
        time.sleep(0.2)
    return _port_open() == up








_LAUNCHD_LABEL = "org.agent-context.daemon"
_LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
_SYSTEMD_UNIT = "agent-context-daemon.service"
_SYSTEMD_UNIT_FILE = Path.home() / ".config" / "systemd" / "user" / _SYSTEMD_UNIT
_SUPERVISED_WAIT_SECS = 30.0


def _supervisor() -> tuple[str, str] | None:
    '("launchd", label) / ("systemd", unit) when a supervisor unit is installed for\n    this user, else None. AGENT_CONTEXT_SUPERVISOR=none forces the unsupervised path.'
    if os.environ.get("AGENT_CONTEXT_SUPERVISOR", "").lower() == "none":
        return None
    if sys.platform == "darwin" and _LAUNCHD_PLIST.is_file():
        return ("launchd", _LAUNCHD_LABEL)
    if sys.platform != "darwin" and _SYSTEMD_UNIT_FILE.is_file():
        return ("systemd", _SYSTEMD_UNIT)
    return None


def _supervisor_restart_cmd(sup: tuple[str, str]) -> list[str]:
    kind, name = sup
    if kind == "launchd":
        return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{name}"]
    return ["systemctl", "--user", "restart", name]


def _parse_ss_pid(line: str) -> int | None:
    'pid out of an `ss -ltnp` row: users:(("python3",pid=123,fd=6)).'
    m = re.search(r"pid=(\d+)", line)
    return int(m.group(1)) if m else None


def _port_owner_pid() -> int | None:
    'The pid actually listening on the daemon port, from the OS — not from\n    daemon.info, which can name a process that lost the bind race and is gone.'
    with contextlib.suppress(Exception):
        if sys.platform == "darwin":
            out = subprocess.run(["lsof", "-nP", f"-tiTCP:{PORT}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=5).stdout.split()
            return int(out[0]) if out else None
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{PORT}"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            pid = _parse_ss_pid(line)
            if pid:
                return pid
    return None








def _source_files():
    "The daemon's own package modules — what py_compile verifies."
    return sorted(Path(__file__).resolve().parent.glob("*.py"))


def _gate_files():
    "Every file run_gate's verdict depends on: the package modules and the test\n    suite it runs.\n\n    The version key must span both. Keying on the package alone would make a gate\n    failure unrecoverable: run_gate runs server/tests, so fixing a failing test\n    would leave the version key unchanged, _ATTEMPTED_VERSIONS still holding the\n    failure, and the node pinned on old code.\n\n    Delegates to gate_record.gate_files() (policy), the one definition of this list\n    shared with the pre-commit hook and store-wt-finish."
    try:
        return gate_record.gate_files(_server_dir())
    except OSError:
        return list(_source_files())


def _code_version() -> float | None:
    'Newest mtime across everything the gate judges, or None if undeterminable.\n\n    This is an ordering signal only — "the tree was touched since we booted". It\n    cannot answer whether the code actually changed; _code_fingerprint() does that.'
    try:
        return max(f.stat().st_mtime for f in _gate_files())
    except (OSError, ValueError):
        return None


def _server_commit() -> str | None:
    'The newest store commit that touched server/, as of the checkout NOW.\n\n    Recorded at startup (and on a no-op adoption, when the bytes on disk are proven\n    to be what this process runs), so daemon.info and the published fleet row can\n    name the code a daemon is executing in the one vocabulary a person landing a\n    change has: a commit. code_version is an mtime and build= moves only\n    when VERSION is bumped. None when\n    git cannot say -- a fresh clone without history, or no git at all -- which the\n    reader reports as unknown, never as adopted.'
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        out = subprocess.run(
            ["git", "-C", _store_root(), "log", "-1", "--format=%h", "--", "server/"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        return out or None
    return None


def _code_fingerprint() -> str | None:
    "Content identity of everything the gate judges — sha256 over each file's\n    name and bytes — or None if undeterminable.\n\n    An mtime cannot tell a release from a re-touch. Every git operation that\n    rewrites the worktree restamps all of these files while their bytes stay\n    identical, and `rebase --autostash <base>` followed by `rebase --abort` — the\n    path sync() takes on a conflicting integration — does it twice per cycle, which\n    would make _code_version() climb on code that never moved and loop gate → pass →\n    re-exec → notify.\n\n    So the mtime stays the cheap gate in front, and this decides whether there is\n    anything to deploy at all."
    h = hashlib.sha256()
    try:
        for f in _gate_files():
            h.update(f.name.encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    except OSError:
        return None
    return h.hexdigest()


def _info_path() -> Path:
    "Where this daemon records its pid/version — one file per port.\n\n    daemon.info is the record `_should_bounce()` targets and `get_health()`\n    reports. A single shared file would let a second daemon (a lazy spawn that lost\n    the bind race, or a smoke test on a spare port) overwrite the live daemon's\n    record with its own pid and exit, leaving `get_health().pid` naming a dead\n    process.\n\n    Scoping the file by port removes the race: an instance on another port cannot\n    address this record. 8765 keeps the historical name so existing state is not\n    orphaned."
    name = "daemon.info" if PORT == 8765 else f"daemon-{PORT}.info"
    return _state_dir() / name





_STARTED_VERSION: float | None = None


_STARTED_FINGERPRINT: str | None = None


_STARTED_AT: float | None = None

_STARTED_SERVER_COMMIT: str | None = None








_ATTEMPTED_VERSIONS: dict[str | float, float] = {}
_GATE_RETRY_SECS = 3600.0



_GATE_TIMEOUT_SECS = 1500



_LAST_SUCCESSFUL_SYNC: float | None = None
_LAST_SYNC_ATTEMPT: float | None = None
_LAST_SYNC_ERROR: str | None = None




_LAST_SYNC_ERROR_AT: float | None = None

_WATCHDOG_NOTIFIED: bool = False


def _exec_spec() -> dict:
    'Enough to faithfully re-exec this daemon: the interpreter, the original\n    argv (so `python -m agent_context.server` comes back as a module, not a bare\n    script — relative imports depend on that), and the cwd.'
    orig = list(getattr(sys, "orig_argv", []) or [])
    argv = [sys.executable, *orig[1:]] if len(orig) > 1 else [sys.executable, "-m", "agent_context.server"]
    return {"path": sys.executable, "argv": argv, "cwd": os.getcwd()}


def write_daemon_info() -> None:
    'Record (at daemon startup) the PID + code version booted with + how to\n    re-exec, so a later frontend can bounce stale code and the daemon can\n    self-redeploy. Also stashes the started-with version in-process. Best-effort.'
    global _STARTED_VERSION, _STARTED_FINGERPRINT, _STARTED_AT, _STARTED_SERVER_COMMIT
    ver = _code_version()
    _STARTED_VERSION = ver
    _STARTED_FINGERPRINT = _code_fingerprint()
    _STARTED_AT = time.time()
    _STARTED_SERVER_COMMIT = _server_commit()
    
    
    _write_restart_health(record_start(_STARTED_AT))
    with contextlib.suppress(OSError, TypeError):
        _info_path().write_text(json.dumps(
            {"pid": os.getpid(), "code_version": ver, "started_at": _STARTED_AT,
             "code_fingerprint": _STARTED_FINGERPRINT,
             "server_commit": _STARTED_SERVER_COMMIT,
             "exec": _exec_spec(), "log_path": _live_log_path(),
             "last_successful_sync": _LAST_SUCCESSFUL_SYNC,
             "last_sync_attempt": _LAST_SYNC_ATTEMPT,
             "last_sync_error": _LAST_SYNC_ERROR,
             "last_sync_error_at": _LAST_SYNC_ERROR_AT}))








_STARTS_FILE = "daemon-starts"
RESTART_LOOP_STARTS = 3
_STARTS_KEEP_SECS = 86400.0







_EXPECTED_FILE = "daemon-expected-restarts"
_EXPECTED_WINDOW_SECS = 180.0

_EXPECTED_SLACK_SECS = 1.0


def _live_markers(p, t: float) -> list[float]:
    return [s for s in _read_starts(p) if -_EXPECTED_SLACK_SECS <= t - s <= _EXPECTED_WINDOW_SECS]


def _write_markers(p, markers: list[float]) -> None:
    
    paths.write_atomic(p, "".join(f"{s!r}\n" for s in markers))


def expect_restart(now: float | None = None) -> float:
    'Record that the daemon is about to be restarted on purpose, and return the marker\n    so a restart that never happens can cancel it. Best-effort.'
    t = time.time() if now is None else now
    with contextlib.suppress(OSError):
        p = _state_dir() / _EXPECTED_FILE
        _write_markers(p, [*_live_markers(p, t), t])
    return t


def cancel_expected_restart(marker: float) -> None:
    'Drop a marker whose restart did not happen, so it cannot excuse a later crash.'
    with contextlib.suppress(OSError):
        p = _state_dir() / _EXPECTED_FILE
        _write_markers(p, [s for s in _read_starts(p) if s != marker])


def _spend_expected_restart(t: float) -> bool:
    'True when a live marker existed for this start; the oldest one is used up.'
    try:
        p = _state_dir() / _EXPECTED_FILE
        markers = sorted(_live_markers(p, t))
        if not markers:
            return False
        _write_markers(p, markers[1:])
        return True
    except OSError:
        return False


def record_start(now: float | None = None) -> int:
    'Append this start to <state-dir>/daemon-starts and return the starts in the\n    last hour, this one included. A start the deploy path announced with\n    `expect_restart` is not appended, so it returns the count without it. Best-effort:\n    a state dir that cannot be written reports 1, never raises.'
    t = time.time() if now is None else now
    try:
        p = _state_dir() / _STARTS_FILE
        stamps = _read_starts(p)
        if _spend_expected_restart(t):
            return sum(1 for s in stamps if 0 <= t - s <= 3600)
        stamps = [s for s in stamps if t - s <= _STARTS_KEEP_SECS] + [t]
        p.write_text("".join(f"{int(s)}\n" for s in stamps[-500:]))
        return sum(1 for s in stamps if t - s <= 3600)
    except OSError:
        return 1


def _read_starts(p) -> list[float]:
    out = []
    with contextlib.suppress(OSError, ValueError):
        out.extend(float(line) for line in p.read_text().split())
    return out


def starts_last_hour(now: float | None = None) -> int:
    t = time.time() if now is None else now
    try:
        return sum(1 for s in _read_starts(_state_dir() / _STARTS_FILE) if 0 <= t - s <= 3600)
    except OSError:
        return 0


def _write_restart_health(starts: int) -> None:
    "The per-turn notice reads every ok:false verdict in the health dir; this is\n    the restart loop's. Written at startup (a looping daemon may live two seconds)\n    and refreshed by get_health, so it clears an hour after the loop stops."
    try:
        d = _health_dir()
        os.makedirs(d, exist_ok=True)
        ok = starts <= RESTART_LOOP_STARTS
        rec = {"component": "agent-context daemon", "ok": ok, "ts": int(time.time()),
               "failures": [] if ok else [fleet.restart_loop_message("health", starts)]}
        paths.write_atomic(os.path.join(d, "daemon-restarts.json"), json.dumps(rec, indent=2))
    except Exception:
        pass


def _read_daemon_info() -> dict | None:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(_info_path().read_text())
    return None


def _update_daemon_info(**fields) -> None:
    'Merge `fields` into daemon.info, preserving pid/code_version/started_at/exec.\n    Best-effort — a failed write must never disturb the sync loop.'
    with contextlib.suppress(OSError, TypeError, ValueError):
        info = _read_daemon_info() or {}
        if "pid" not in info and _STARTED_VERSION is not None:
            
            
            
            
            
            
            info.update({"pid": os.getpid(), "code_version": _STARTED_VERSION,
                         "code_fingerprint": _STARTED_FINGERPRINT,
                         "server_commit": _STARTED_SERVER_COMMIT,
                         "started_at": _STARTED_AT, "exec": _exec_spec()})
        info.update(fields)
        _info_path().write_text(json.dumps(info))


def _live_log_path() -> str | None:
    "Where this daemon's output goes, resolved from fd 1, never guessed from a\n    filename. None when stdout is a tty/pipe (an unsupervised foreground run).\n\n    <log-dir> holds more than one plausible log. The supervisor's StandardOut/ErrPath\n    is `daemon-supervised.log`; `daemon.log` is a pre-supervision leftover that\n    survives as a stale file with a more convincing name. Publishing the resolved\n    path in daemon.info (and so in get_health) gives the first question any\n    diagnosis asks an authoritative answer."
    with contextlib.suppress(OSError):
        if not stat.S_ISREG(os.fstat(1).st_mode):
            return None
    with contextlib.suppress(Exception):                      
        return os.readlink("/proc/self/fd/1")
    if sys.platform == "darwin":
        with contextlib.suppress(Exception):
            import fcntl  
            F_GETPATH = 50                                    
            buf = fcntl.fcntl(1, F_GETPATH, b"\0" * 1024)
            return buf.rstrip(b"\0").decode() or None
    return None



_LOG_UNLINKED_WARNED: bool = False


def check_log_alive() -> str | None:
    "Warn (once) when this daemon's own stdout is an unlinked regular file.\n\n    The supervisor opens the log; log housekeeping can then remove the path while\n    the daemon keeps writing into the now-nameless inode, and everything written\n    since is unrecoverable. A tty or a pipe is not a log, so only regular files are\n    judged. Returns the message when it fires, else None."
    global _LOG_UNLINKED_WARNED
    try:
        st = os.fstat(1)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 0:
        _LOG_UNLINKED_WARNED = False   
        return None
    if _LOG_UNLINKED_WARNED:
        return None
    _LOG_UNLINKED_WARNED = True
    msg = ("agent-context: this daemon's log file has been UNLINKED — everything written "
           "since goes to a dead inode and cannot be read back. Restart through the "
           "supervisor to reopen it (macOS: launchctl kickstart -k "
           "gui/$(id -u)/org.agent-context.daemon).")
    log.warning(msg)
    _notify(msg)
    return msg


_CYCLE_STARTED_AT: float | None = None



_NEXT_CYCLE_DELAY: float | None = None


def note_next_cycle_delay(secs: float) -> None:
    global _NEXT_CYCLE_DELAY
    _NEXT_CYCLE_DELAY = float(secs)


def _heartbeat_known() -> bool:
    "Has this process recorded any heartbeat? Only a process that has not (a relay,\n    a reader, a daemon before its first cycle) may fall back to daemon.info for the\n    cycle-in-flight stamp: in the daemon itself None means 'between cycles', and the\n    file can hold a stale start when the disk stopped taking writes."
    return (_LAST_SYNC_ATTEMPT is not None or _LAST_SUCCESSFUL_SYNC is not None
            or _CYCLE_STARTED_AT is not None)


def record_cycle_start(now: float | None = None) -> None:
    'A sync cycle has begun. Until it ends, the loop is alive by definition.\n\n    last_sync_attempt is stamped when a cycle ends, so a slow cycle (one push waiting\n    on a mirror) would look like a dead loop to get_health for as long as it ran. The\n    start time tells the two apart, and it travels in daemon.info too, so a relay\n    reading the file sees it.'
    global _CYCLE_STARTED_AT
    t = time.time() if now is None else now
    _CYCLE_STARTED_AT = t
    _update_daemon_info(cycle_started_at=t)


def record_sync_success(now: float | None = None) -> None:
    'Heartbeat: a sync cycle completed without raising. Clears the last error.'
    global _LAST_SUCCESSFUL_SYNC, _LAST_SYNC_ATTEMPT, _LAST_SYNC_ERROR, _LAST_SYNC_ERROR_AT
    global _CYCLE_STARTED_AT
    t = time.time() if now is None else now
    _LAST_SUCCESSFUL_SYNC = t
    _LAST_SYNC_ATTEMPT = t
    _LAST_SYNC_ERROR = None
    _LAST_SYNC_ERROR_AT = None
    _CYCLE_STARTED_AT = None
    
    
    reset_sleep_accounting()
    reset_attempt_sleep_accounting()
    _update_daemon_info(last_successful_sync=t, last_sync_attempt=t, last_sync_error=None,
                        last_sync_error_at=None, cycle_started_at=None)


_LAST_DIVERGENCE: dict | None = None


def record_divergence(div: dict | None) -> None:
    'Record the measured HEAD-vs-mirrors gap so `get_health` can report it.\n\n    Separate from record_sync_success/failure on purpose: the measurement is true\n    regardless of what the cycle concluded, and the failure this closes is a cycle\n    concluding `healthy` over a large commit gap. Storing it here means the gap\n    reaches the health signal on the first cycle, before any streak has had time to\n    flip the verdict.'
    global _LAST_DIVERGENCE
    _LAST_DIVERGENCE = div or {}
    _update_daemon_info(divergence=_LAST_DIVERGENCE)


def record_sync_failure(error, now: float | None = None) -> None:
    "Heartbeat: a sync cycle raised. Records the attempt time + error string but\n    leaves last_successful_sync untouched (that's what staleness is measured from)."
    global _LAST_SYNC_ATTEMPT, _LAST_SYNC_ERROR, _LAST_SYNC_ERROR_AT, _CYCLE_STARTED_AT
    t = time.time() if now is None else now
    _LAST_SYNC_ATTEMPT = t
    _LAST_SYNC_ERROR = str(error)
    _LAST_SYNC_ERROR_AT = t
    _CYCLE_STARTED_AT = None
    reset_attempt_sleep_accounting()
    _update_daemon_info(last_sync_attempt=t, last_sync_error=str(error), last_sync_error_at=t,
                        cycle_started_at=None)










_UNHEALTHY_NOTIFY_CYCLES = 2
_UNHEALTHY_RENOTIFY_SECS = 6 * 3600.0
_UNHEALTHY_STREAK = 0
_UNHEALTHY_NOTIFIED_AT = 0.0


def _write_sync_health_verdict(reason: str | None, streak: int) -> None:
    'Publish sync health where the next session will read it aloud.\n\n    ~/.local/state/agent-context/health/ is that block\'s input: preflight-core-health\'s\n    generic_findings() reads every verdict file there and puts an `ok: false` into\n    the degraded-core-systems report at SessionStart. So this needs no hook change:\n    the daemon is one more probe, as the "adding a core system means adding its\n    probe" rule asks.\n\n    Written on the same threshold that pages (_UNHEALTHY_NOTIFY_CYCLES), since\n    anything that pages should also reach the next session. Cleared on the first\n    healthy cycle.\n\n    Best-effort and silent: the store must keep syncing on a machine where ~/.claude\n    does not exist or is not writable, and a probe that can raise would take down the\n    loop it reports on.'
    try:
        d = _health_dir()
        os.makedirs(d, exist_ok=True)
        ok = not reason or streak < _UNHEALTHY_NOTIFY_CYCLES
        
        
        
        
        
        why = (reason or "").strip().rstrip(".")
        detail = (f"this machine has not synced for {streak} consecutive cycles. "
                  f"{why}. Until it clears, the store this session reads is STALE: "
                  f"memories, docs and observations written on other machines are not "
                  f"here, and everything written here is not reaching them.")
        rec = {"component": "agent-context sync", "ok": ok, "ts": int(time.time()),
               "failures": [] if ok else [detail]}
        
        
        paths.write_atomic(os.path.join(d, "store-sync.json"), json.dumps(rec, indent=2))
    except Exception:
        pass


def note_sync_health(reason: str | None, now: float | None = None) -> str | None:
    'Count consecutive unhealthy sync cycles and page a human once it persists.\n\n    Integration that cannot land stops the fleet converging, so it needs a path to a\n    human beyond a warning per cycle.\n\n    Returns the message sent, or None. Any healthy cycle re-arms the alert, so a\n    condition that comes back pages again.'
    global _UNHEALTHY_STREAK, _UNHEALTHY_NOTIFIED_AT
    t = time.time() if now is None else now
    if not reason:
        _UNHEALTHY_STREAK = 0
        _UNHEALTHY_NOTIFIED_AT = 0.0
        _write_sync_health_verdict(None, 0)
        return None
    _UNHEALTHY_STREAK += 1
    
    
    
    _write_sync_health_verdict(reason, _UNHEALTHY_STREAK)
    if _UNHEALTHY_STREAK < _UNHEALTHY_NOTIFY_CYCLES:
        return None
    if _UNHEALTHY_NOTIFIED_AT and (t - _UNHEALTHY_NOTIFIED_AT) < _UNHEALTHY_RENOTIFY_SECS:
        return None
    _UNHEALTHY_NOTIFIED_AT = t
    host = socket.gethostname().split(".")[0]
    msg = (f"agent-context: sync has failed {_UNHEALTHY_STREAK} cycles in a row on "
           f"{host} — {reason}")
    log.warning(msg)
    _notify(msg)
    return msg

















_BACKOFF_AFTER_CYCLES = 6
_BACKOFF_MAX_SECS = 3600.0



_POKE_SLICE_SECS = 1.0


def sync_poke_path() -> Path:
    'The file a hook touches to ask for a sync cycle now (see _BACKOFF_AFTER_CYCLES).'
    return _state_dir() / "sync-requested"


def sync_requested() -> bool:
    'Consume a pending poke. True once per touch; never raises.'
    p = sync_poke_path()
    try:
        if p.exists():
            p.unlink()
            return True
    except OSError:
        pass
    return False


def sleep_until_poked(delay: float, slice_secs: float | None = None,
                      _sleep=None, _requested=None) -> bool:
    'Sleep `delay` seconds in slices, returning early (True) on a poke.\n\n    Sleeping the whole retry delay in one call would leave a fault that a hook already\n    fixed "failing" in daemon.info, the health file and the fleet row until the delay\n    ran out. A hook cannot call the daemon, but it can touch a file, and this is the\n    daemon looking for it.'
    step = _POKE_SLICE_SECS if slice_secs is None else slice_secs
    ask = sync_requested if _requested is None else _requested
    
    
    zzz = time.sleep if _sleep is None else _sleep
    left = float(delay)
    
    
    while True:
        zzz(max(0.0, min(step, left)))
        left -= step
        if ask():
            return True
        if left <= 0:
            return False


def sync_retry_delay(interval: float, streak: int | None = None,
                     network: bool = True) -> float:
    'Seconds to wait before the next sync cycle: `interval` while healthy, doubling\n    once failures persist past the alert threshold, capped at an hour. Any healthy\n    cycle clears the streak in note_sync_health and restores the base interval.\n\n    `network=False` means the failing cycle returned before touching a remote -- a\n    dirty server/ tree or a truncated worktree. Those cost one local `git status`, so\n    backing off throttles a cost that was never incurred, and the only thing the delay\n    buys is a longer wait once the fault clears.\n\n    The quota that motivated backoff at all (1Password reads) is spent in fetch and\n    push. A cycle that reaches neither\n    cannot spend it, so there is nothing here to protect.'
    s = _UNHEALTHY_STREAK if streak is None else streak
    if s < _BACKOFF_AFTER_CYCLES or not network:
        return float(interval)
    return min(_BACKOFF_MAX_SECS, float(interval) * 2.0 ** (s - _BACKOFF_AFTER_CYCLES + 1))


def _should_bounce() -> bool:
    disk = _code_version()
    info = _read_daemon_info()
    run = info.get("code_version") if info else None
    if disk is None or run is None or info is None:  
        return False
    if disk <= run + 1e-6:           
        return False
    
    
    
    
    ran_fp = info.get("code_fingerprint")
    if ran_fp:
        fp = _code_fingerprint()
        if fp is not None and fp == ran_fp:
            return False
    return True


def _bounce_daemon() -> None:
    "Restart the running daemon onto the on-disk code.\n\n    Supervised host: ask the supervisor (launchctl kickstart -k / systemctl restart) —\n    it is the only actor that can restart its own child cleanly, and its wrapper takes\n    the port over from any lazy spawn. Otherwise SIGTERM whoever owns the port, falling\n    back to daemon.info's pid only when the OS cannot say; the recorded pid can name\n    a dead process. A clean SIGTERM lets an in-flight request finish."
    sup = _supervisor()
    if sup:
        
        
        marker = expect_restart()
        restarted = False
        with contextlib.suppress(Exception):
            done = subprocess.run(_supervisor_restart_cmd(sup), capture_output=True, timeout=15)
            restarted = getattr(done, "returncode", 0) == 0
        if not restarted:
            cancel_expected_restart(marker)
        _wait_port(False, 5)
        return
    pid = _port_owner_pid()
    if not pid:
        info = _read_daemon_info()
        pid = info.get("pid") if info else None
    if not pid:
        return
    marker = expect_restart()
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        cancel_expected_restart(marker)
    _wait_port(False, 5)


def _notify(msg: str) -> None:
    'Best-effort CLI notification; the absence (or failure) of `notify` on PATH\n    must never affect daemon lifecycle, so everything is suppressed.'
    with contextlib.suppress(Exception):
        subprocess.run(["notify", msg], capture_output=True, timeout=5)


def _gate_detail(out: str, cap: int = 700) -> str:
    "The part of pytest's output a human can act on from a phone alert: the\n    'short test summary' (which -rfE fills with every FAILED/ERROR id and a one-line\n    reason), capped. A bare last-N-chars tail can cut the failing test id in half and\n    drop the reason."
    i = out.rfind("short test summary info")
    tail = out[i:] if i >= 0 else out
    return tail[-cap:]


def _timeout_detail(out: str, cap: int = 1200) -> str:
    'The part of a timed-out gate a human can act on: the head of faulthandler\'s\n    dump, which is where the hung test is.\n\n    _gate_detail keeps the tail, which is right for a failure (pytest\'s short test\n    summary is last) and wrong for a timeout. faulthandler prints each thread "most\n    recent call first", so the innermost frame, the one naming the test, is the first\n    line after the header, and the tail holds only pytest\'s own outer frames plus\n    runpy.'
    i = out.find("Timeout (")
    if i < 0:
        i = out.find("Thread 0x")
    if i < 0:
        i = out.find("Current thread")
    return out[i:i + cap] if i >= 0 else out[:cap]









GATE_FAILED = "failed:"
GATE_INTERRUPTED = gate_lock.INTERRUPTED_PREFIX
GATE_TIMEOUT = "timeout:"
_GATE_OUTPUT_KEEP = 256 * 1024


def gate_reason_class(detail: str) -> str:
    '"failed", "interrupted" or "timeout" for a gate detail, "" for a pass or an unclassified one.'
    for prefix in (GATE_FAILED, GATE_INTERRUPTED, GATE_TIMEOUT):
        if detail.startswith(prefix):
            return prefix[:-1]
    return ""


def gate_reason_text(detail: str) -> tuple[str, str]:
    '(class, detail without its class prefix); an unclassified detail reads as failed.'
    cls = gate_reason_class(detail)
    if not cls:
        return "failed", detail
    return cls, detail[len(cls) + 1:].lstrip()


def _gate_rerun_enabled() -> bool:
    return os.environ.get("AGENT_CONTEXT_GATE_RERUN", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _gate_wall_budget(timeout: int) -> int:
    'The longest one gate can take: the first run plus the whole-suite rerun a failure gets,\n    each with its own full budget. A party waiting behind the gate waits this long, so it never\n    gives up between the two runs. The exit-time join (_bounce_join_secs) is unchanged.'
    return timeout * (2 if _gate_rerun_enabled() else 1)


_PROGRESS = re.compile(r"\[\s*(\d{1,3})%\]")


def _timeout_projection(out: str, timeout: int) -> str:
    'How far a timed-out run got and how long a full run would take at that pace, with the\n    setting that fixes it. Empty when the output has no usable percentage. The pace is the\n    average so far, so the number is an estimate.'
    marks = [int(m) for m in _PROGRESS.findall(out or "")]
    pct = marks[-1] if marks else 0
    if not 0 < pct < 100:
        return ""
    need = round(timeout * 100 / pct)
    return (f"reached {pct}% in {timeout} s, so a full run needs about {need} s on this node; "
            f"set AGENT_CONTEXT_GATE_TIMEOUT above {need} (seconds) in the daemon's environment; ")


def _signal_name(returncode: int) -> str:
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"signal {-returncode}"





_PYTEST_BASE_ARGS = ("-q", "-rfE", "--tb=line", "--color=no")

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _failing_ids(text: str, limit: int = 20) -> list[str]:
    "Test ids from pytest's `-rfE` summary lines (FAILED/ERROR <id> - <reason>).\n\n    --color=no on the pytest command keeps this plain in the gate's own runs; ANSI escapes are\n    still stripped here because a caller's inherited FORCE_COLOR can override that flag, and a\n    parse that finds nothing on a colored run would make a real failure look like an empty\n    list of ids."
    ids: list[str] = []
    for line in text.splitlines():
        m = re.match(r"(?:FAILED|ERROR) (\S+)", _ANSI_ESCAPE.sub("", line))
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids[:limit]


def _save_gate_output(out: str, err: str, note: str) -> str:
    'Keep the tail of one pytest run\'s full output where a person can read it: the log line\n    carries a bounded summary and this file carries the rest. The previous run\'s file stays\n    beside it. Returns the path, or "" when it could not be written.'
    try:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        last, prev = d / "gate-last-run.log", d / "gate-prev-run.log"
        body = ((out or "") + (f"\n--- stderr ---\n{err}" if err else ""))[-_GATE_OUTPUT_KEEP:]
        
        
        
        
        if last.exists():
            paths.write_atomic(str(prev), last.read_bytes(), mode=0o600)
        paths.write_atomic(str(last), f"# {note}\n{body}", mode=0o600)
        return str(last)
    except OSError:
        return ""


def _child_cpu_secs() -> float:
    "Cumulative CPU seconds burned by this process's children, or 0.0 where the\n    platform cannot say. Only ever read as a delta around one child."
    with contextlib.suppress(Exception):
        import resource
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        return ru.ru_utime + ru.ru_stime
    return 0.0


def _throttle_hint(cpu: float, wall: float) -> str:
    'Name waiting when a timed-out gate barely used the CPU, or "".\n\n    A gate that spent almost no CPU inside a long wall time was not slow, it was\n    held. Saying so saves the reader from hunting for a hung test that is not there.\n\n    The remedy is per-platform, so the hint is too. A hint that names the wrong\n    remedy is worse than no hint: the reader stops looking. Give each platform only\n    what can be true on it.\n\n    macOS: launchd puts a job and everything it spawns in a throttled I/O band unless\n    ProcessType says otherwise. `Adaptive` does not say otherwise for this daemon:\n    launchd promotes Adaptive jobs on XPC activity, and this one serves HTTP on a TCP\n    port and has none, so it stays in the background band.\n\n    Elsewhere: no such band exists, so the process was waiting on something real.\n    Slow storage under the tests\' TMPDIR (fsync against a slow disk array; see\n    `store._write_atomic`) produces the same low-CPU signature.'
    if wall <= 0 or cpu <= 0 or (cpu / wall) > 0.25:
        return ""
    shape = (f"used only {cpu:.0f}s CPU across {wall:.0f}s wall ({cpu / wall:.0%}) — the gate "
             f"was WAITING, not computing, so it is not a hung test. ")
    if sys.platform == "darwin":
        return shape + ("Check the supervisor's QoS: the LaunchAgent's ProcessType must be "
                        "Standard or Interactive, never Background and never Adaptive (no XPC "
                        "here, so it is never promoted). ")
    return shape + ("There is no launchd QoS band on this platform — look for blocking I/O. "
                    "Check what filesystem TMPDIR lands on (pytest's tmp_path lives there) and "
                    "time an fsync against it; a spinning or network volume charges hundreds of "
                    "ms per flush and the suite makes hundreds of writes. ")


def _gate_env() -> dict[str, str]:
    "The gate's environment, with TMPDIR on the state dir's filesystem.\n\n    pytest's tmp_path lives under TMPDIR, and some tests write an executable there. The\n    NAS nodes mount the system temp dir noexec, so those tests would fail the gate while\n    the code was fine. The state dir is under $HOME, which is\n    executable on every node. If the directory cannot be made, the inherited TMPDIR stays."
    env = dict(os.environ)
    try:
        tmp = _state_dir() / "gate-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmp)
    except OSError:
        pass
    return env


_GATE_SYNC_TIMEOUT_SECS = 180


def _gate_sync_enabled() -> bool:
    return os.environ.get("AGENT_CONTEXT_GATE_SYNC", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _uv_path() -> str | None:
    'uv from PATH, else ~/.local/bin/uv: the NAS init script starts the daemon with a bare\n    PATH that does not carry ~/.local/bin (memory agent-context-fleet-bootstrap-ops).'
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home() / ".local" / "bin" / "uv"
    return str(home) if os.access(home, os.X_OK) else None


def _sync_gate_venv(server_dir: Path, timeout: int) -> str:
    "Bring the running venv up to the new code's lockfile, dev group included, before the\n    suite runs. The venv is only ever synced by hand, so a dependency added to the lock\n    would fail collection and pin the node on old code while the code was fine. `--inexact` never removes a package, so the old code still\n    running here keeps every import it has. Returns a one-line outcome for the log; a failed\n    or skipped sync never fails the gate itself: the suite then says what is missing.\n\n    Only the server's own venv is synced: a daemon on another interpreter is left alone."
    if not _gate_sync_enabled():
        return "skipped: AGENT_CONTEXT_GATE_SYNC is off"
    venv = server_dir / ".venv"
    try:
        if Path(sys.prefix).resolve() != venv.resolve():
            return f"skipped: the daemon runs from {sys.prefix}, not {venv}"
    except OSError as e:
        return f"skipped: {e!r}"
    uv = _uv_path()
    if not uv:
        return "skipped: uv not found on PATH or in ~/.local/bin"
    try:
        r = subprocess.run([uv, "sync", "--frozen", "--inexact", "--group", "dev",
                            "--directory", str(server_dir)],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"failed: uv sync timed out (>{timeout}s)"
    except OSError as e:
        return f"failed: {e!r}"
    if r.returncode != 0:
        return f"failed: uv sync exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}"
    return "ok"


def _server_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent          


def run_gate(timeout: int = _GATE_TIMEOUT_SECS) -> tuple[bool, str]:
    'Verify the on-disk server code before bouncing into it: py_compile every\n    package module, then run the pytest suite. Returns (ok, detail). A non-pass detail starts\n    with its class: `failed:`, `interrupted:` (a signal ended the run: no verdict on the\n    code) or `timeout:`; see `gate_reason_class`. A failed suite is rerun once, whole.\n\n    Any inability to verify — pytest missing, a timeout, or an infra error — yields\n    ok=False so the caller keeps the known-good running daemon rather than bounce\n    blindly into unverified code. Only used to guard up→newer bounces, never the\n    cold-start path.'
    server_dir = _server_dir()
    tests_dir = server_dir / "tests"
    py = sys.executable
    deadline = time.monotonic() + timeout
    try:
        files = [str(f) for f in _source_files()]
        c = subprocess.run([py, "-m", "py_compile", *files],
                           capture_output=True, text=True, timeout=timeout)
        if c.returncode != 0:
            if c.returncode < 0:
                return False, (f"{GATE_INTERRUPTED} py_compile ended by "
                               f"{_signal_name(c.returncode)}; this run says nothing about the code")
            return False, f"{GATE_FAILED} py_compile failed: {(c.stderr or c.stdout)[-300:]}"
        gate_key = None
        if tests_dir.is_dir():
            gate_key = gate_record.gate_key(server_dir, py)
            cached = gate_record.has_passed(gate_key) if gate_key is not None else None
            if gate_key is not None and cached is not None:
                log.info("agent-context: gate_cached fp=%s recorded_by=%s", gate_key[:8],
                         cached.get("by"))
                return True, "gate passed (cached: recorded pass, no pytest run)"
            sync_budget = max(1, min(_GATE_SYNC_TIMEOUT_SECS, int(deadline - time.monotonic()) // 3))
            log.info("agent-context: gate_venv_sync %s", _sync_gate_venv(server_dir, sync_budget))
            remaining = max(1, int(deadline - time.monotonic()))
            kind, detail, first_ids = _run_pytest(py, server_dir, tests_dir, remaining, timeout, 1)
            if kind == "failed" and _gate_rerun_enabled():
                
                
                
                
                
                
                shown = ", ".join(first_ids) or "no test ids"
                log.warning("agent-context: gate_rerun: the suite failed (%s); running the whole "
                            "suite once more before any hold", shown)
                kind, detail2, _ = _run_pytest(py, server_dir, tests_dir, timeout, timeout, 2)
                if kind == "pass":
                    log.warning("agent-context: gate_flaked: passed on the whole-suite rerun; the "
                                "first run failed: %s", shown)
                    if gate_key is not None:
                        gate_record.record_pass(gate_key, {}, "daemon")
                    return True, f"gate passed (on the whole-suite rerun; first run failed: {shown})"
                if kind == "failed":
                    return False, (f"{GATE_FAILED} reproduced on the whole-suite rerun. "
                                   f"{detail2[len(GATE_FAILED):].lstrip()}")
                return False, detail2       
            if kind != "pass":
                return False, detail
            if gate_key is not None:
                gate_record.record_pass(gate_key, {}, "daemon")
        return True, "gate passed"
    except subprocess.TimeoutExpired:
        return False, f"{GATE_TIMEOUT} gate timed out (>{timeout}s) in py_compile"
    except Exception as e:  
        return False, f"{GATE_FAILED} gate could not run: {e!r}"


def _run_pytest(py: str, server_dir: Path, tests_dir: Path, remaining: int, timeout: int,
                attempt: int) -> tuple[str, str, list[str]]:
    'One whole-suite pytest run. Returns (kind, detail, failing test ids) where kind is\n    "pass", "failed", "interrupted" or "timeout" and the detail of a non-pass starts with the\n    class (`gate_reason_class`). The full output of every run is kept by `_save_gate_output`.'
    
    
    
    
    
    
    
    
    dump_after = min(_gate_per_test_secs(), max(5, remaining - 10))
    proc = subprocess.Popen(
        [py, "-m", "pytest", *_PYTEST_BASE_ARGS, *gate_record.parallel_args(py), "-p", "faulthandler",
         "-o", f"faulthandler_timeout={dump_after}", str(tests_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(server_dir),
        env=_gate_env())
    log.info("agent-context: gate pytest running as pid %s (timeout %ss, attempt %d)",
             proc.pid, remaining, attempt)
    cpu0, wall0 = _child_cpu_secs(), time.monotonic()
    try:
        out, err = proc.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        
        
        hint = _throttle_hint(_child_cpu_secs() - cpu0, time.monotonic() - wall0)
        _save_gate_output(out, err, f"attempt {attempt}: timed out, pytest pid {proc.pid}")
        return "timeout", (f"{GATE_TIMEOUT} gate timed out (>{timeout}s) in pytest pid {proc.pid}; "
                           f"{hint}{_timeout_projection(out or '', remaining)}"
                           f"stacks: {_timeout_detail((err or '') + (out or ''))}"), []
    rc = proc.returncode
    text = (out or "") + (err or "")
    saved = _save_gate_output(out, err, f"attempt {attempt}: pytest pid {proc.pid}, exit {rc}")
    if rc == 0:
        return "pass", "gate passed", []
    signalled = isinstance(rc, int) and rc < 0
    
    
    if signalled or (rc == 2 and "KeyboardInterrupt" in text[-4000:]):
        how = _signal_name(rc) if signalled else "an interrupt"
        return "interrupted", (f"{GATE_INTERRUPTED} pytest ended by {how} (its service was "
                               "stopped or the process was killed); this run says nothing about "
                               "the code"), []
    ids = _failing_ids(text)
    where = f" (full output: {saved})" if saved else ""
    named = f" [{len(ids)} failing: {', '.join(ids)}]" if ids else ""
    
    
    kind = "failed" if rc == 1 else "error"
    return kind, f"{GATE_FAILED} pytest failed: {_gate_detail(out or err)}{named}{where}", ids








def _self_deploy_enabled() -> bool:
    return os.environ.get("AGENT_CONTEXT_SELF_DEPLOY", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _server_tree_dirty() -> bool:
    "True if the store's server/ tree has uncommitted changes (an in-progress\n    write/release). Conservative: any error → report dirty so we skip the redeploy."
    store_root = Path(__file__).resolve().parents[3]   
    try:
        r = subprocess.run(["git", "-C", str(store_root), "status", "--porcelain", "--", "server/"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode != 0 or bool(r.stdout.strip())
    except Exception:
        return True













_INFLIGHT = 0
_INFLIGHT_LOCK = threading.Lock()
_DRAIN_SECS = 10.0


def inflight_enter() -> None:
    global _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT += 1


def inflight_exit() -> None:
    global _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT = max(0, _INFLIGHT - 1)


def inflight() -> int:
    with _INFLIGHT_LOCK:
        return _INFLIGHT


def drain_inflight(timeout: float = _DRAIN_SECS, poll: float = 0.05) -> bool:
    'Wait until no tool call is in flight, or `timeout` passes. True when drained.\n\n    Called before the store lock is taken for a re-exec, not inside it: a request\n    that is itself waiting on the store lock could otherwise never finish, and the\n    drain would always run to its deadline.'
    deadline = time.monotonic() + max(0.0, timeout)
    while inflight() > 0:
        if time.monotonic() >= deadline:
            log.warning("agent-context: re-exec proceeding with %d request(s) still in "
                        "flight after %.0fs -- they will be dropped", inflight(), timeout)
            return False
        time.sleep(poll)
    return True


def _do_exec() -> None:
    'Re-exec this process onto the on-disk code. Factored out so tests can patch\n    it instead of actually replacing the process image. Env is preserved across\n    execv, so the daemon comes back in the same (http) mode.'
    info = _read_daemon_info() or {}
    ex = info.get("exec") or {}
    argv = ex.get("argv") or [sys.executable, "-m", "agent_context.server"]
    path = ex.get("path") or (argv[0] if argv else sys.executable)
    cwd = ex.get("cwd")
    if cwd and os.path.isdir(cwd):
        with contextlib.suppress(OSError):
            os.chdir(cwd)
    log.info("agent-context: re-exec onto new code: %s", argv)
    marker = expect_restart()
    try:
        os.execv(path, argv)
    except OSError:
        cancel_expected_restart(marker)
        raise











_CODE_DEFER_REASON: str | None = None


def _set_defer_reason(reason: str | None) -> None:
    global _CODE_DEFER_REASON
    _CODE_DEFER_REASON = reason


def code_defer_reason() -> str | None:
    'Why the newer code on disk has not been adopted, or None when nothing is pending.'
    return _CODE_DEFER_REASON


def _gate_failed_recently(version: str | float) -> bool:
    "True if this exact build already failed its gate inside the cooldown, so a\n    retry would only burn the suite again. Outside it, the build is retried —\n    that is the only route off a node pinned by a cause the version key can't see."
    at = _ATTEMPTED_VERSIONS.get(version)
    return at is not None and (time.monotonic() - at) < _GATE_RETRY_SECS


def _adopt_code_version(disk: float) -> None:
    "Accept the on-disk mtime as this process's own version without re-execing.\n\n    For the case where the tree was re-touched but its content is what we are\n    already running. Without adopting it the mtime comparison re-fires every cycle;\n    and because the frontend's _should_bounce() reads daemon.info rather than this\n    process's memory, the adopted version has to be published there too — otherwise\n    the next relay bounces the daemon we just decided not to bounce."
    global _STARTED_VERSION, _STARTED_SERVER_COMMIT
    _STARTED_VERSION = disk
    fields: dict[str, object] = {"code_version": disk}
    
    
    
    commit = _server_commit()
    if commit:
        _STARTED_SERVER_COMMIT = fields["server_commit"] = commit
    _update_daemon_info(**fields)





_GATE_PER_TEST_SECS = 20


def _gate_per_test_secs() -> int:
    raw = os.environ.get("AGENT_CONTEXT_GATE_PER_TEST")
    with contextlib.suppress(ValueError, TypeError):
        if raw:
            return max(5, int(raw))
    return _GATE_PER_TEST_SECS


def _gate_timeout_secs(default: int) -> int:
    raw = os.environ.get("AGENT_CONTEXT_GATE_TIMEOUT")
    with contextlib.suppress(ValueError, TypeError):
        if raw:
            return max(30, int(raw))
    return default







_HOME_CONVERGE_SECS = 6 * 3600.0
_HOME_CONVERGED_AT = 0.0


def maybe_converge_home(root=None, now: float | None = None, interval: float | None = None) -> str | None:
    "Run the store's home-materialize.py at most once per interval. Returns the\n    action taken, or None when it was throttled or unavailable.\n\n    Best-effort by contract: a node whose projection cannot be written must keep\n    syncing, so every failure is logged and swallowed rather than raised."
    global _HOME_CONVERGED_AT
    t = time.time() if now is None else now
    every = _HOME_CONVERGE_SECS if interval is None else interval
    if _HOME_CONVERGED_AT and (t - _HOME_CONVERGED_AT) < every:
        return None
    script = os.path.join(_store_root(root), "global", "scripts", "home-materialize.py")
    if not os.path.isfile(script):
        return None
    _HOME_CONVERGED_AT = t   
                             
    try:
        p = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=180)
    except Exception as e:
        log.warning("agent-context: home-materialize failed to run: %s", e)
        return None
    if p.returncode != 0:
        log.warning("agent-context: home-materialize exited %s: %s",
                    p.returncode, (p.stderr or "").strip()[:500])
        return None
    log.info("agent-context: home projection converged")
    return "converged"


def _metric(event_type: str, ts: str | None = None, **fields: object) -> None:
    'Append one event to the metrics log. Any failure is ignored: a deploy decision never\n    depends on whether a measurement was written.'
    try:
        from agent_context import metrics
        metrics.emit(event_type, ts=ts, **fields)
    except Exception:
        pass


def _shared_gate(key: str, role: str, timeout: int) -> gate_lock.Outcome:
    "Run the deploy gate through the machine-wide single flight: the daemon's self-redeploy and\n    every session's bridge share one gate.lock and one recorded verdict per code fingerprint, so\n    the suite runs once per build and nobody restarts the daemon under another party's gate."
    def _current() -> str:
        fp = _code_fingerprint()
        disk = _code_version()
        if fp is None and disk is None:
            raise RuntimeError("code identity is unreadable")     
        return str(fp if fp is not None else disk)

    def _run() -> tuple[bool, str]:
        
        
        _metric("gate_start", ref=role)
        began = time.monotonic()
        result = run_gate(timeout=timeout)
        _metric("gate_end", ref=role, ok=result[0], dur_ms=int((time.monotonic() - began) * 1000))
        return result

    return gate_lock.single_flight(
        _state_dir(), key, role, _run, current_fingerprint=_current,
        timeout=float(_gate_wall_budget(timeout)), retry_secs=_GATE_RETRY_SECS)


def maybe_self_redeploy(store=None, gate_timeout: int = _GATE_TIMEOUT_SECS) -> None:
    "After a sync cycle, self-redeploy if the on-disk code is strictly newer than\n    what this process booted with AND the gate passes. Called from the daemon sync\n    loop; a no-op on cold start (disk == started) and when disabled by env.\n\n    The gate ceiling (_GATE_TIMEOUT_SECS) is headroom, not an expectation. It is not a\n    substitute for correct supervisor QoS. A daemon spawned in macOS's throttled band\n    puts pytest there too, and no ceiling survives that. ProcessType=Background causes\n    it; ProcessType=Adaptive does not cure it, because launchd promotes Adaptive jobs on\n    XPC activity and this daemon has none (HTTP on a TCP port). It is Standard.\n    If this times out, read the CPU-vs-wall hint in the message before looking for a\n    hung test: see _throttle_hint. The NAS nodes are slow enough on their own to want\n    the margin regardless.\n\n    Loop-safe on two axes: it only fires when disk is strictly newer than\n    started-with (after the re-exec the new process's started-with == that version),\n    and only when the code's content differs from what we booted: a tree that was\n    merely rewritten in place can never trigger a deploy. A failed gate is recorded\n    against that content so the same build isn't re-attempted (logged once)."
    if not _self_deploy_enabled():
        _set_defer_reason("self-deploy is disabled by env on this machine")
        return
    started, disk = _STARTED_VERSION, _code_version()
    if started is None or disk is None:      
        _set_defer_reason(None)
        return
    if disk <= started + 1e-6:               
        _set_defer_reason(None)              
        return
    
    
    
    
    
    fp = _code_fingerprint()
    if fp is not None and fp == _STARTED_FINGERPRINT:
        log.info("agent-context: server tree re-touched (newer mtime, identical "
                 "content) — adopting the timestamp, not redeploying")
        _adopt_code_version(disk)
        return
    key = fp if fp is not None else disk     
    if _gate_failed_recently(key):           
        _set_defer_reason("the gate failed on this code and is cooling off — read the "
                          "gate lines in the log; a restart masks it rather than fixing it")
        return
    
    
    if _server_tree_dirty():
        log.info("agent-context: newer server code on disk but server/ tree is dirty — "
                 "deferring self-redeploy to a later cycle")
        _set_defer_reason("server/ has uncommitted edits, so the gate has not run — "
                          "release or revert them")
        return
    
    
    _ATTEMPTED_VERSIONS[key] = time.monotonic()
    outcome = _shared_gate(str(key), gate_lock.ROLE_DAEMON, _gate_timeout_secs(gate_timeout))
    if outcome.ok is None:
        
        
        _ATTEMPTED_VERSIONS.pop(key, None)
        _set_defer_reason(f"the gate gave no verdict on this code ({outcome.source}); "
                          "it retries on the next cycle")
        return
    if not outcome.ok and outcome.source != "ran":
        
        _set_defer_reason("the gate failed on this code and is cooling off — read the "
                          "gate lines in the log; a restart masks it rather than fixing it")
        return
    ok, detail = outcome.ok, outcome.detail
    if not ok:
        _metric("gate_cooloff_start", ref=gate_lock.ROLE_DAEMON, dur_ms=int(_GATE_RETRY_SECS * 1000))
        cls, body = gate_reason_text(detail)
        verb = {"failed": "failed", "timeout": "timed out"}.get(cls, cls)
        log.warning("agent-context: newer server code failed the gate (reason class %s): %s. "
                    "not self-redeploying; keeping the current known-good code", cls, body)
        _set_defer_reason(f"the gate ran and failed ({cls}): {body}")
        _notify(f"agent-context: self-redeploy skipped, gate {verb} on new code "
                f"(reason class {cls}): {body}")
        return  
    
    
    
    def _announce_and_exec():
        log.info("agent-context: newer server code passed the gate — self-redeploying (re-exec)")
        _do_exec()

    if store is None:
        _announce_and_exec()
        return

    
    
    
    
    drain_inflight()

    
    
    if not store.lock.acquire(timeout=10):
        log.info("agent-context: store busy — deferring self-redeploy")
        _ATTEMPTED_VERSIONS.pop(key, None)    
        _set_defer_reason("the store was busy when the re-exec was due; transient, it "
                          "retries on the next cycle")
        return
    
    
    
    
    
    
    
    
    try:
        if _server_tree_dirty():
            _ATTEMPTED_VERSIONS.pop(key, None)
            return
        _announce_and_exec()
    finally:
        store.lock.release()









_SYNC_INTERVAL = 300
_WATCHDOG_INTERVAL = 60


def _sync_stall_secs() -> float:
    raw = os.environ.get("AGENT_CONTEXT_SYNC_STALL_SECS")
    if raw:
        with contextlib.suppress(ValueError):
            v = float(raw)
            if v > 0:
                return v
    return 3.0 * _SYNC_INTERVAL   


def _watchdog_restart_enabled() -> bool:
    return os.environ.get("AGENT_CONTEXT_WATCHDOG_RESTART", "").strip().lower() in (
        "1", "true", "yes", "on")













_SLEEP_EPSILON = 30.0        
_SLEEP_STATE: dict[str, float] = {"wall": 0.0, "mono": 0.0, "slept": 0.0,
                                  
                                  
                                  "since_attempt": 0.0}


def note_sleep_gap(wall=None, mono=None) -> float:
    'Sample the clocks; return seconds spent suspended since the previous sample.\n\n    Called once per watchdog tick. The first call only seeds the baseline. A negative\n    or sub-epsilon skew is ordinary jitter and contributes nothing.'
    wall = time.time() if wall is None else wall
    mono = time.monotonic() if mono is None else mono
    prev_wall, prev_mono = _SLEEP_STATE["wall"], _SLEEP_STATE["mono"]
    _SLEEP_STATE["wall"], _SLEEP_STATE["mono"] = wall, mono
    if not prev_wall:
        return 0.0
    gap = (wall - prev_wall) - (mono - prev_mono)
    if gap <= _SLEEP_EPSILON:
        return 0.0
    _SLEEP_STATE["slept"] += gap
    _SLEEP_STATE["since_attempt"] += gap
    return gap


def slept_secs() -> float:
    'Total time this process has spent suspended, as measured by note_sleep_gap.'
    return _SLEEP_STATE["slept"]


def slept_since_attempt_secs() -> float:
    "Suspended time since the last recorded sync attempt (or cycle start).\n\n    get_health's loop_alive is an age-since-attempt, and the watchdog's `slept` resets\n    only on success, so a failing loop on a laptop that sleeps would discount time it\n    had already been judged on, and a lid-close would read as loop-dead on wake until\n    the next cycle ran."
    return _SLEEP_STATE["since_attempt"]


def reset_sleep_accounting() -> None:
    'Forget accumulated suspend time — called when a sync succeeds, since the age\n    being judged restarts from there.'
    _SLEEP_STATE["slept"] = 0.0


def reset_attempt_sleep_accounting() -> None:
    'Called on every attempt record (success or failure): the attempt clock restarts.'
    _SLEEP_STATE["since_attempt"] = 0.0


def watchdog_decision(now, last_success, threshold, already_notified, restart_enabled,
                      last_attempt=None, slept=0.0):
    "Pure decision core → (should_notify, should_restart, reason).\n\n    - last_success None → nothing to judge (the loop supplies a started_at fallback).\n    - age ≤ threshold → healthy.\n    - age > threshold → stalled: notify only on entering the stall (not already\n      notified), so we don't alert every cycle.\n    - restart only when restart_enabled and the loop looks wedged: not just the\n      remote being down (attempts still happening) but the loop not even attempting\n      (last_attempt is also older than threshold). Default off.\n    - `slept` is time the machine was suspended inside that age. It is\n      discounted from every age computed here, including the wedge test, because a\n      frozen process is not a process failing to attempt."
    if last_success is None:
        return (False, False, "no successful sync recorded yet")
    slept = max(0.0, slept)
    raw = now - last_success
    age = max(0.0, raw - slept)
    if age <= threshold:
        note = ""
        if slept > _SLEEP_EPSILON:
            note = (f" ({int(raw)}s wall, {int(slept)}s of it suspended — "
                    f"asleep, not stalled)")
        return (False, False, f"healthy: last successful sync {int(age)}s ago{note}")
    reason = f"stalled: last successful sync {int(age)}s ago (> {int(threshold)}s threshold)"
    if slept > _SLEEP_EPSILON:
        reason += f", after discounting {int(slept)}s suspended"
    should_notify = not already_notified
    wedged = bool(restart_enabled and last_attempt is not None
                  and max(0.0, (now - last_attempt) - slept) > threshold)
    return (should_notify, wedged, reason)


def _watchdog_loop(store=None, interval: int | None = None) -> None:
    global _WATCHDOG_NOTIFIED
    interval = interval or _WATCHDOG_INTERVAL
    while True:
        time.sleep(interval)
        try:
            
            
            note_sleep_gap()
            now = time.time()
            threshold = _sync_stall_secs()
            baseline = _LAST_SUCCESSFUL_SYNC
            if baseline is None:  
                baseline = (_read_daemon_info() or {}).get("started_at")
            slept = slept_secs()
            notify, restart, reason = watchdog_decision(
                now, baseline, threshold, _WATCHDOG_NOTIFIED,
                _watchdog_restart_enabled(), last_attempt=_LAST_SYNC_ATTEMPT,
                slept=slept)
            stalled = (baseline is not None
                       and max(0.0, (now - baseline) - slept) > threshold)
            if notify:
                log.warning("agent-context: WATCHDOG — %s", reason)
                _notify(f"agent-context: sync loop {reason}")
                _WATCHDOG_NOTIFIED = True
            if not stalled:
                _WATCHDOG_NOTIFIED = False  
            if restart:
                log.warning("agent-context: WATCHDOG self-recovery enabled and loop wedged "
                            "(%s) — re-exec", reason)
                _notify("agent-context: watchdog re-exec (sync loop wedged)")
                _do_exec()
        except Exception as e:  
            log.warning("agent-context: watchdog check failed: %s", e)


def start_watchdog(store=None) -> None:
    'Start the liveness watchdog as a daemon thread (best-effort).'
    threading.Thread(target=_watchdog_loop, args=(store,), daemon=True).start()


def code_currency(info: dict) -> bool | None:
    'Is this daemon running the code that is on disk? None when undecidable.\n\n    `verdict` measures sync only, so a machine whose sync is fine but whose deploy gate\n    keeps failing would report `healthy` while running old code. Sync health and code\n    currency are different questions, and a daemon that answers only the first is not\n    reporting its health.\n\n    Uses the same content-identity discipline as _should_bounce: a newer mtime over\n    byte-identical code is a re-touched tree and carries no release, so it counts as\n    current and raises no false alarm every sync cycle.'
    disk, run = _code_version(), info.get("code_version")
    if disk is None or run is None:
        return None                       
    if disk <= run + 1e-6:
        return True
    ran_fp = info.get("code_fingerprint")
    if ran_fp:
        fp = _code_fingerprint()
        if fp is not None and fp == ran_fp:
            return True                   
    return False


def _log_hint(log_path) -> str | None:
    "Where to read this daemon's output when `log_path` is None.\n\n    None when a file is being written (the path already says it). Otherwise the one\n    command that shows the live log on this supervisor: journald when a user unit\n    exists, launchd's log dir on macOS, the Entware init script's log on Synology.\n    Named per platform and never guessed, for the reason _throttle_hint gives."
    if log_path:
        return None
    home = Path.home()
    unit = home / ".config" / "systemd" / "user" / "agent-context-daemon.service"
    if unit.exists():
        return "journalctl --user -u agent-context-daemon.service -n 200 --no-pager"
    if sys.platform == "darwin":
        return f"ls -t '{home / 'Library' / 'Logs' / 'agent-context'}'/*.log | head -1"
    init = Path("/opt/etc/init.d/S51agent-context")
    if init.exists():
        return f"sudo {init} status; ls -t ~/.local/state/agent-context/*.log"
    return "stdout is not a file; check the supervisor that started this daemon"


def get_health() -> dict:
    'Structured daemon liveness snapshot (in-process heartbeat, falling back to\n    persisted daemon.info).'
    info = _read_daemon_info() or {}
    now = time.time()
    threshold = _sync_stall_secs()
    last_success = _LAST_SUCCESSFUL_SYNC if _LAST_SUCCESSFUL_SYNC is not None \
        else info.get("last_successful_sync")
    since = (now - last_success) if last_success is not None else None
    last_attempt = _LAST_SYNC_ATTEMPT if _LAST_SYNC_ATTEMPT is not None \
        else info.get("last_sync_attempt")
    since_attempt = (now - last_attempt) if last_attempt is not None else None
    wedge = _working_tree_wedge()

    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    div = _LAST_DIVERGENCE if _LAST_DIVERGENCE is not None else info.get("divergence")
    behind_now = None if div is None else {
        r: d["behind"] for r, d in div.items() if isinstance(d, dict) and d.get("behind")}
    current = code_currency(info)
    started_at = info.get("started_at")
    age = (now - started_at) if started_at else None
    
    
    
    
    
    
    cycle_start = _CYCLE_STARTED_AT if _heartbeat_known() else info.get("cycle_started_at")
    in_flight = (now - cycle_start) if cycle_start is not None else None
    suspended = slept_since_attempt_secs()
    awake_in_flight = None if in_flight is None else max(0.0, in_flight - suspended)
    flying = bool(awake_in_flight is not None and awake_in_flight <= threshold)
    
    
    retry_delay = _NEXT_CYCLE_DELAY
    alive_window = max(threshold, (retry_delay or 0.0) + 2 * _POKE_SLICE_SECS)
    if since_attempt is None:
        starting = bool(age is not None and age <= threshold)
        loop_alive = starting or flying
    else:
        starting = False
        loop_alive = max(0.0, since_attempt - suspended) <= alive_window or flying
    
    
    
    
    
    
    
    
    
    
    
    never_synced = bool(last_success is None and not starting and loop_alive
                        and age is not None and age > threshold)
    
    
    
    behind_all = bool(behind_now) and all(n > 0 for n in behind_now.values())
    
    
    restarts = starts_last_hour(now)
    _write_restart_health(restarts)
    verdict = ("restart-loop" if restarts > RESTART_LOOP_STARTS
               else "starting" if starting
               else "loop-dead" if not loop_alive
               else "integration-failing"
               if (bool(since is not None and since > threshold) or bool(wedge)
                   or never_synced or behind_all)
               else "stale-code" if current is False
               else "healthy")
    
    
    
    loop_dead_reason = None
    if verdict == "loop-dead":
        loop_dead_reason = (f"no completed sync cycle for "
                            f"{int(since_attempt if since_attempt is not None else (age or 0))}s")
        if in_flight is not None:
            loop_dead_reason += f" (current cycle started {int(in_flight)}s ago)"
        if suspended > _SLEEP_EPSILON:
            loop_dead_reason += f", {int(suspended)}s of it suspended"
    return {
        "pid": info.get("pid") or os.getpid(),
        "starts_last_hour": restarts,
        "code_version": info.get("code_version"),
        "started_at": info.get("started_at"),
        "last_successful_sync": last_success,
        "last_sync_attempt": last_attempt,
        "last_sync_error": (_LAST_SYNC_ERROR if _LAST_SYNC_ERROR is not None
                            else info.get("last_sync_error")) or (
            f"working tree wedged: {wedge}" if wedge else loop_dead_reason),
        
        
        
        "cycle_in_flight_secs": in_flight,
        "suspended_secs": suspended,
        
        "retry_delay_secs": retry_delay,
        
        
        
        "last_sync_error_at": (_LAST_SYNC_ERROR_AT if _LAST_SYNC_ERROR_AT is not None
                               else info.get("last_sync_error_at")),
        "sync_stall_secs": threshold,
        "seconds_since_sync": since,
        "seconds_since_attempt": since_attempt,
        "loop_alive": loop_alive,
        
        
        
        
        
        "behind": behind_now,
        
        
        
        "log_path": info.get("log_path"),
        
        "server_commit": info.get("server_commit"),
        
        
        
        "log_hint": _log_hint(info.get("log_path")),
        
        
        
        
        
        
        
        
        "code_current": current,
        
        
        "code_defer_reason": code_defer_reason(),
        
        
        
        
        
        
        
        
        "verdict": verdict,
        "stalled": (bool(since is not None and since > threshold) or bool(wedge)
                    or never_synced),
        
        
        
        "working_tree_wedged": wedge or None,
    }


def _store_root(root=None):
    return root or os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")


def _live_git_secs(root):
    'Elapsed seconds of the longest-running `git` process on this machine, or 0.\n\n    A lock file only means something while the git that took it is alive. Nothing\n    records which process that was, so the test is: is there ANY git process that has\n    been running at least as long as the lock has existed? Portable `ps` output,\n    parsed loosely; any failure reads as 0 (no live git), which the caller pairs with\n    the lock-age threshold so a momentary miss cannot delete a lock a live git holds.'
    try:
        out = subprocess.run(["ps", "-axo", "etime=,command="], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return 0
    longest = 0
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        et, cmd = parts
        exe = cmd.split()[0] if cmd.split() else ""
        if not (exe == "git" or exe.endswith("/git")):
            continue
        
        days, _, rest = et.rpartition("-")
        fields = [int(x) for x in rest.split(":") if x.isdigit()]
        while len(fields) < 3:
            fields.insert(0, 0)
        secs = (int(days) * 86400 if days else 0) + fields[0] * 3600 + fields[1] * 60 + fields[2]
        longest = max(longest, secs)
    return longest


def _lock_files(g):
    "Every git lock file a dead process can strand under `.git`.\n\n    The three top-level ones, plus `refs/**/*.lock`. A stranded\n    `refs/remotes/<r>/<branch>.lock` blocks no commit, so _working_tree_wedge's\n    commit-oriented checks miss it, but it makes `git fetch --all` exit non-zero\n    forever and freezes that remote-tracking ref at whatever it held when the lock\n    appeared. sync() then rebases and picks its base\n    against a stale view of the fleet, which both hides a real divergence and\n    guarantees the diverged-remotes fallback. Unlinking one is as lossless as\n    unlinking index.lock: a lock file holds no content, only the claim to write."
    names = [os.path.join(g, n) for n in ("index.lock", "HEAD.lock", "packed-refs.lock")]
    for dirpath, _dirs, files in os.walk(os.path.join(g, "refs")):
        names.extend(os.path.join(dirpath, f) for f in files if f.endswith(".lock"))
    return [p for p in names if os.path.exists(p)]


def heal_working_tree(root=None, lock_age_secs=600):
    "Recover the unambiguous wedge states in the store's working tree; return the\n    actions taken (empty when there was nothing to do).\n\n    Detection alone would let a single empty cherry-pick or a 0-byte index.lock stall\n    every sync, and because get_health reads the live tree, that same lock would fail\n    the test suite and with it the self-deploy gate. Each case here is lossless:\n\n    - stale lock file (index/HEAD/packed-refs, or any `refs/**/*.lock` — see\n      _lock_files): older than the threshold and no git process has been alive that\n      long → nothing can be holding it → unlink.\n    - unfinished cherry-pick with a clean tree (no conflict markers, nothing staged):\n      git stopped on an empty pick (the change already landed via another mirror\n      leg) → `cherry-pick --quit` keeps HEAD and drops only the pending state.\n    - interrupted rebase with no live git: the daemon was killed mid-replay. sync()\n      already aborts a failed rebase; this finishes that job for one it never got\n      to. `rebase --abort` restores the pre-rebase branch — every commit still\n      exists on it.\n\n    A merge in progress, or a cherry-pick with a dirty tree, is left alone: those\n    need a human. The caller logs what remains via _working_tree_wedge."
    root = _store_root(root)
    g = os.path.join(root, ".git")
    if not os.path.isdir(g):
        return []
    actions = []

    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True, timeout=30)

    live = None  
    for p in _lock_files(g):
        try:
            age = time.time() - os.path.getmtime(p)
        except OSError:
            continue
        if age <= lock_age_secs:
            continue
        if live is None:
            live = _live_git_secs(root)
        if live >= age:
            continue  
        with contextlib.suppress(OSError):
            os.unlink(p)
            actions.append(f"removed stale {os.path.relpath(p, g)} "
                           f"({int(age)}s old, no live git)")

    if os.path.exists(os.path.join(g, "CHERRY_PICK_HEAD")):
        st = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
        if not st and git("cherry-pick", "--quit").returncode == 0:
            actions.append("quit empty cherry-pick (clean tree)")

    if any(os.path.isdir(os.path.join(g, d)) for d in ("rebase-merge", "rebase-apply")):
        if live is None:
            live = _live_git_secs(root)
        if live == 0 and git("rebase", "--abort").returncode == 0:
            actions.append("aborted interrupted rebase (no live git; branch restored)")
        elif live == 0:
            
            
            
            
            
            husk = os.path.join(g, "rebase-merge")
            stash = os.path.join(husk, "autostash")
            if os.path.isdir(husk) and not os.path.exists(os.path.join(husk, "head-name")):
                sha = ""
                with contextlib.suppress(OSError):
                    sha = open(stash).read().strip()
                import shutil
                shutil.rmtree(husk, ignore_errors=True)
                actions.append("removed rebase husk (finished autostash rebase)"
                               + (f"; its autostash is dangling commit {sha} — "
                                  f"`git stash apply {sha}` recovers it" if sha else ""))

    for a in actions:
        log.warning("agent-context: working-tree self-heal — %s", a)
    return actions


def _working_tree_wedge(root=None, lock_age_secs=600):
    "Describe an interrupted git state in the store's working tree, or ''.\n\n    Detects what blocks commits while leaving fetch working: an abandoned\n    rebase/merge, and locks left behind by a killed git — plus the inverse, a\n    stranded ref lock, which blocks fetch while leaving commits working.\n    heal_working_tree unlinks a stale lock before this runs, so anything still named\n    here is one a live git may own, which is the case that needs a human."
    root = _store_root(root)
    g = os.path.join(root, ".git")
    if not os.path.isdir(g):
        return ""
    found = []
    for d, label in (("rebase-merge", "interactive rebase in progress"),
                     ("rebase-apply", "rebase/am in progress")):
        if os.path.isdir(os.path.join(g, d)):
            found.append(label)
    for f, label in (("MERGE_HEAD", "unfinished merge"),
                     ("CHERRY_PICK_HEAD", "unfinished cherry-pick")):
        if os.path.exists(os.path.join(g, f)):
            found.append(label)
    for p in _lock_files(g):
        try:
            if (time.time() - os.path.getmtime(p)) > lock_age_secs:
                found.append(f"stale {os.path.relpath(p, g)} "
                             f"(> {lock_age_secs // 60} min old)")
        except OSError:
            pass
    return "; ".join(found)













_bounce_thread: threading.Thread | None = None
_bounce_thread_lock = threading.Lock()


def _bounce_join_secs() -> float:
    "How long a departing process waits for a bounce it started. run_gate's own\n    wall clock bounds the thread, so bound the wait by the same number plus room for\n    the bounce itself — a wedged gate then delays exit by that much and no more."
    return _gate_timeout_secs(_GATE_TIMEOUT_SECS) + 30.0


def _gated_bounce() -> None:
    'Gate the on-disk code and, only if it passes, bounce the daemon onto it.\n\n    Serialized machine-wide by a non-blocking flock: after a release every relay that\n    starts sees `_should_bounce()`, and without this they would each run the suite —\n    N concurrent pytest runs racing one bounce. A loser returns rather than queueing,\n    because by the time it acquired the lock the winner would already have done the\n    work; the re-check under the lock is what makes that safe.'
    try:
        lock = open(_state_dir() / "bounce.lock", "w")
    except OSError:
        return
    with lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except OSError:
            return              
        if not _port_open() or not _should_bounce():
            return              
        self_deploys = _self_deploy_enabled()
        disk = _code_version()
        fp = _code_fingerprint()
        if fp is None and disk is None:
            return                  
        key = str(fp if fp is not None else disk)
        age = time.time() - disk if disk is not None else float("inf")
        if gate_lock.bridge_should_defer(daemon_self_deploys=self_deploys, code_age_secs=age):
            log.info("agent-context: gate_defer role=bridge pid=%d fp=%s — the daemon deploys "
                     "itself; leaving fresh code to it for one sync cycle", os.getpid(), key[:8])
            return
        outcome = _shared_gate(key, gate_lock.ROLE_BRIDGE, _gate_timeout_secs(_GATE_TIMEOUT_SECS))
        if not outcome.ok:
            if outcome.ok is False and outcome.source == "ran":
                log.warning("agent-context: on-disk code is newer but the verification gate "
                            "did not pass (%s) — not bouncing; keeping the running daemon",
                            outcome.detail)
                _notify(f"agent-context: skipped daemon bounce, gate failed: {outcome.detail}")
            return
        if not gate_lock.bridge_may_restart(daemon_self_deploys=self_deploys, code_age_secs=age):
            log.info("agent-context: gate passed for fp=%s; the daemon's own self-deploy "
                     "adopts it, no restart requested by pid=%d", key[:8], os.getpid())
            return
        if not gate_lock.claim_bounce(_state_dir(), key):
            return                  
        log.info("agent-context: on-disk code newer and gate passed — bouncing the daemon "
                 "(requested_by pid=%d fp=%s)", os.getpid(), key[:8])
        _bounce_daemon()
        
        
        
        
        
        if _supervisor() is None:
            with contextlib.suppress(Exception):
                ensure_daemon()


def _start_background_bounce() -> None:
    "Run `_gated_bounce()` off the caller's latency path, at most one per process."
    global _bounce_thread
    with _bounce_thread_lock:
        if _bounce_thread is not None and _bounce_thread.is_alive():
            return
        _bounce_thread = threading.Thread(
            target=_gated_bounce, name="agent-context-bounce", daemon=True)
        _bounce_thread.start()


@atexit.register
def _join_bounce_thread() -> None:
    'Give a short-lived caller time to finish the bounce it started.\n\n    The Synology maintenance cron calls `ensure_daemon()` and exits immediately; a\n    bare daemon thread would be killed mid-gate there, orphaning pytest and leaving\n    the node on old code. The thread stays a daemon thread so a wedged gate can never\n    pin the interpreter outright, and this bounded join is what a normal (~10 s) gate\n    actually needs. A long-lived relay has finished it long before exit.'
    t = _bounce_thread
    if t is not None and t.is_alive():
        t.join(_bounce_join_secs())


def ensure_daemon() -> str:
    'Ensure the shared HTTP daemon is up; start it detached if not. Returns its URL.\n\n    A remote daemon (AGENT_CONTEXT_HOST not loopback) is owned by its own machine: there is\n    nothing to probe, supervise, bounce or spawn here. Only the bearer token is checked, so\n    a missing one fails now with a clear message rather than as a 401 on every request.'
    if is_remote():
        problem = _remote_host_problem(_host())
        if problem:
            raise RuntimeError(f"AGENT_CONTEXT_HOST={_host()!r} {problem}")
        if not os.environ.get("AGENT_CONTEXT_TOKEN"):
            raise RuntimeError(
                f"AGENT_CONTEXT_TOKEN is not set; it is required to reach the remote "
                f"daemon at {mcp_url()}")
        return mcp_url()
    if _port_open():
        
        
        if _should_bounce():
            _start_background_bounce()
        return URL
    if _supervisor() is not None:
        
        
        if _wait_port(True, _SUPERVISED_WAIT_SECS):
            return URL
        log.warning("agent-context: supervised daemon did not come up within %ss — "
                    "falling back to a lazy spawn", _SUPERVISED_WAIT_SECS)
    with open(_state_dir() / "daemon.lock", "w") as lock:
        flock(lock, LOCK_EX)  
        if _port_open():  
            return URL
        env = dict(
            os.environ,
            AGENT_CONTEXT_TRANSPORT="http",
            AGENT_CONTEXT_HOST=HOST,
            AGENT_CONTEXT_PORT=str(PORT),
        )
        logf = open(_log_dir() / "daemon.log", "a")
        subprocess.Popen(
            [sys.executable, "-m", "agent_context.server"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,  
        )
        for _ in range(150):  
            if _port_open():
                return URL
            time.sleep(0.1)
        raise RuntimeError(f"agent-context daemon did not come up at {URL}")


class _DaemonGone(Exception):
    'Daemon-side transport failure or terminated session — reconnect + replay.'


CONTENT_CHANGED_METHOD = "notifications/agent-context/content_changed"


class _ContentNotifier(Protocol):
    def notify(self, paths: list[str]) -> None: ...


def content_changed_paths(item: object) -> list[str] | None:
    'The store paths a `content_changed` push carries, `[]` when malformed, and None\n    for any other message.'
    root = _msg_root(item)
    if getattr(root, "method", None) != CONTENT_CHANGED_METHOD:
        return None
    params = getattr(root, "params", None)
    paths = params.get("paths") if isinstance(params, dict) else None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return []
    return paths


def intercept_content_changed(item: object, refresher: _ContentNotifier) -> bool:
    'True when the push was consumed here. Only a remote relay materializes files; a\n    local one has nothing to refresh and forwards the message like any other.'
    if not is_remote():
        return False
    paths = content_changed_paths(item)
    if paths is None:
        return False
    refresher.notify(paths)
    return True


def _msg_root(item):
    m = getattr(item, "message", None)
    return getattr(m, "root", None)







_UNSUPPORTED_METHODS = frozenset({"server/discover"})
METHOD_NOT_FOUND = -32601
INVALID_REQUEST = -32600


def _jsonrpc_reply(rid, *, result: dict | None = None, code: int = 0,
                   message: str = "") -> SessionMessage:
    "A response to request `rid`, as the client's stdio stream carries it: `result` when\n    given, else an error with `code` and `message`."
    from mcp.shared.message import SessionMessage
    from mcp.types import ErrorData, JSONRPCError, JSONRPCMessage, JSONRPCResponse
    if result is not None:
        return SessionMessage(JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=rid, result=result)))
    return SessionMessage(JSONRPCMessage(JSONRPCError(
        jsonrpc="2.0", id=rid, error=ErrorData(code=code, message=message))))


def _answer_locally(root, pre_init: bool) -> tuple[bool, SessionMessage | None]:
    '(handled, reply) for a client message the daemon must not see: a method no daemon\n    implements, or anything but `initialize` before the handshake (a session-less POST that is\n    not `initialize` gets HTTP 400). A request gets a reply, a notification is dropped\n    (reply None). Not handled: (False, None).'
    method = getattr(root, "method", None)
    if method is None or (not pre_init and method not in _UNSUPPORTED_METHODS):
        return False, None
    if method == "initialize":
        return False, None
    rid = getattr(root, "id", None)
    if rid is None:
        return True, None
    if method == "ping":
        return True, _jsonrpc_reply(rid, result={})
    return True, _jsonrpc_reply(rid, code=METHOD_NOT_FOUND, message=f"Method not found: {method}")





_REJECTING_STATUSES = frozenset({400, 405, 406, 413, 415, 422})


def _rejected_request(exc: BaseException) -> tuple[object, str, int] | None:
    "(request id, method, HTTP status) when `exc` carries the daemon's refusal of one request:\n    an httpx.HTTPStatusError in `_REJECTING_STATUSES`, found through exception groups, a\n    `_DaemonGone` wrapper and causes. None otherwise."
    seen: set[int] = set()
    todo: list[object] = [exc]
    while todo:
        e = todo.pop()
        if not isinstance(e, BaseException) or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, BaseExceptionGroup):
            todo.extend(e.exceptions)
        todo.extend(a for a in e.args if isinstance(a, BaseException))
        todo.extend(x for x in (e.__cause__, e.__context__) if x is not None)
        if not isinstance(e, httpx.HTTPStatusError):
            continue
        status = e.response.status_code
        if status not in _REJECTING_STATUSES:
            continue
        try:
            body = json.loads(e.request.content or b"null")
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(body, dict) and body.get("id") is not None and body.get("method"):
            return body["id"], str(body["method"]), status
    return None


def _with_evidence(item):
    "`item`, or a copy of it with this machine's project evidence in `params._meta`,\n    when it is a `tools/call` whose arguments hold a string `cwd`. Reads local disk\n    (marker file, git remotes), so callers keep it off the event loop."
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    from . import identity
    r = _msg_root(item)
    if not isinstance(r, JSONRPCRequest) or r.method != "tools/call" or not r.params:
        return item
    try:
        params = identity.add_project_evidence(r.params)
    except Exception:     
        log.warning("agent-context bridge: no project evidence for this call", exc_info=True)
        return item
    if params == r.params:
        return item
    return SessionMessage(JSONRPCMessage(r.model_copy(update={"params": params})),
                          metadata=getattr(item, "metadata", None))



_BODY_FILE_MAX_BYTES = 8 * 1024 * 1024


def _with_body_file(item):
    "`item`, or a copy of it with `arguments.body_path` read off this machine's disk\n    and sent as `arguments.body`."
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    r = _msg_root(item)
    if not isinstance(r, JSONRPCRequest) or r.method != "tools/call" or not r.params:
        return item
    args = r.params.get("arguments")
    if not isinstance(args, dict):
        return item
    path = args.get("body_path")
    if not isinstance(path, str) or not path or args.get("body") is not None:
        return item
    full = os.path.expanduser(path)
    try:
        if not os.path.isabs(full) or os.path.getsize(full) > _BODY_FILE_MAX_BYTES:
            return item
        with open(full, encoding="utf-8") as fh:
            body = fh.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return item
    sent = {k: v for k, v in args.items() if k != "body_path"}
    sent["body"] = body
    params = {**r.params, "arguments": sent}
    return SessionMessage(JSONRPCMessage(r.model_copy(update={"params": params})),
                          metadata=getattr(item, "metadata", None))




_PARENT_POLL_SECONDS = 5.0


async def _await_parent_exit(start_ppid: int) -> None:
    "Return once this process's parent is no longer [start_ppid].\n\n    Split out of the bridge's watchdog only so it can be tested: inside the task\n    group the check is a closure over a live stdio relay, which a unit test cannot\n    reach. Reparenting to init is what actually happens on macOS and Linux when a\n    parent dies, so comparing against the starting ppid, and not against 1, is the\n    check that holds however the process tree is arranged."
    while os.getppid() == start_ppid:
        await anyio.sleep(_PARENT_POLL_SECONDS)


def connect_headers() -> dict[str, str]:
    "Headers for one connection to the daemon: the bearer token, plus, on a remote\n    relay, this machine's identity (the daemon serves every machine, so it cannot\n    know which one is calling), plus what the bridge says of itself (peer_wake). Built\n    per connect, so a reconnect sends the current values."
    from . import identity  
    token = os.environ.get("AGENT_CONTEXT_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if is_remote():
        local = identity.local_headers()
        identity.publish_local_headers(local)
        headers.update(local)
    headers.update(peer_wake.connect_headers())
    return headers


_RESUME: dict | None = None


def set_resume(state: dict | None) -> None:
    'Hand the next bridge the session a previous process of this relay left (relay_swap).'
    global _RESUME
    _RESUME = state
    relay_stdio.PRELOAD = state.get("leftover", b"") if state else b""


def take_resume() -> dict | None:
    global _RESUME
    state, _RESUME = _RESUME, None
    return state


def _client_options(watch) -> dict:
    "Extra arguments for `streamablehttp_client`: the release watch's hooks, when the client\n    in use takes a client factory (a test's stand-in does not)."
    import inspect
    if watch is None:
        return {}
    try:
        takes = "httpx_client_factory" in inspect.signature(streamablehttp_client).parameters
    except (TypeError, ValueError):
        takes = False
    return {"httpx_client_factory": watch.client_factory()} if takes else {}


async def _bridge() -> None:
    'Transparent MCP relay: this process\'s stdio <-> the shared daemon\'s HTTP.\n\n    Reconnecting: when the daemon restarts (supervised restart, self-deploy,\n    crash), the old streamable-HTTP session dies and every request on it comes\n    back "Session terminated" (-32600), which would wedge the client for the rest\n    of its session. The bridge detects daemon-side failure, re-ensures the daemon, opens a fresh HTTP session, silently\n    replays the MCP handshake (initialize + notifications/initialized) plus any\n    in-flight requests, and carries on — the client never sees the restart.'
    st = {
        "init_msg": None,        
        "init_id": None,
        "init_note": None,       
        "init_answered": False,  
        "pending": {},           
        "client_eof": False,
        "stdin_handed_over": False,  
    }
    resume = take_resume()
    if resume is not None and resume.get("init_msg") is not None:
        
        
        st["init_msg"], st["init_note"] = resume["init_msg"], resume.get("init_note")
        st["init_id"] = getattr(_msg_root(resume["init_msg"]), "id", None)
        st["init_answered"] = True

    def _settle(rid) -> None:
        'Request `rid` has its answer: stop replaying it.'
        st["pending"].pop(rid, None)

    remote = is_remote()
    
    
    refresher = None
    watch = None
    if remote:
        from . import relay_materialize  
        refresher = relay_materialize.ContentRefresher()
        watch = relay_swap.ReleaseWatch(relay_swap.own_release())
        missed = relay_swap.failed_release(resume, watch.own)
        if missed:
            watch.failed.add(missed)
            log.warning("agent-context: relay resumed on its own release; the exec into %s "
                        "failed, and this process will not try it again", missed)
    
    
    
    streams = watch if watch is not None else relay_swap.ReleaseWatch("")
    client_options = _client_options(streams)

    
    async with stdio_server() as (c_read, c_write), anyio.create_task_group() as refresher_tg:
        if refresher is not None:
            refresher_tg.start_soon(refresher.run)
        if watch is not None:
            refresher_tg.start_soon(relay_swap.swap_when_released, watch, st)
        attempts = 0
        
        own: dict[str, dict] = {}
        
        turn = peer_wake.TurnWatch()
        while not st["client_eof"]:
            try:
                streams.reset_stream()
                async with streamablehttp_client(
                        mcp_url(), headers=connect_headers() or None,
                        **client_options) as (d_read, d_write, _get_sid):
                    
                    
                    
                    
                    if st["init_msg"] is not None:
                        await d_write.send(st["init_msg"])
                        async for item in d_read:
                            if isinstance(item, Exception):
                                raise _DaemonGone(item)
                            r = _msg_root(item)
                            if (getattr(r, "id", None) == st["init_id"]
                                    and not hasattr(r, "method")):
                                if not st["init_answered"]:
                                    st["init_answered"] = True
                                    _settle(st["init_id"])
                                    await c_write.send(item)
                                break
                            await c_write.send(item)
                        if st["init_note"] is not None:
                            await d_write.send(st["init_note"])
                        for m in list(st["pending"].values()):
                            await d_write.send(m)
                        log.warning("agent-context bridge: reconnected to daemon, "
                                    "replayed %d in-flight request(s)", len(st["pending"]))
                        if refresher is not None:
                            refresher.request()  
                    attempts = 0

                    
                    
                    for lost in list(own.values()):
                        if lost.get("text"):      
                            await to_thread.run_sync(peer_wake.hold, lost["text"])
                    own.clear()
                    turn.forget()

                    async with anyio.create_task_group() as tg:

                        async def report_turn(state: str | None) -> None:
                            "Tell the daemon the session's turn started or ended. A report\n                            that cannot be sent is dropped: the reconnect reports afresh."
                            if state is None:
                                return
                            rid = peer_wake.report_id()
                            own[rid] = {}
                            try:
                                await d_write.send(peer_wake.report_request(
                                    rid, peer_wake.turn(state)))
                            except Exception:
                                own.pop(rid, None)

                        async def watch_turn() -> None:
                            while True:
                                await report_turn(await to_thread.run_sync(turn.poll))
                                await anyio.sleep(peer_wake.TURN_POLL_SECONDS)

                        async def wake_or_report(peer: dict) -> None:
                            'Wake the session; when that fails, report it to the daemon, which\n                            queues the message and tells the sender. A report that cannot be\n                            sent, or that the daemon refuses, leaves the message held here.'
                            if not peer.get("text"):
                                return
                            if await to_thread.run_sync(peer_wake.attempt, peer) is not None:
                                await report_turn(turn.woke())
                                return
                            rid = peer_wake.report_id()
                            own[rid] = peer
                            try:
                                await d_write.send(peer_wake.report_request(
                                    rid, peer_wake.undelivered(peer)))
                            except Exception:   
                                log.warning("agent-context bridge: could not report peer "
                                            "message %s as undelivered", peer.get("id"))

                        async def client_to_daemon() -> None:
                            async for item in c_read:
                                if isinstance(item, Exception):
                                    log.warning("agent-context bridge (client): %r", item)
                                    continue
                                if remote:   
                                    item = await to_thread.run_sync(_with_evidence, item)
                                    item = await to_thread.run_sync(_with_body_file, item)
                                r = _msg_root(item)
                                handled, reply = _answer_locally(r, st["init_msg"] is None)
                                if handled:
                                    if reply is not None:
                                        await c_write.send(reply)
                                    continue
                                method = getattr(r, "method", None)
                                rid = getattr(r, "id", None)
                                if method == "initialize":
                                    st["init_msg"], st["init_id"] = item, rid
                                elif method == "notifications/initialized":
                                    st["init_note"] = item
                                elif method is not None and rid is not None:
                                    st["pending"][rid] = item
                                await d_write.send(item)
                            pipe = relay_stdio.ACTIVE
                            if pipe is not None and pipe.frozen:
                                
                                
                                st["stdin_handed_over"] = True
                                return
                            st["client_eof"] = True   
                            tg.cancel_scope.cancel()

                        async def daemon_to_client() -> None:
                            async for item in d_read:
                                if isinstance(item, Exception):
                                    raise _DaemonGone(item)
                                if refresher is not None and intercept_content_changed(
                                        item, refresher):
                                    continue
                                r = _msg_root(item)
                                peer = peer_wake.peer_message(r)
                                if peer is not None and not peer_wake.forwards():
                                    
                                    tg.start_soon(wake_or_report, peer)
                                    continue
                                waiting = peer_wake.peer_waiting(r)
                                if waiting is not None:
                                    tg.start_soon(to_thread.run_sync, peer_wake.note_waiting,
                                                  waiting)
                                    continue
                                if peer_wake.peer_watch(r):
                                    tg.start_soon(to_thread.run_sync, peer_wake.note_watch)
                                    continue
                                rid = getattr(r, "id", None)
                                if isinstance(rid, str) and rid in own:
                                    
                                    reported = own.pop(rid)
                                    if reported.get("text") and not peer_wake.report_accepted(r):
                                        tg.start_soon(to_thread.run_sync, peer_wake.hold,
                                                      reported["text"])
                                    continue
                                if rid is not None and not hasattr(r, "method"):
                                    err = getattr(r, "error", None)
                                    
                                    
                                    
                                    
                                    if (err is not None and rid in st["pending"]
                                            and (getattr(err, "code", None) in (-32600, 32600)
                                                 or "session terminated" in
                                                 str(getattr(err, "message", "")).lower())):
                                        
                                        
                                        raise _DaemonGone(getattr(err, "message", "session terminated"))
                                    if rid == st["init_id"]:
                                        if st["init_answered"]:
                                            continue   
                                        st["init_answered"] = True
                                    
                                    
                                    await c_write.send(item)
                                    _settle(rid)
                                    continue
                                await c_write.send(item)
                            raise _DaemonGone("daemon closed the stream")

                        async def watch_parent() -> None:
                            "Exit when the client process is gone.\n\n                            Stdin EOF is the bridge's natural exit. That works when a\n                            client shuts down politely and fails every other time: a\n                            SIGKILLed or crashed harness, and a `/mcp` reconnect (which\n                            starts a fresh bridge and abandons this one), leave the write\n                            end of the pipe open, so `async for item in c_read` above\n                            blocks forever. The process is then reparented to init and\n                            survives until reboot. Nothing notices: the daemon stays\n                            healthy and every tool call still works, so the leak is\n                            invisible from inside a session.\n\n                            A bridge serves one client, so that client being gone is\n                            sufficient reason to stop. Polling getppid is\n                            the portable check — reparenting is what actually happens on\n                            both macOS and Linux, and it needs no signal handler, no\n                            platform API and no bookkeeping the relay path could get\n                            wrong."
                            start_ppid = os.getppid()
                            await _await_parent_exit(start_ppid)
                            log.info("agent-context bridge: client process %d is gone "
                                     "— exiting", start_ppid)
                            st["client_eof"] = True   
                            tg.cancel_scope.cancel()

                        async def watch_stream() -> None:
                            "Reconnect as soon as the daemon's event stream drops. The mcp\n                            client re-opens it twice and then stops without a word, so a relay\n                            with nothing to send would never learn the daemon restarted: no\n                            `content_changed` pushes, no new release, until its next request."
                            await streams.stream_lost.wait()
                            raise _DaemonGone("the daemon's event stream dropped")

                        tg.start_soon(client_to_daemon)
                        tg.start_soon(daemon_to_client)
                        tg.start_soon(watch_parent)
                        tg.start_soon(watch_stream)
                        tg.start_soon(watch_turn)
            except BaseException as exc:  
                if isinstance(exc, anyio.get_cancelled_exc_class()):
                    raise
                if st["client_eof"]:
                    break
                rejected = _rejected_request(exc)
                if rejected is not None:
                    
                    rid, method, status = rejected
                    if rid == st["init_id"]:
                        st["init_msg"] = st["init_id"] = st["init_note"] = None
                    log.warning("agent-context bridge: daemon refused %s (HTTP %d); "
                                "answered the client with an error", method, status)
                    with contextlib.suppress(Exception):
                        await c_write.send(_jsonrpc_reply(
                            rid, code=INVALID_REQUEST,
                            message=f"agent-context daemon refused {method}: HTTP {status}"))
                    _settle(rid)  
                attempts += 1
                if attempts > 20:
                    raise RuntimeError(
                        "agent-context bridge: daemon unreachable after retries") from exc
                log.warning("agent-context bridge: daemon connection lost (%s: %r) — "
                            "reconnecting (attempt %d)", type(exc).__name__, exc, attempts)
                with contextlib.suppress(Exception):
                    ensure_daemon()
                await anyio.sleep(min(0.25 * attempts, 3.0))
        refresher_tg.cancel_scope.cancel()


def _degraded_instructions() -> str:
    return (f"agent-context is unavailable for this session: the ls daemon at {mcp_url()} "
            "was unreachable at start, so this relay has no upstream and no tools. Skills, "
            "hooks and docs were materialized from the last cached bundle. Start a new "
            "session once ls is back.")


async def _serve_degraded() -> None:
    "A stdio MCP server with no upstream and no store: it answers `initialize` with the\n    reason and lists no tools. It never touches a local store, because the ls daemon is\n    the store's only writer."
    from mcp.server.lowlevel import Server
    from mcp.types import Tool

    server = Server("agent-context", instructions=_degraded_instructions())

    @server.list_tools()
    async def _no_tools() -> list[Tool]:
        return []

    from mcp.server.stdio import stdio_server as mcp_stdio_server  
    from mcp.shared.message import SessionMessage
    async with mcp_stdio_server() as (c_read, c_write), anyio.create_task_group() as tg:
        s_send, s_read = anyio.create_memory_object_stream[SessionMessage | Exception](16)

        async def screen() -> None:
            "Answer a method no mcp release implements (`server/discover`) here, as the\n            bridge does: the SDK's session answers it -32602 and logs a 31-error validation\n            warning into the client's MCP log on every start."
            async with s_send:
                async for item in c_read:
                    root = _msg_root(item)
                    if getattr(root, "method", None) in _UNSUPPORTED_METHODS:
                        _, reply = _answer_locally(root, pre_init=False)
                        if reply is not None:
                            await c_write.send(reply)
                        continue
                    await s_send.send(item)

        async def watch_parent() -> None:
            start_ppid = os.getppid()
            await _await_parent_exit(start_ppid)
            log.info("agent-context degraded relay: client process %d is gone, exiting",
                     start_ppid)
            tg.cancel_scope.cancel()

        tg.start_soon(screen)
        tg.start_soon(watch_parent)
        await server.run(s_read, c_write, server.create_initialization_options())
        tg.cancel_scope.cancel()


def run_degraded_relay() -> None:
    "Serve the degraded relay on this process's stdio until the client closes (blocking)."
    anyio.run(_serve_degraded)


def run_bridge() -> None:
    "Bridge this process's stdio MCP stream to the shared daemon (blocking)."
    anyio.run(_bridge)
