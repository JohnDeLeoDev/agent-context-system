'Agent work metrics: one append-only, hash-chained event log and the library that writes it.\n\nStandard library only, and it imports nothing from this package. The gate scripts\n(store-precommit-gate.py, store-wt-finish.py) load this file by path under whatever python\nruns them, so it also stays runnable on python 3.8 (no `match`, no `X | Y` outside\nannotations, no `removesuffix`).\n\nLog: `<metrics dir>/YYYY-MM.jsonl`, one JSON object per line, keys sorted. `prev` is the\nsha256 (hex) of the previous line\'s bytes without the newline, `"0" * 64` for the first\nline of a file. `<YYYY-MM>.head` holds the hash of the newest line so an edit to the last\nline, or a truncated tail, is detected by `verify`. The directory is\n`$AGENT_CONTEXT_METRICS_DIR`, else `$XDG_STATE_HOME/agent-context/metrics`, else\n`~/.local/state/agent-context/metrics`. It is outside the git-synced store.\n\nPrivacy: a line holds only whitelisted keys with short values in a restricted character\nset. No prompt text, command line, file content, message body or path can be written.\n\n`emit` never raises. It waits about 200 ms for a stuck lock holder and up to 3 s while a\nburst of writers keeps appending. A failure means the event is lost, not that the caller\nchanges behavior.\n\nNative Windows records nothing: the log needs fcntl, pread and O_NOFOLLOW, none of which it\nhas, and `emit` returns False there at once. The import is guarded so the relay, which imports\nthis module through `server`, still starts on Windows.'
from __future__ import annotations

try:
    import fcntl
except ImportError:  
    fcntl = None
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ZERO = "0" * 64
LOCK_WAIT_SECS = 0.2
LOCK_CAP_SECS = 3.0
MAX_LINE_BYTES = 512
SESSION_LEN = 8

TYPES = frozenset({
    "phase", "landed", "gate_start", "gate_end", "gate_cooloff_start", "gate_cooloff_end",
    "adopt", "tokens", "approval_asked", "approval_answered", "msg", "block",
})
PHASES = (
    "requested", "criteria_sent", "criteria_approved", "tests_written", "tests_locked",
    "impl_done", "committed", "review_start", "review_end", "landing_asked",
    "landing_approved", "landed",
)
INT_FIELDS = frozenset({"dur_ms", "n", "failed", "skipped", "reruns",
                        "in_tok", "out_tok", "cache_r", "cache_w"})
STR_FIELDS = frozenset({"session", "change", "ref", "src", "id"})
FIELDS = INT_FIELDS | STR_FIELDS | {"ok", "cost_usd"}
SOURCES = frozenset({"baseline", "backfill"})

_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_SESSION = re.compile(r"^[A-Za-z0-9-]{1,36}$")
_CHANGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REF = re.compile(r"^[A-Za-z0-9._:@+=-]{1,120}$")
_ID = re.compile(r"^[A-Za-z0-9._:@+=-]{1,64}$")
_MAX_INT = 2 ** 53


_RUN = re.compile(r"[A-Za-z0-9]{20,}")


class VerifyResult:
    'Outcome of `verify`. A plain class: the gate scripts load this file by path, where\n    the `dataclass` decorator needs the module registered in `sys.modules`.'

    def __init__(self, ok: bool = True, lines: int = 0, bad_file: str | None = None,
                 bad_line: int | None = None, reason: str | None = None) -> None:
        self.ok = ok
        self.lines = lines
        self.bad_file = bad_file
        self.bad_line = bad_line
        self.reason = reason


def metrics_dir() -> Path:
    override = os.environ.get("AGENT_CONTEXT_METRICS_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "agent-context" / "metrics"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _valid_ts(value: object) -> bool:
    if not isinstance(value, str) or not _TS.match(value):
        return False
    try:
        time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _clean(event_type: object, ts: object, fields: dict[str, object]) -> dict[str, object] | None:
    'The validated row without `prev`, or None when anything is off the whitelist.'
    if not isinstance(event_type, str) or event_type not in TYPES:
        return None
    row: dict[str, object] = {"type": event_type}
    if ts is None:
        ts = now_iso()
    if not _valid_ts(ts):
        return None
    row["ts"] = ts
    if "session" not in fields:
        env_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if env_session and _SESSION.match(env_session):
            fields = dict(fields, session=env_session)
    for key, value in fields.items():
        if key not in FIELDS:
            return None
        if value is None:
            continue
        if key == "session":
            if not isinstance(value, str) or not _SESSION.match(value):
                return None
            row[key] = value[:SESSION_LEN]
        elif key == "change":
            if not isinstance(value, str) or not _CHANGE.match(value) or _RUN.search(value):
                return None
            row[key] = value
        elif key == "ref":
            if not isinstance(value, str) or not _REF.match(value) or _RUN.search(value):
                return None
            row[key] = value
        elif key == "id":
            if not isinstance(value, str) or not _ID.match(value) or _RUN.search(value):
                return None
            row[key] = value
        elif key == "src":
            if not isinstance(value, str) or value not in SOURCES:
                return None
            row[key] = value
        elif key in INT_FIELDS:
            if type(value) is not int or value < 0 or value > _MAX_INT:
                return None
            row[key] = value
        elif key == "ok":
            if type(value) is not bool:
                return None
            row[key] = value
        elif key == "cost_usd":
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or value < 0 or value != value or value > 1e9):
                return None
            row[key] = round(float(value), 6)
    return row


