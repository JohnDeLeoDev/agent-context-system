'Read-side telemetry for the store.\n\nThe rest of the store measures the write side: what exists, what it costs to\nload, how many rows a scope spends. This module measures whether any of it is\never read. Tiering a memory into the always-loaded bootstrap is decided once, at\nwrite time, and every session on every machine pays for it afterwards, so a scope\nnear its memory-row budget needs evidence of which always-loaded memories earn\nthe slot.\n\nTwo signals, deliberately kept apart:\n  reads — the body was actually fetched (get_memory / get_doc / get_skill …).\n          Strong: an agent decided it needed this specific thing.\n  hits  — it surfaced in a search result. Weak: it merely matched a query, and\n          the agent may have ignored it.\n\nDemotion should be driven by `reads`; `hits` is the tiebreak that says "this is\nstill findable and still relevant" for something with no direct reads.\n\nThe live counters stay outside the git store: they churn on every call, and a\nhot shared file would be in every rebase. What is synced is a per-machine\nsnapshot, `machines/<uuid>/usage.json`, written by the daemon\'s sync loop at most\nhourly and only when changed. One writer per file means no merge conflicts, and\n`load_fleet()` sums the snapshots so the evidence window is the fleet\'s, not one\nmachine\'s. Per-machine windows alone are each too short and too easily reset to\ngive the "re-tier on evidence" rule any evidence.\n\nEvery entry point is fail-silent. Telemetry that can break a read is worse than\nno telemetry.'
from __future__ import annotations



import contextlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import dryrun, paths

_FLUSH_SECS = 30.0          
_lock = threading.Lock()
_data: dict | None = None   
_dirty = False
_last_flush = 0.0








_pending: dict = {}




_state_dir = paths.state_dir


def _path() -> Path:
    override = os.environ.get("AGENT_CONTEXT_USAGE_FILE")
    return Path(override) if override else _state_dir() / "usage.json"


def _enabled() -> bool:
    return os.environ.get("AGENT_CONTEXT_USAGE", "1") not in ("0", "false", "no")


def _read_file() -> dict | None:
    'The on-disk map, or None if it is absent or unreadable.'
    try:
        raw = json.loads(_path().read_text())
    except Exception:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("e"), dict):
        return None
    return {"since": float(raw.get("since") or time.time()), "e": raw["e"]}


def _merge_rows(dst: dict, src: dict) -> None:
    "Add src's counts into dst. Reads and hits sum; last_read takes the later one."
    for k, v in src.items():
        cur = dst.get(k)
        if cur is None:
            dst[k] = {"r": v.get("r", 0), "h": v.get("h", 0), "t": v.get("t", 0)}
        else:
            cur["r"] = cur.get("r", 0) + v.get("r", 0)
            cur["h"] = cur.get("h", 0) + v.get("h", 0)
            cur["t"] = max(cur.get("t", 0) or 0, v.get("t", 0) or 0)


def _under_tmp(p: str) -> bool:
    "True when `p` resolves inside the OS temp root — i.e. it is a test's tmpdir.\n\n    macOS matters here: `mktemp -d` and pytest hand back /var/folders/<...>, which\n    realpath()s to /private/var/folders/<...> and matches none of the literal /tmp\n    prefixes, so a prefix list alone would miss every tmpdir on this fleet's Macs."
    try:
        rp = os.path.realpath(p)
    except Exception:
        return False
    for root in (tempfile.gettempdir(), "/tmp", "/private/tmp",
                 "/var/folders", "/private/var/folders"):
        try:
            if rp.startswith(os.path.realpath(root).rstrip(os.sep) + os.sep):
                return True
        except Exception:
            continue
    return False


def _forensics(reason: str, since: float) -> None:
    'Record the moment a fresh observation window is started, and its context.\n\n    A window reset is invisible after the fact: the only evidence it leaves is a\n    file that looks normal. `publish_snapshot` keeps a local reset from shortening\n    the fleet\'s recorded window, but that does not say what caused the reset.\n\n    So the evidence is captured at the reset, in the one branch that can start a\n    new window. What is captured separates two causes a later reader cannot\n    otherwise tell apart: the file being unlinked (parent directory still\n    present, still holding its other entries) and the directory being wiped or\n    moved, which is what a cleaner, a migration or an OS-level "Application\n    Support" sweep looks like.\n\n    The record is written under ~/.local/state, not beside the usage file, so\n    whatever removes that file does not remove the record too. Fail-silent, like\n    everything else in this module: instrumentation must never be the reason a\n    session cannot count a read.'
    
    
    
    
    
    if os.environ.get("AGENT_CONTEXT_USAGE_FILE"):
        return


    with contextlib.suppress(Exception):
        
        
        
        p = str(_path())
        parent = os.path.dirname(p)
        try:
            entries = sorted(os.listdir(parent))
            parent_state = {
                "exists": True,
                "mtime": os.stat(parent).st_mtime,
                "n_entries": len(entries),
                
                
                "entries": entries[:12],
            }
        except OSError as exc:
            parent_state = {"exists": False, "error": str(exc)}

        rec = {
            "ts": time.time(),
            "reason": reason,
            "since": since,
            "path": p,
            "parent": parent_state,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "argv": sys.argv[:6],
        }
        
        
        
        
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state")

        
        
        
        
        
        
        
        
        
        if _under_tmp(str(_path())) and not _under_tmp(base):
            return

        out = os.path.join(base, "agent-context", "usage-window-forensics.jsonl")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")


