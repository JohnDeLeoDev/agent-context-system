
"Per-machine, non-synced directories: the daemon log and the daemon state.\n\nThese differ on macOS. `~/Library/Logs` is subject to the OS's own log housekeeping, which\ncan remove the whole `agent-context` directory, including `daemon.info` (the self-deploy\nversion guard) and `usage.json` (the machine's telemetry window). Only the log may live\nsomewhere a cleaner is allowed to empty. On Linux the two paths coincide.\n\nStdlib only, and imports nothing else from this package: `usage.py` sits on the\nstore's hot read path and `daemon.py` is imported by every relay, so neither can\nafford an import cycle here."
import contextlib
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path


_LEGACY_LOG_STATE = ("daemon.info", "daemon.lock", "usage.json")


def _fsync_enabled() -> bool:
    "Whether `write_atomic` flushes to the platter. The only durability switch in\n    this server; see `write_atomic` for what it costs and why it exists. Default on;\n    off only for `AGENT_CONTEXT_FSYNC` in the falsy set, which in practice means the\n    test suite's conftest and nothing else.\n\n    Read per call rather than cached at import, so a test can monkeypatch the\n    environment and so a daemon that re-execs onto new code picks up the current\n    value instead of one frozen at first import."
    return os.environ.get("AGENT_CONTEXT_FSYNC", "1").strip().lower() not in (
        "0", "false", "no", "off")





_WRITE_CHECK: Callable[[str], None] | None = None


def set_write_check(check: Callable[[str], None] | None) -> None:
    global _WRITE_CHECK
    _WRITE_CHECK = check




_WRITE_NOTE: Callable[[str], None] | None = None


def set_write_note(note: Callable[[str], None] | None) -> None:
    global _WRITE_NOTE
    _WRITE_NOTE = note


def write_atomic(path, text: str | bytes, mode: int | None = None) -> None:
    'Write `text` to `path` leaving no window in which the file is truncated, and\n    no litter behind if the write dies.\n\n    The one atomic writer. It lives here, in the module that imports nothing from\n    this package, so that audit.py, fleet.py, usage.py and store.py can all share it\n    without an import cycle. Temp litter would be swept into the store by the\n    autocommit\'s `git add -A`, so the cleanup on failure matters as much as the\n    atomic replace. invariant-check refuses a hand-rolled `os.replace` anywhere else\n    in the server.\n\n    `open(path, "w")` empties the target before the first byte is written; a failure\n    in that window leaves a 0-byte file and the previous content is gone.\n    `os.replace` is atomic on POSIX and Windows, so a concurrent reader sees the\n    whole old file or the whole new one, never a partial.\n\n    Atomicity is os.replace; durability is the fsync, and only the second one is\n    negotiable: `_fsync_enabled()` is the single switch. It has to be switchable\n    because fsync costs whatever the slowest volume in the fleet charges, and on a\n    slow volume it can push the self-deploy gate past its timeout.'
    path = str(path)
    if _WRITE_CHECK is not None:
        _WRITE_CHECK(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".ac-tmp-", suffix=".part")
    try:
        binary = isinstance(text, bytes)
        with (os.fdopen(fd, "wb") if binary else os.fdopen(fd, "w", encoding="utf-8")) as fh:
            fh.write(text)
            fh.flush()
            if _fsync_enabled():
                os.fsync(fh.fileno())
        
        
        
        
        
        if mode is None:
            try:
                mode = os.stat(path).st_mode & 0o7777
            except FileNotFoundError:
                mode = 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        if _WRITE_NOTE is not None:
            _WRITE_NOTE(path)
    except BaseException:
        
        
        
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _legacy_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "agent-context"


def log_dir() -> Path:
    "Where the daemon's own log goes. Wipeable by design — put nothing here that\n    has to survive."
    if sys.platform == "darwin":
        d = _legacy_log_dir()
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        d = Path(base) / "agent-context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def health_dir() -> Path:
    'Where this machine\'s core-health verdicts go, for preflight-core-health to\n    read at SessionStart.\n\n    It lives here, beside state_dir() and log_dir(), because everything that writes\n    a machine-global path has to be patchable in one place, or the test suite writes\n    to the real machine. `os.path.expanduser("~")` inline is not patchable by the\n    conftest fixture.\n\n    The path is harness_paths.state_dir() plus `health`: ~/.local/state/agent-context on\n    every platform, not this module\'s state_dir(), which is the daemon\'s own. Health\n    verdicts belong to the store, not to one harness, so nothing here names ~/.claude.\n    Not created eagerly: each writer makes the directory it writes into.'
    return Path.home() / ".local" / "state" / "agent-context" / "health"


def state_dir() -> Path:
    'Where `daemon.info`, `daemon.lock` and `usage.json` go. Migrates any legacy\n    copy out of the log dir on first access; a no-op on Linux, where the two\n    directories are the same path.'
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "agent-context"
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        d = Path(base) / "agent-context"
    d.mkdir(parents=True, exist_ok=True)
    legacy = _legacy_log_dir()
    if legacy != d:
        for name in _LEGACY_LOG_STATE:
            src, dst = legacy / name, d / name
            if src.exists() and not dst.exists():
                with contextlib.suppress(OSError):
                    src.replace(dst)
    return d
