"deps_report: this machine's dependency verdict, for `fleet.publish` to carry into the store.\n\n`global/scripts/deps-check.py` writes ~/.local/state/agent-context/deps.json. This module reads\nits `fleet` block and computes nothing: `fleet.problems` owns the wording. It never raises,\nbecause it runs inside the sync loop's publish.\n\nNone means unknown: no report, or an unreadable one. A caller must not read None as clean.\nA report older than STALE_SECS (the checker has not run) comes back with `stale` set and `ok`\nNone, so fleet health can report that the checker stopped."

import contextlib
import json
import time
from pathlib import Path

from . import paths

STALE_SECS = 3 * 86400
_LISTS = ("missing", "broken", "below_floor")


def path() -> Path:
    'deps.json beside the health directory, under the same patchable home.'
    return paths.health_dir().parent / "deps.json"


def read(report_path=None, now=None) -> dict | None:
    'The publishable block: {ok, missing, broken, below_floor}, or None when unknown.'
    t = time.time() if now is None else now
    with contextlib.suppress(Exception):
        doc = json.loads(Path(report_path or path()).read_text())
        block = doc["fleet"]
        at = doc["at"]
        if (isinstance(at, (int, float)) and isinstance(block, dict)
                and isinstance(block.get("ok"), bool)
                and all(isinstance(block.get(k), list) for k in _LISTS)):
            if t - at > STALE_SECS:
                return {"ok": None, "stale": True, **{k: [] for k in _LISTS}}
            return {"ok": block["ok"], **{k: sorted(str(n) for n in block[k]) for k in _LISTS}}
    return None