def _load() -> dict:
    global _data
    if _data is None:
        d = _read_file()
        if d is None:
            
            
            
            
            
            since = time.time()
            reason = "absent"
            with contextlib.suppress(OSError):
                since = _path().stat().st_mtime
                
                
                
                reason = "unparseable"
            _forensics(reason, since)
            d = {"since": since, "e": {}}
        _data = d
    return _data


def _key(kind: str, scope: str, name: str) -> str:
    return f"{kind}\t{scope}\t{name}"


def record(kind: str, scope: str, name: str, *, read: bool = False, hit: bool = False) -> None:
    'Count one read and/or one search hit against an entity. Never raises.'
    if not _enabled() or not name or dryrun.active():     
        return
    global _dirty
    try:
        with _lock:
            e = _load()["e"]
            row = e.setdefault(_key(kind, scope or "global", name), {"r": 0, "h": 0, "t": 0})
            now = time.time()
            pend = _pending.setdefault(_key(kind, scope or "global", name),
                                       {"r": 0, "h": 0, "t": 0})
            if read:
                row["r"] += 1
                row["t"] = now
                pend["r"] += 1
                pend["t"] = now
            if hit:
                row["h"] += 1
                row["t"] = row["t"] or now
                pend["h"] += 1
                pend["t"] = pend["t"] or now
            _dirty = True
        _maybe_flush()
    except Exception:
        pass


_MISS_MAX_BYTES = 256 * 1024


def _miss_path() -> Path:
    return _path().with_name("search-misses.jsonl")


def record_miss(query: str, project, top_score: float, rows: int) -> None:
    'Append one search that returned nothing, or nothing strong. Never raises.\n\n    A hit is counted against the entity it found. A miss has no entity, so without this\n    a question the store cannot answer leaves no trace and the gap is never filled.'
    if not _enabled() or not query or dryrun.active():
        return
    try:
        p = _miss_path()
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > _MISS_MAX_BYTES:
                lines = p.read_text(encoding="utf-8").splitlines()
                p.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": int(time.time()), "q": query[:200], "project": project,
                                    "rows": rows, "top": round(top_score, 2)}) + "\n")
    except Exception:
        pass


def misses(limit: int = 20) -> list:
    'Missed queries on this machine, most frequent first, then most recent.'
    try:
        lines = _miss_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    seen: dict = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key = " ".join(str(row.get("q") or "").lower().split())
        if not key:
            continue
        hit = seen.setdefault(key, {"query": row.get("q"), "count": 0, "last": 0,
                                    "rows": row.get("rows"), "top": row.get("top")})
        hit["count"] += 1
        if (row.get("t") or 0) >= hit["last"]:
            hit.update(last=row.get("t") or 0, rows=row.get("rows"), top=row.get("top"))
    return sorted(seen.values(), key=lambda r: (-r["count"], -r["last"]))[:limit]


def record_many(kind: str, pairs, *, read: bool = False, hit: bool = False) -> None:
    'Same as record() for a whole result set: pairs of (scope, name).'
    for scope, name in pairs or ():
        record(kind, scope, name, read=read, hit=hit)


def _maybe_flush() -> None:
    global _last_flush
    if not _dirty:
        return
    now = time.monotonic()
    if now - _last_flush < _FLUSH_SECS:
        return
    _last_flush = now
    flush()


def flush() -> None:
    "Merge this process's delta into the shared file, atomically and under a lock.\n    Called on a timer from record(), and at interpreter exit so a short-lived stdio\n    process doesn't lose its whole session.\n\n    Read-modify-write, never blind overwrite: with stdio transport there is one server\n    process per session, so several are writing this file concurrently. Two details do\n    the work. The flock serializes the read-merge-write so no update is lost, and the\n    temp file carries the PID -- a single shared `usage.json.tmp` would have every\n    writer filling the same path and then publishing whatever another one left there."
    global _dirty, _data, _pending
    if not _enabled():
        return
    try:
        from .flock import LOCK_EX, LOCK_UN, flock
        with _lock:
            if _data is None or not _dirty or not _pending:
                return
            delta, _pending = _pending, {}
            p = _path()
            lockfile = p.with_name(p.name + ".lock")
            with open(lockfile, "a+") as lf:
                flock(lf, LOCK_EX)
                try:
                    merged = _read_file() or {"since": _data["since"], "e": {}}
                    _merge_rows(merged["e"], delta)
                    
                    merged["since"] = min(float(merged["since"]), float(_data["since"]))
                    paths.write_atomic(p, json.dumps(merged, separators=(",", ":")))
                finally:
                    flock(lf, LOCK_UN)
            
            
            _data = merged
            _dirty = False
    except Exception:
        pass


