"The uuid hint is only a hint. A caller cannot know its own store-registered uuid without\nreading machines/*.toml, so the machine is re-resolved from the body's `hostname` and\n`home_dir`, the same fields `_ensure_machine` writes into a machine row, and the rollup is\nwritten under the RESOLVED uuid, never the hint. No match, no write."
from __future__ import annotations

import json
import os
import re
import threading

from . import paths

_WRITE_LOCK = threading.Lock()

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_BYTES = 5 * 1024 * 1024


class UploadRefused(Exception):
    'A reason and an HTTP status, both safe to print.'

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def parse_upload_request(uuid_hint: str, month: str, body: bytes) -> tuple[str, str, dict, dict | None]:
    "(hostname, home_dir, usage, tools) from a validated request, or raises UploadRefused.\n    `tools` is the rollup's per-tool block; optional, since a collector older than chunk 3\n    sent only `usage`."
    if not UUID_RE.match(uuid_hint):
        raise UploadRefused(400, "uuid_hint must be a UUID")
    if not MONTH_RE.match(month):
        raise UploadRefused(400, "month must be YYYY-MM")
    if len(body) > MAX_BYTES:
        raise UploadRefused(413, "body too large")
    try:
        data = json.loads(body)
    except ValueError:
        raise UploadRefused(400, "body is not JSON") from None
    if not isinstance(data, dict):
        raise UploadRefused(400, "body must be a JSON object")
    hostname, home_dir, usage = data.get("hostname"), data.get("home_dir"), data.get("usage")
    if not isinstance(hostname, str) or not hostname:
        raise UploadRefused(400, "hostname is required")
    if not isinstance(home_dir, str) or not home_dir:
        raise UploadRefused(400, "home_dir is required")
    if not isinstance(usage, dict) or not usage:
        raise UploadRefused(400, "usage must be a non-empty JSON object")
    tools = data.get("tools")
    if tools is not None and not isinstance(tools, dict):
        raise UploadRefused(400, "tools must be a JSON object")
    return hostname, home_dir, usage, tools or None


def resolve_machine_uuid(store, hostname: str, home_dir: str) -> str | None:
    'The one machine row `hostname`/`home_dir` identifies: home_dir first (more specific,\n    includes the account), hostname as a fallback whenever home_dir does not narrow to exactly\n    one row on its own, zero matches or several (two NAS units on the same fleet account share\n    a home_dir; only their hostnames differ). None when neither narrows to exactly one row.'
    machines = [e for e in store.entities.values()
               if e.get("type") == "machine" and e.get("machine_uuid")]
    by_home = [e for e in machines if e.get("home_dir") == home_dir]
    if len(by_home) == 1:
        return by_home[0].get("machine_uuid")
    by_host = [e for e in machines if e.get("hostname") == hostname]
    if len(by_host) == 1:
        return by_host[0].get("machine_uuid")
    return None


def write_rollup(store, machine_uuid: str, month: str, usage: object, tools: object = None) -> bool:
    "Write the rollup under the resolved machine's uuid; True when the bytes changed.\n\n    Serialized the same way every time (sorted keys, compact separators), so an unchanged\n    upload compares equal to what is on disk and writes nothing — no commit for an hourly\n    re-upload of the same month."
    path = os.path.join(store.root, "machines", machine_uuid, "token-usage", f"{month}.json")
    payload = {"usage": usage, "tools": tools} if tools else {"usage": usage}
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    
    
    
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