def _counter_path(directory: Path) -> Path:
    return directory / "rejected.count"


def rejected_count(directory: Path | None = None) -> int:
    try:
        text = _counter_path(directory or metrics_dir()).read_text(encoding="utf-8").strip()
        return int(text) if text else 0
    except (OSError, ValueError):
        return 0


def _count_rejected(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_counter_path(directory)), os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
            count = int(raw) if raw.isdigit() else 0
            os.ftruncate(fd, 0)
            os.pwrite(fd, str(count + 1).encode("ascii"), 0)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass


def _lock(fd: int) -> bool:
    'Take the file lock. Gives up after LOCK_WAIT_SECS with no progress, so a stuck\n    holder costs a caller about 200 ms. While other writers keep appending (the file keeps\n    growing) the wait restarts, up to LOCK_CAP_SECS in all: a burst of writers is not a\n    stuck holder.'
    now = time.monotonic()
    deadline, cap = now + LOCK_WAIT_SECS, now + LOCK_CAP_SECS
    size = os.fstat(fd).st_size
    pause = 0.0005
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            now = time.monotonic()
            grown = os.fstat(fd).st_size
            if grown != size:
                size, deadline = grown, now + LOCK_WAIT_SECS
            if now >= deadline or now >= cap:
                return False
            time.sleep(pause)
            pause = min(pause * 2, 0.004)


def _tail(fd: int) -> bytes:
    size = os.fstat(fd).st_size
    if size == 0:
        return b""
    take = min(size, 4096)
    return os.pread(fd, take, size - take)


def _append(directory: Path, row: dict[str, object]) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    month = str(row["ts"])[:7]
    path = directory / (month + ".jsonl")
    
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o640)
    try:
        if not _lock(fd):
            _count_rejected(directory)
            return False
        if (os.fstat(fd).st_mode & 0o777) != 0o640:
            os.fchmod(fd, 0o640)
        tail = _tail(fd)
        last = tail.rstrip(b"\n").rsplit(b"\n", 1)[-1] if tail else b""
        row = dict(row)
        row["prev"] = hashlib.sha256(last).hexdigest() if last else ZERO
        line = json.dumps(row, sort_keys=True, separators=(",", ":"))
        data = (line + "\n").encode("utf-8")
        if len(data) > MAX_LINE_BYTES:
            _count_rejected(directory)
            return False
        if tail and not tail.endswith(b"\n"):
            data = b"\n" + data      
        if os.write(fd, data) != len(data):
            return False
        head = os.open(str(directory / (month + ".head")),
                       os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
        try:
            os.write(head, hashlib.sha256(line.encode("utf-8")).hexdigest().encode("ascii"))
        finally:
            os.close(head)
        return True
    finally:
        os.close(fd)


def emit(event_type: str, *, ts: str | None = None, **fields: object) -> bool:
    'Append one event. True when it was written. Never raises.'
    if fcntl is None:
        return False
    try:
        directory = metrics_dir()
        row = _clean(event_type, ts, fields)
        if row is None:
            _count_rejected(directory)
            return False
        return _append(directory, row)
    except Exception:
        return False


def log_files(directory: Path | None = None) -> list[Path]:
    d = directory or metrics_dir()
    try:
        return sorted(p for p in d.iterdir() if re.fullmatch(r"\d{4}-\d\d\.jsonl", p.name))
    except OSError:
        return []


def read_events(directory: Path | None = None) -> list[dict[str, object]]:
    'Every event in every month file, oldest first by `ts` (file order breaks ties).'
    rows: list[dict[str, object]] = []
    for path in log_files(directory):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    rows.sort(key=lambda r: str(r.get("ts", "")))
    return rows


def verify(directory: Path | None = None) -> VerifyResult:
    "Walk every chain. Reports the first line whose `prev` does not match the line\n    before it, or whose file's newest line does not match its `.head` anchor."
    d = directory or metrics_dir()
    total = 0
    for path in log_files(d):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return VerifyResult(False, total, path.name, None, "unreadable")
        expected = ZERO
        for number, line in enumerate(lines, start=1):
            try:
                row = json.loads(line)
            except ValueError:
                return VerifyResult(False, total, path.name, number, "json")
            if not isinstance(row, dict) or row.get("prev") != expected:
                return VerifyResult(False, total, path.name, number, "chain")
            expected = hashlib.sha256(line.encode("utf-8")).hexdigest()
            total += 1
        head = path.with_suffix(".head")
        if lines and head.is_file():
            try:
                anchored = head.read_text(encoding="utf-8").strip()
            except OSError:
                anchored = ""
            if anchored != expected:
                return VerifyResult(False, total, path.name, len(lines), "tail")
    return VerifyResult(True, total)


if __name__ == "__main__":
    from agent_context import metrics_report

    sys.exit(metrics_report.main(sys.argv[1:]))