def stats(kind: str, scope: str, name: str) -> dict:
    '{reads, hits, last_read} for one entity — zeros when never touched.'
    try:
        with _lock:
            row = _load()["e"].get(_key(kind, scope or "global", name))
    except Exception:
        row = None
    row = row or {"r": 0, "h": 0, "t": 0}
    return {"reads": row.get("r", 0), "hits": row.get("h", 0), "last_read": row.get("t", 0) or None}


def tracking_since() -> float:
    "Epoch when this machine started counting. Without it, 'never read' is\n    indistinguishable from 'tracking only started yesterday', and demoting on that\n    basis would quietly gut the index."
    try:
        with _lock:
            return _load()["since"]
    except Exception:
        return time.time()


def snapshot() -> dict:
    '{since, entries: {key: {reads, hits, last_read}}} — for reporting tools.'
    try:
        with _lock:
            d = _load()
            return {
                "since": d["since"],
                "entries": {k: {"reads": v.get("r", 0), "hits": v.get("h", 0),
                                "last_read": v.get("t", 0) or None}
                            for k, v in d["e"].items()},
            }
    except Exception:
        return {"since": time.time(), "entries": {}}






_PUBLISH_MIN_SECS = 3600.0
_last_publish = 0.0


def _machine_uuid(machine_uuid=None) -> str:
    if machine_uuid:
        return machine_uuid
    from .machine import get_machine_uuid  
    return get_machine_uuid()


def snapshot_path(store_root, machine_uuid=None) -> Path:
    "Where this machine's counters land inside the store."
    return Path(store_root) / "machines" / _machine_uuid(machine_uuid) / "usage.json"


def publish_snapshot(store_root, machine_uuid=None, *, force=False) -> bool:
    "Copy this machine's counters into the store so the fleet can be aggregated.\n\n    Returns True when a file was written. Skipped (False) when disabled, inside the\n    publish interval (unless `force`), or when the snapshot on disk is already\n    byte-identical — so an idle machine never dirties the tree. Never raises."
    global _last_publish
    if not _enabled():
        return False
    try:
        now = time.monotonic()
        if not force and _last_publish and now - _last_publish < _PUBLISH_MIN_SECS:
            return False
        p = snapshot_path(store_root, machine_uuid)
        with _lock:
            d = _load()
            
            
            
            
            
            
            
            
            
            
            
            
            since = float(d["since"])
            with contextlib.suppress(OSError, ValueError, TypeError):
                prior = json.loads(p.read_text())
                if isinstance(prior, dict) and prior.get("since"):
                    since = min(since, float(prior["since"]))
            payload = json.dumps({"since": since, "e": d["e"]},
                                 separators=(",", ":"), sort_keys=True)
        with contextlib.suppress(OSError):
            if p.read_text() == payload:
                _last_publish = now
                return False
        
        
        
        paths.write_atomic(p, payload)
        _last_publish = now
        return True
    except Exception:
        return False


def _merge_into(merged: dict, entries: dict) -> None:
    for k, v in entries.items():
        if not isinstance(v, dict):
            continue
        row = merged.setdefault(k, {"r": 0, "h": 0, "t": 0})
        row["r"] += int(v.get("r") or 0)
        row["h"] += int(v.get("h") or 0)
        row["t"] = max(row["t"], float(v.get("t") or 0))


class FleetUsage:
    "A merged, read-only view over every machine's counters."

    def __init__(self, since: float, entries: dict, machines: list):
        self.since = since
        self.machines = machines
        self._e = entries

    def stats(self, kind: str, scope: str, name: str) -> dict:
        row = self._e.get(_key(kind, scope or "global", name)) or {"r": 0, "h": 0, "t": 0}
        return {"reads": row.get("r", 0), "hits": row.get("h", 0),
                "last_read": row.get("t", 0) or None}

    @property
    def days(self) -> float:
        return max(0.0, (time.time() - self.since) / 86400)


def load_fleet(store_root, machine_uuid=None, *, per_machine=False) -> FleetUsage:
    "Merge every `machines/*/usage.json` in the store with this machine's live\n    counters. reads/hits sum per (kind, scope, name); `since` is the oldest window\n    start, so tracking_days is the fleet's, not one machine's. Our own on-disk\n    snapshot is skipped in favor of the live counters (it is only ever older).\n    A missing or corrupt snapshot is skipped, not fatal. `per_machine=True` returns\n    the local view only."
    me = _machine_uuid(machine_uuid)
    merged, sinces, machines = {}, [], []
    if not per_machine:
        root = Path(store_root) / "machines"
        try:
            dirs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            dirs = []
        for d in dirs:
            if d.name == me:
                continue
            try:
                raw = json.loads((d / "usage.json").read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict) or not isinstance(raw.get("e"), dict):
                continue
            sinces.append(float(raw.get("since") or time.time()))
            machines.append(d.name)
            _merge_into(merged, raw["e"])
    try:
        with _lock:
            _merge_into(merged, _load()["e"])
    except Exception:
        pass
    sinces.append(tracking_since())
    machines.append(me)
    return FleetUsage(min(sinces), merged, machines)
