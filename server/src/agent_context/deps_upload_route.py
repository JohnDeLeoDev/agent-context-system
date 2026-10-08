"PUT /deps/<uuid-hint>.json: a relay-only machine's deps-check.py report, uploaded to ls since\nsuch a machine has no store checkout and no daemon to publish machines/<uuid>/daemon-status.json\nof its own.\n\nModeled on token_usage_route.py: the URL's uuid segment is only a hint, re-resolved from the\nbody's `hostname`/`home_dir` via that module's own `resolve_machine_uuid` (reused, not\nduplicated). The body is deps-check.py's own report shape (see `global/scripts/deps-check.py`'s\n`evaluate()` and `deps_report.read()`'s expectations of it) plus `hostname`/`home_dir` for the\nmatch -- the same shape token_usage_route's body adds them to a collector rollup. `hostname` and\n`home_dir` are stripped before the report is written, so machines/<uuid>/deps.json stays exactly\nwhat deps-check.py would have written locally."
from __future__ import annotations

import json
import os
import threading

from . import paths
from .token_usage_route import UUID_RE, resolve_machine_uuid  

_WRITE_LOCK = threading.Lock()

MAX_BYTES = 1 * 1024 * 1024
_FLEET_LISTS = ("missing", "broken", "below_floor")


class UploadRefused(Exception):
    'A reason and an HTTP status, both safe to print.'

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def parse_upload_request(uuid_hint: str, body: bytes) -> tuple[str, str, dict]:
    '(hostname, home_dir, report) from a validated request, or raises UploadRefused.\n\n    `report` is the body with `hostname`/`home_dir` removed -- exactly what gets written to\n    machines/<uuid>/deps.json. Only what `deps_report.read()` actually needs is required:\n    `at` and a `fleet` block shaped {ok: bool, missing: [...], broken: [...], below_floor: [...]}.'
    if not UUID_RE.match(uuid_hint):
        raise UploadRefused(400, "uuid_hint must be a UUID")
    if len(body) > MAX_BYTES:
        raise UploadRefused(413, "body too large")
    try:
        data = json.loads(body)
    except ValueError:
        raise UploadRefused(400, "body is not JSON") from None
    if not isinstance(data, dict):
        raise UploadRefused(400, "body must be a JSON object")
    hostname, home_dir = data.get("hostname"), data.get("home_dir")
    if not isinstance(hostname, str) or not hostname:
        raise UploadRefused(400, "hostname is required")
    if not isinstance(home_dir, str) or not home_dir:
        raise UploadRefused(400, "home_dir is required")
    at = data.get("at")
    if not isinstance(at, (int, float)):
        raise UploadRefused(400, "at is required")
    fleet_block = data.get("fleet")
    if not isinstance(fleet_block, dict) or not isinstance(fleet_block.get("ok"), bool) or not all(
            isinstance(fleet_block.get(k), list) for k in _FLEET_LISTS):
        raise UploadRefused(400, "fleet must be {ok: bool, missing: [...], broken: [...], "
                                 "below_floor: [...]}")
    report = {k: v for k, v in data.items() if k not in ("hostname", "home_dir")}
    return hostname, home_dir, report


def write_deps(store, machine_uuid: str, report: dict) -> bool:
    "Write the report under the resolved machine's uuid; True when the bytes changed.\n\n    Same compare-then-write-under-a-lock shape as token_usage_route.write_rollup, so a repeated\n    identical upload writes nothing and arms no commit."
    path = os.path.join(store.root, "machines", machine_uuid, "deps.json")
    blob = json.dumps(report, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with _WRITE_LOCK:
        try:
            with open(path, "rb") as fh:
                if fh.read() == blob:
                    return False
        except OSError:
            pass
        paths.write_atomic(path, blob)
        store._arm_commit(path)
        return True
