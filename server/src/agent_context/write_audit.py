"Append-only, hash-chained audit log of write attempts through the network daemon.\n\nOne JSON line per attempt, allowed or denied, in `<dir>/YYYY-MM.jsonl`. Every line carries\n`prev`, the sha256 of the previous line's exact bytes, so a deleted or edited line breaks\nthe chain at the next line. (The LAST line can be edited without a trace until another line\nfollows it; the file's `chattr +a` bit and the journal copy cover that.)\n\nThe file is made append-only with `sudo -n chattr +a` when it is created. That is best\neffort: a host with no passwordless sudo, or a filesystem without the attribute, logs one\nwarning and carries on, because refusing to audit is worse than auditing without the bit.\nAn agent running as this daemon's own user can still remove the bit with sudo, which is\nwhy the plan says so: this log defends against remote machines, not against ls itself.\n\nDirectory: `AGENT_CONTEXT_AUDIT_DIR`, else `<state dir>/audit-writes`.\n`python -m agent_context.write_audit verify <file>` checks a file's chain."
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import paths

log = logging.getLogger("agent_context.write_audit")

ZERO = "0" * 64
_TAIL_BYTES = 16384


def audit_dir() -> Path:
    override = os.environ.get("AGENT_CONTEXT_AUDIT_DIR")
    return Path(override) if override else Path(paths.state_dir()) / "audit-writes"


def protect_file(path) -> None:
    'Make `path` append-only. Never raises.'
    try:
        subprocess.run(["sudo", "-n", "chattr", "+a", str(path)], capture_output=True,
                       timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("write-audit: could not set the append-only bit on %s: %s", path, exc)


def _line_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _last_line(path: Path) -> bytes | None:
    'The last non-empty line, however long it is.'
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    window = _TAIL_BYTES
    with open(path, "rb") as handle:
        while True:
            handle.seek(max(0, size - window))
            chunk = handle.read().rstrip(b"\n \t\r")
            cut = chunk.rfind(b"\n")
            if cut != -1 or size <= window:
                return chunk[cut + 1:] or None
            window *= 4


class WriteAudit:
    def __init__(self, directory: Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._dir = directory
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, entry: dict) -> dict:
        'Append `entry` (plus `ts` and `prev`) and return the stored record.'
        moment = self._clock()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
        directory = self._dir or audit_dir()
        with self._lock:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (time.strftime("%Y-%m", time.gmtime(moment)) + ".jsonl")
            is_new = not path.exists()
            last = _last_line(path)
            row = {**entry, "ts": stamp, "prev": _line_hash(last) if last else ZERO}
            raw = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
            with open(path, "ab") as handle:
                handle.write(raw.encode("utf-8") + b"\n")
        if is_new:
            protect_file(path)          
        log.info("write-audit %s", raw)
        return row


def verify_chain(path) -> tuple[bool, int | None]:
    "(True, None) when every line's `prev` matches the line before it; else (False, n)\n    with the 1-based number of the first line that does not."
    previous = ZERO
    number = 0
    with open(path, "rb") as handle:
        for raw in handle:
            raw = raw.rstrip(b"\n")
            if not raw.strip():
                continue
            number += 1
            try:
                claimed = json.loads(raw).get("prev")
            except (ValueError, AttributeError):
                return False, number
            if claimed != previous:
                return False, number
            previous = _line_hash(raw)
    return True, None


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "verify":
        ok, bad = verify_chain(argv[1])
        print("ok" if ok else f"chain broken at line {bad}")
        return 0 if ok else 1
    print("usage: python -m agent_context.write_audit verify <file>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
