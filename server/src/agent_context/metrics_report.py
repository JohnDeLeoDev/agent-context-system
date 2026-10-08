"Agent work metrics: the collector, the ledger backfill, the summary and the command line.\n\n`python -m agent_context.metrics <command>`:\n    phase CHANGE NAME     one phase event (the orchestrator's one call per phase)\n    collect               adoption, token import, cooling-off end (run every 5 minutes)\n    backfill FILE...      import the hand-written ledgers, marked src=backfill\n    summary               lead, waiting and active time, gates, adoption, tokens\n    verify                walk the hash chain\n\nThe event log and its rules live in `metrics.py`."
import argparse
import calendar
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_context import metrics, paths

from .flock import LOCK_EX, LOCK_NB, flock

RECENT_ID_BYTES = 262144
PRIORITY = ("user_approval", "gate", "review", "adoption", "active")
_ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z$")




def _epoch(ts: str) -> int:
    'UTC `YYYY-MM-DDTHH:MM:SS...` to epoch seconds. Slices instead of strptime: the\n    summary converts every event, and strptime is the slow part of a 100,000 line month.'
    return calendar.timegm((int(ts[0:4]), int(ts[5:7]), int(ts[8:10]),
                            int(ts[11:13]), int(ts[14:16]), int(ts[17:19]), 0, 0, 0))


def _iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _day_of(ts: str) -> str:
    return ts[:10]


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "agent-context"


def _whole(value: float) -> float | int:
    return int(value) if value == int(value) else round(value, 1)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]




def _cursor_file() -> Path:
    return metrics.metrics_dir() / "collect.cursor.json"


def _load_cursor(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cursor(path: Path, data: dict[str, object]) -> None:
    paths.write_atomic(path, json.dumps(data, sort_keys=True, separators=(",", ":")), mode=0o640)


def _recent_ids(path: Path, full: bool = False) -> set[str]:
    '`id` values in the last 256 KB of a month file, or in all of it when `full`.\n    Collector lines carry an id so a run killed between an emit and its cursor save does\n    not write the line twice. A lost cursor reads the whole file.'
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(0 if full else max(0, size - RECENT_ID_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return set()
    return set(re.findall(r'"id":"([^"]+)"', tail))


def _emit_once(event_type: str, event_id: str, ts: str, *, full: bool = False,
               **fields: object) -> str:
    "'emitted', 'duplicate' (already in the log) or 'failed'."
    log = metrics.metrics_dir() / (ts[:7] + ".jsonl")
    if event_id in _recent_ids(log, full):
        return "duplicate"
    return "emitted" if metrics.emit(event_type, ts=ts, id=event_id, **fields) else "failed"




def _collect_adoption(store: Path, cursor: dict[str, Any], now: float, counts: dict[str, int],
                      full: bool) -> None:
    seen: dict[str, str] = cursor.setdefault("adopt", {})
    seq: dict[str, int] = cursor.setdefault("adopt_seq", {})
    for status in sorted((store / "machines").glob("*/daemon-status.json")):
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        machine = str(data.get("machine_id") or "")
        commit = str(data.get("server_commit") or "")[:7]
        if not machine or not commit:
            continue
        before = seen.get(machine)
        if before == commit:
            continue
        fields: dict[str, object] = {"ref": machine + "@" + commit}
        if before is None:
            fields["src"] = "baseline"
        
        
        result = _emit_once("adopt", f"adopt:{machine}@{commit}:{seq.get(machine, 0)}",
                            _iso(now), full=full, **fields)
        if result == "failed":
            continue
        seq[machine] = seq.get(machine, 0) + 1
        seen[machine] = commit
        if result == "emitted":
            counts["adopt"] += 1


def _claim_change(claims_dir: Path | None, session: str) -> str | None:
    if claims_dir is None or not re.fullmatch(r"[A-Za-z0-9-]{1,36}", session):
        return None
    try:
        data = json.loads((claims_dir / (session + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    worktree = data.get("worktree") if isinstance(data, dict) else None
    return worktree if isinstance(worktree, str) and worktree else None


def _collect_tokens(token_db: Path | None, claims_dir: Path | None, cursor: dict[str, Any],
                    now: float, counts: dict[str, int], full: bool) -> None:
    if token_db is None or not token_db.is_file():
        return
    try:
        con = sqlite3.connect(f"file:{token_db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT day, COALESCE(root_session, session_id), COUNT(*), SUM(input_tokens),"
                " SUM(output_tokens), SUM(cache_read), SUM(cache_w5 + cache_w1h), SUM(cost_usd)"
                " FROM requests GROUP BY day, COALESCE(root_session, session_id)"
                " ORDER BY day, COALESCE(root_session, session_id)").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return
    seen: dict[str, list[float]] = cursor.setdefault("tokens", {})
    today = _day_of(_iso(now))
    for day, session, n, in_tok, out_tok, cache_r, cache_w, cost in rows:
        if not session or not day:
            continue
        key = f"{day}|{session}"
        total = [float(n or 0), float(in_tok or 0), float(out_tok or 0), float(cache_r or 0),
                 float(cache_w or 0), float(cost or 0)]
        prior = seen.get(key)
        base = prior if prior is not None else [0.0] * 6
        delta = [t - b for t, b in zip(total, base)]
        if delta[0] <= 0:
            continue
        ts = _iso(min(now, _epoch(day + "T23:59:59Z")))
        fields: dict[str, object] = {
            "session": session, "change": _claim_change(claims_dir, session),
            "n": int(delta[0]), "in_tok": int(delta[1]), "out_tok": int(delta[2]),
            "cache_r": int(delta[3]), "cache_w": int(delta[4]), "cost_usd": round(delta[5], 6)}
        if prior is None and day < today:
            fields["src"] = "baseline"
        result = _emit_once("tokens", f"tok:{day}:{session}:{int(total[0])}", ts, full=full, **fields)
        if result == "failed":
            continue
        seen[key] = total
        if result == "emitted":
            counts["tokens"] += 1


def _collect_cooloff(now: float, counts: dict[str, int]) -> None:
    events = metrics.read_events()
    closed = {int(str(e["n"])) for e in events if e.get("type") == "gate_cooloff_end" and "n" in e}
    for e in events:
        if e.get("type") != "gate_cooloff_start" or "dur_ms" not in e:
            continue
        start = _epoch(str(e["ts"]))
        end = start + int(str(e["dur_ms"])) // 1000
        if start in closed or end > now:
            continue
        result = _emit_once("gate_cooloff_end", f"cool:{start}", _iso(end), ref="daemon", n=start)
        if result == "emitted":
            counts["cooloff_end"] += 1
        if result != "failed":
            closed.add(start)


def collect(*, store: Path, token_db: Path | None = None, claims_dir: Path | None = None,
            now: float | None = None) -> dict[str, int]:
    "One collector pass. Reads the store's daemon-status files and the token database,\n    makes no network call, and adds no line on a repeat run."
    moment = time.time() if now is None else now
    cursor_path = _cursor_file()
    counts = {"adopt": 0, "tokens": 0, "cooloff_end": 0}
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    
    lock_fd = os.open(str(cursor_path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o640)
    try:
        try:
            flock(lock_fd, LOCK_EX | LOCK_NB)
        except OSError:
            return counts
        cursor = _load_cursor(cursor_path)
        full = not cursor        
        before = json.dumps(cursor, sort_keys=True)
        _collect_adoption(store, cursor, moment, counts, full)
        _collect_tokens(token_db, claims_dir, cursor, moment, counts, full)
        _collect_cooloff(moment, counts)
        if json.dumps(cursor, sort_keys=True) != before:
            _save_cursor(cursor_path, cursor)
    finally:
        os.close(lock_fd)
    return counts




def _bf_id(*parts: str) -> str:
    return "bf:" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


class _Backfill:
    def __init__(self) -> None:
        self.ids = {str(e["id"]) for e in metrics.read_events() if "id" in e}
        self.counts: dict[str, int] = {}

    def put(self, event_type: str, ts: str, **fields: object) -> None:
        event_id = _bf_id(event_type, ts, *[str(fields[k]) for k in sorted(fields)])
        if event_id in self.ids:
            return
        if metrics.emit(event_type, ts=ts, id=event_id, src="backfill", **fields):
            self.ids.add(event_id)
            self.counts[event_type] = self.counts.get(event_type, 0) + 1


def _approval_kind(header: str, question: str) -> str:
    if question.startswith("Unlock the locked test"):
        return "unlock"
    if question.startswith("Land branch") or header.startswith("Land"):
        return "landing"
    if question.startswith("Allow one git"):
        return "token"
    if question.startswith("Allow agent writes") or "sudo" in question.lower():
        return "sudo"
    if "criteria" in header.lower() or "criteria" in question.lower():
        return "criteria"
    return "other"


def _sibling_session(path: Path) -> str | None:
    sibling = path.with_name("timing-ledger.md")
    if sibling.is_file():
        match = re.search(r"session ([0-9a-f]{8})", sibling.read_text(encoding="utf-8")[:400])
        if match:
            return match.group(1)
    return path.parent.name if re.fullmatch(r"[0-9a-f]{8}", path.parent.name) else None


def _backfill_tsv(path: Path, text: str, bf: _Backfill) -> None:
    session = _sibling_session(path)
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        asked, answered = _ISO.match(cols[0]), _ISO.match(cols[1])
        if not asked or not answered:
            continue
        kind = _approval_kind(cols[2], cols[3])
        bf.put("approval_asked", asked.group(1) + "Z", ref=kind, session=session)
        bf.put("approval_answered", answered.group(1) + "Z", ref=kind, session=session)


_T = r"(\d\d:\d\d:\d\d)"


def _backfill_timing(text: str, bf: _Backfill) -> None:
    head = re.search(r"session ([0-9a-f]{8}).*?(\d{4}-\d\d-\d\d)", text.splitlines()[0]) if text else None
    if not head:
        return
    session, date = head.group(1), head.group(2)
    change: str | None = None
    for line in text.splitlines():
        title = re.match(r"^## ([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if title:
            change = title.group(1)
            continue
        if change is None or not line.startswith("- "):
            continue

        def phase(name: str, clock: str, change: str = change) -> None:
            bf.put("phase", f"{date}T{clock}Z", ref=name, change=change, session=session)

        asked = re.search(r"criteria asked " + _T + r", approved " + _T, line)
        if asked:
            phase("criteria_sent", asked.group(1))
            phase("criteria_approved", asked.group(2))
        locked = re.search(r"tests locked " + _T + r"(?: and " + _T + r")?", line)
        if locked:
            for clock in locked.groups():
                if clock:
                    phase("tests_locked", clock)
        if re.search(r"\bcommits?\b", line):
            for clock in re.findall(r"\b[0-9a-f]{7,40} " + _T, line):
                phase("committed", clock)
        landing = re.search(r"landing asked " + _T + r", approved " + _T, line)
        if landing:
            phase("landing_asked", landing.group(1))
            phase("landing_approved", landing.group(2))
        landed = re.search(r"\blanded " + _T, line)
        if landed:
            phase("landed", landed.group(1))


def _backfill_metrics_ledger(path: Path, text: str, bf: _Backfill) -> None:
    change_match = re.search(r"# Metrics ledger, change: (\S+)", text)
    date_match = re.search(r"Recorded from (\d{4}-\d\d-\d\d)T", text)
    if not change_match or not date_match:
        return
    change, date = change_match.group(1), date_match.group(1)
    session = path.parent.name if re.fullmatch(r"[0-9a-f]{8}", path.parent.name) else None
    for line in text.splitlines():
        row = re.match(r"^\|\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s*\|\s*(.*?)\s*\|?$", line)
        if row:
            if "review START" in row.group(2):
                bf.put("phase", row.group(1), ref="review_start", change=change, session=session)
            elif "review END" in row.group(2):
                bf.put("phase", row.group(1), ref="review_end", change=change, session=session)
            continue
        answer = re.match(r"^\|\s*(\S+)[^|]*\|[^|]*\|[^|]*\bby " + _T + r"Z", line)
        if answer:
            kind = "unlock" if answer.group(1) == "test-unlock" else "other"
            bf.put("approval_answered", f"{date}T{answer.group(2)}Z", ref=kind,
                   change=change, session=session)


def backfill(paths_in: Sequence[Path]) -> dict[str, int]:
    'backfill.'
    bf = _Backfill()
    for path in paths_in:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        name = Path(path).name
        if name.endswith(".tsv"):
            _backfill_tsv(Path(path), text, bf)
        elif text.startswith("# Timing ledger"):
            _backfill_timing(text, bf)
        elif "# Metrics ledger, change:" in text:
            _backfill_metrics_ledger(Path(path), text, bf)
    return bf.counts




def _since_epoch(since: str | None) -> int | None:
    if not since:
        return None
    match = re.fullmatch(r"(\d+)([dh])", since)
    if match:
        unit = 86400 if match.group(2) == "d" else 3600
        return int(time.time()) - int(match.group(1)) * unit
    return _epoch(since if "T" in since else since + "T00:00:00Z")


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _split_days(start: int, end: int) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    while start < end:
        boundary = (start // 86400 + 1) * 86400
        stop = min(end, boundary)
        out.append((_day_of(_iso(start)), stop - start))
        start = stop
    return out


def _terse_spans(state: Path) -> dict[str, list[tuple[int, int, int]]]:
    'Per session (8 chars): (start, end, kind) with kind 1 = working, 0 = idle.'
    rows: dict[str, list[tuple[int, str]]] = {}
    for line in _read_lines(state / "terse-telemetry.jsonl"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        at, event, session = row.get("at"), row.get("event"), row.get("session")
        if not (isinstance(at, str) and isinstance(event, str) and isinstance(session, str)):
            continue
        if not _ISO.match(at) or not session.strip():
            continue
        try:
            rows.setdefault(session[:8], []).append((_epoch(at), event))
        except ValueError:
            continue
    spans: dict[str, list[tuple[int, int, int]]] = {}
    for session, seq in rows.items():
        seq.sort(key=lambda r: r[0])
        current: int | None = None
        last_stop: int | None = None
        out = spans.setdefault(session, [])
        for at, event in seq:
            if event == "UserPromptSubmit" and current is None:
                if last_stop is not None and at > last_stop:
                    out.append((last_stop, at, 0))
                current = at
            elif event == "Stop" and current is not None:
                out.append((current, at, 1))
                last_stop, current = at, None
    return spans


def _terse_unattributed(state: Path) -> int:
    'Event rows in terse-telemetry with no usable session id (empty, blank, missing, not text).'
    count = 0
    for line in _read_lines(state / "terse-telemetry.jsonl"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        at, event, session = row.get("at"), row.get("event"), row.get("session")
        if not (isinstance(at, str) and isinstance(event, str) and _ISO.match(at)):
            continue
        if not (isinstance(session, str) and session.strip()):
            count += 1
    return count


def _sessions_section(spans: dict[str, list[tuple[int, int, int]]], day: str | None,
                      since: int | None) -> dict[str, Any]:
    acc: dict[str, dict[str, dict[str, Any]]] = {}
    for session in sorted(spans):
        for start, end, kind in spans[session]:
            if since is not None and end < since:
                continue
            for d, seconds in _split_days(start, end):
                if day and d != day:
                    continue
                cell = acc.setdefault(session, {}).setdefault(
                    d, {"working_s": 0, "idle_s": 0, "turns": 0, "lat": []})
                cell["working_s" if kind else "idle_s"] += seconds
            if kind and (not day or _day_of(_iso(start)) == day):
                cell = acc.setdefault(session, {}).setdefault(
                    _day_of(_iso(start)), {"working_s": 0, "idle_s": 0, "turns": 0, "lat": []})
                cell["turns"] += 1
                cell["lat"].append(end - start)
    out: dict[str, Any] = {}
    for session in sorted(acc):
        out[session] = {}
        for d in sorted(acc[session]):
            cell = acc[session][d]
            lat = cell["lat"]
            out[session][d] = {
                "working_s": cell["working_s"], "idle_s": cell["idle_s"], "turns": cell["turns"],
                "latency_median_s": _whole(statistics.median(lat)) if lat else None,
                "latency_p95_s": _whole(_percentile(lat, 0.95)) if lat else None,
                "requires_action_s": None, "source": "terse-telemetry"}
    return out


def _grants(state: Path, day: str | None, since: int | None) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for name in ("test-lock-consent.log", "git-write-consent.log"):
        for line in _read_lines(state / name):
            cols = line.split("\t")
            if len(cols) < 3 or cols[1] != "APPROVED-BY-QUESTION" or not _ISO.match(cols[0]):
                continue
            if day and _day_of(cols[0]) != day:
                continue
            try:
                if since is not None and _epoch(cols[0]) < since:
                    continue
            except ValueError:
                continue
            kinds[cols[2]] = kinds.get(cols[2], 0) + 1
    return {k: kinds[k] for k in sorted(kinds)}


def _waits(events: list[dict[str, Any]]) -> dict[str, Any]:
    open_asks: dict[tuple[str, str], list[int]] = {}
    waits: dict[str, list[int]] = {}
    for e in events:
        kind = e.get("type")
        if kind not in ("approval_asked", "approval_answered"):
            continue
        key = (str(e.get("session")), str(e.get("ref")))
        at = int(e["_t"])
        if kind == "approval_asked":
            open_asks.setdefault(key, []).append(at)
        elif open_asks.get(key):
            asked = open_asks[key].pop(0)
            waits.setdefault(key[1], []).append(max(0, at - asked))
    return {k: {"n": len(v), "median_s": _whole(statistics.median(v)), "max_s": max(v)}
            for k, v in sorted(waits.items())}


def _adoption_section(events: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, int | None]]:
    'Per landing that touched server/: lag to each known machine. A machine that adopts a\n    later landing has adopted this one. Returns (section, landing ref -> last adoption epoch).'
    landed = [e for e in events if e.get("type") == "landed" and e.get("ok") is True and e.get("ref")]
    rank: dict[str, int] = {}
    for i, e in enumerate(landed):
        rank.setdefault(str(e["ref"]), i)
    adopts: dict[str, list[tuple[int, str]]] = {}
    machines: set[str] = set()
    for e in events:
        if e.get("type") != "adopt" or "@" not in str(e.get("ref", "")):
            continue
        machine, sha = str(e["ref"]).split("@", 1)
        machines.add(machine)
        if e.get("src") != "baseline":
            adopts.setdefault(machine, []).append((int(e["_t"]), sha))
    section: dict[str, Any] = {}
    finished: dict[str, int | None] = {}
    for i, e in enumerate(landed):
        at = int(e["_t"])
        lags: dict[str, int | None] = {}
        latest = 0
        for machine in sorted(machines):
            lag = None
            for when, sha in adopts.get(machine, []):
                if when >= at and rank.get(sha, -1) >= i:
                    lag = when - at
                    break
            lags[machine] = lag
            if lag is not None:
                latest = max(latest, at + lag)
        ref = str(e["ref"])
        section[ref] = {"landed_at": str(e["ts"]), "machines": lags}
        done = machines and all(v is not None for v in lags.values())
        finished[ref] = latest if done else None
    return section, finished


def _sweep(intervals: dict[str, list[tuple[int, int]]], first: int, last: int) -> dict[str, int]:
    'Give every second of [first, last] to the highest-priority label covering it.'
    points: list[tuple[int, str, int]] = []
    for label, spans in intervals.items():
        for start, end in spans:
            start, end = max(start, first), min(end, last)
            if end > start:
                points.append((start, label, 1))
                points.append((end, label, -1))
    points.sort(key=lambda p: p[0])
    depth = {label: 0 for label in PRIORITY}
    totals = {label: 0 for label in PRIORITY}
    totals["unattributed"] = 0
    cursor, i = first, 0
    while cursor < last:
        while i < len(points) and points[i][0] <= cursor:
            depth[points[i][1]] += points[i][2]
            i += 1
        nxt = points[i][0] if i < len(points) else last
        nxt = min(max(nxt, cursor + 1), last)
        label = next((lb for lb in PRIORITY if depth[lb] > 0), "unattributed")
        totals[label] += nxt - cursor
        cursor = nxt
    return totals


def _changes_section(events: list[dict[str, Any]], finished: dict[str, int | None],
                     spans: dict[str, list[tuple[int, int, int]]]) -> dict[str, Any]:
    by_change: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e.get("change") and e.get("type") not in ("tokens", "adopt"):
            by_change.setdefault(str(e["change"]), []).append(e)
    out: dict[str, Any] = {}
    for name in sorted(by_change):
        evs = by_change[name]
        times = [int(e["_t"]) for e in evs]
        first, last = min(times), max(times)
        intervals: dict[str, list[tuple[int, int]]] = {label: [] for label in PRIORITY}
        open_pair: dict[str, int] = {}
        workers: set[str] = set()
        for e in evs:
            at, kind, ref = int(e["_t"]), e.get("type"), str(e.get("ref"))
            if kind in ("gate_start", "gate_end", "landed") and e.get("session"):
                workers.add(str(e["session"]))
            if kind == "phase":
                pair = {"criteria_sent": "criteria", "criteria_approved": "criteria",
                        "landing_asked": "landing", "landing_approved": "landing",
                        "review_start": "review", "review_end": "review"}.get(ref)
                if pair is None:
                    continue
                if ref.endswith(("_sent", "_asked", "_start")):
                    open_pair[pair] = at
                elif pair in open_pair:
                    label = "review" if pair == "review" else "user_approval"
                    intervals[label].append((open_pair.pop(pair), at))
            elif kind in ("gate_start", "gate_end"):
                key = "gate:" + ref
                if kind == "gate_start":
                    open_pair[key] = at
                elif key in open_pair:
                    intervals["gate"].append((open_pair.pop(key), at))
                elif "dur_ms" in e:
                    intervals["gate"].append((at - int(e["dur_ms"]) // 1000, at))
            elif kind == "approval_asked":
                open_pair["appr:" + ref] = at
            elif kind == "approval_answered" and "appr:" + ref in open_pair:
                intervals["user_approval"].append((open_pair.pop("appr:" + ref), at))
        for e in evs:
            if e.get("type") == "landed" and e.get("ok") is True:
                done = finished.get(str(e.get("ref")))
                if done is not None:
                    intervals["adoption"].append((int(e["_t"]), done))
                    last = max(last, done)
        for session in workers:
            intervals["active"].extend((s, t) for s, t, kind in spans.get(session[:8], []) if kind)
        seconds = _sweep(intervals, first, last)
        lead = last - first
        shown: dict[str, Any] = {label: seconds[label] for label in PRIORITY}
        shown["other_session"] = None
        shown["unattributed"] = seconds["unattributed"]
        out[name] = {
            "lead_s": lead, "seconds": shown,
            "share": {k: round(100.0 * v / lead, 1) for k, v in shown.items()
                      if v is not None} if lead else {}}
    return out


def _gates_section(events: list[dict[str, Any]]) -> dict[str, Any]:
    runs: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e.get("type") == "gate_end":
            runs.setdefault(str(e.get("ref")), []).append(e)
    out: dict[str, Any] = {}
    for ref in sorted(runs):
        rs = runs[ref]
        durations = [int(e["dur_ms"]) / 1000 for e in rs if "dur_ms" in e]
        streaks: dict[str, bool] = {}
        flips = 0
        for e in rs:
            key = str(e.get("change"))
            ok = e.get("ok") is True
            if ok and streaks.get(key) is False:
                flips += 1
            streaks[key] = ok
        out[ref] = {
            "runs": len(rs), "failed": sum(1 for e in rs if e.get("ok") is False),
            "dur_median_s": _whole(statistics.median(durations)) if durations else None,
            "dur_p95_s": _whole(_percentile(durations, 0.95)) if durations else None,
            "red_then_green": flips}
    return out


def _tokens_section(events: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("requests", "in_tok", "out_tok", "cache_r", "cache_w", "cost_usd")

    def blank() -> dict[str, Any]:
        return {k: 0 for k in keys}

    total, by_session, by_change = blank(), {}, {}
    for e in events:
        if e.get("type") != "tokens":
            continue
        add = {"requests": int(e.get("n", 0)), "in_tok": int(e.get("in_tok", 0)),
               "out_tok": int(e.get("out_tok", 0)), "cache_r": int(e.get("cache_r", 0)),
               "cache_w": int(e.get("cache_w", 0)), "cost_usd": float(e.get("cost_usd", 0))}
        cells = [total, by_session.setdefault(str(e.get("session") or "-"), blank())]
        if e.get("change"):
            cells.append(by_change.setdefault(str(e["change"]), blank()))
        for cell in cells:
            for k in keys:
                cell[k] += add[k]
    for cell in [total, *by_session.values(), *by_change.values()]:
        cell["cost_usd"] = round(cell["cost_usd"], 6)
    return {"total": total, "by_session": dict(sorted(by_session.items())),
            "by_change": dict(sorted(by_change.items()))}


def build_summary(*, state: Path, day: str | None = None, change: str | None = None,
                  since: str | None = None) -> dict[str, Any]:
    floor = _since_epoch(since)
    everything = metrics.read_events()
    for e in everything:
        try:
            e["_t"] = _epoch(str(e.get("ts", "1970-01-01T00:00:00Z")))
        except ValueError:
            e["_t"] = 0
    events = [e for e in everything
              if (not day or _day_of(str(e.get("ts", ""))) == day)
              and (floor is None or int(str(e["_t"])) >= floor)]
    spans = _terse_spans(state)
    adoption, finished = _adoption_section(everything)
    scoped = [e for e in events if not change or e.get("change") == change]
    changes = _changes_section(scoped if change else events, finished, spans)
    if change:
        changes = {k: v for k, v in changes.items() if k == change}
    starts = [e for e in events if e.get("type") == "gate_cooloff_start" and "dur_ms" in e]
    grants = _grants(state, day, floor)
    sessions = _sessions_section(spans, day, floor)
    return {
        "empty": not events and not sessions and not grants,
        "sessions": sessions,
        "terse_unattributed_rows": _terse_unattributed(state),
        "approvals": {
            "granted_by_kind": grants,
            "measured_waits": _waits(events),
            "wait_note": "wait is not measurable for approvals with no recorded ask time; "
                         "the approval hook records it from stage 2"},
        "changes": changes,
        "gates": _gates_section(scoped),
        "cooloff": {"windows": len(starts),
                    "seconds": sum(int(str(e["dur_ms"])) // 1000 for e in starts)},
        "adoption": {k: v for k, v in adoption.items()
                     if (not day or _day_of(v["landed_at"]) == day)},
        "tokens": _tokens_section(scoped),
        "not_measured": ["requires_action", "hops", "blocks", "approval_wait", "other_session"],
    }


def _fmt(seconds: float | int | None) -> str:
    if seconds is None:
        return "n/m"
    total = int(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def render_text(summary: dict[str, Any]) -> str:
    out = ["Agent work metrics"]
    if summary.get("empty"):
        out.append("No events recorded.")
    if summary["changes"]:
        out += ["", "Changes (lead time, then seconds by cause; n/m = not measured)"]
        for name, ch in summary["changes"].items():
            sec = ch["seconds"]
            causes = "  ".join(f"{k} {_fmt(v)}" for k, v in sec.items())
            out.append(f"  {name:<28} lead {_fmt(ch['lead_s'])} | {causes}")
    if summary["gates"]:
        out += ["", "Gates"]
        for ref, g in summary["gates"].items():
            out.append(f"  {ref:<12} runs {g['runs']}  failed {g['failed']}  "
                       f"median {_fmt(g['dur_median_s'])}  p95 {_fmt(g['dur_p95_s'])}  "
                       f"red then green {g['red_then_green']}")
    if summary["cooloff"]["windows"]:
        out.append(f"  cooling off: {summary['cooloff']['windows']} window(s), "
                   f"{_fmt(summary['cooloff']['seconds'])}")
    if summary["adoption"]:
        out += ["", "Adoption lag per landing"]
        for ref, a in summary["adoption"].items():
            lags = "  ".join(f"{m} {'not adopted' if v is None else _fmt(v)}"
                             for m, v in a["machines"].items())
            out.append(f"  {ref} landed {a['landed_at']}: {lags}")
    appr = summary["approvals"]
    if appr["granted_by_kind"] or appr["measured_waits"]:
        granted = ", ".join(f"{k} {v}" for k, v in appr["granted_by_kind"].items())
        out += ["", "Approvals", "  granted: " + granted]
        for kind, w in appr["measured_waits"].items():
            out.append(f"  wait {kind:<9} n {w['n']}  median {_fmt(w['median_s'])}  "
                       f"max {_fmt(w['max_s'])}")
        out.append("  " + appr["wait_note"])
    if summary["sessions"]:
        out += ["", "Sessions (working / idle / turns / median turn)"]
        for session, days in summary["sessions"].items():
            for d, c in days.items():
                out.append(f"  {session} {d}  {_fmt(c['working_s'])} / {_fmt(c['idle_s'])} / "
                           f"{c['turns']} / {_fmt(c['latency_median_s'])}")
    if summary["terse_unattributed_rows"]:
        out += ["", (f"Session rows without a session id (left out above): "
                     f"{summary['terse_unattributed_rows']} unattributed")]
    tok = summary["tokens"]["total"]
    if tok["requests"]:
        line = (f"Tokens: {tok['requests']} requests, in {tok['in_tok']}, out {tok['out_tok']}, "
                f"cache read {tok['cache_r']}, cache write {tok['cache_w']}, "
                f"cost ${tok['cost_usd']:.2f}")
        out += ["", line]
    out += ["", "Not measured: " + ", ".join(summary["not_measured"])]
    return "\n".join(out) + "\n"




def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m agent_context.metrics")
    sub = p.add_subparsers(dest="cmd", required=True)
    ph = sub.add_parser("phase", help="emit one phase event")
    ph.add_argument("change")
    ph.add_argument("name")
    co = sub.add_parser("collect", help="one collector pass")
    co.add_argument("--store")
    co.add_argument("--token-db")
    co.add_argument("--claims")
    bf = sub.add_parser("backfill", help="import hand-written ledgers")
    bf.add_argument("files", nargs="+")
    su = sub.add_parser("summary", help="print the summary")
    su.add_argument("--state")
    su.add_argument("--change")
    su.add_argument("--day")
    su.add_argument("--since")
    su.add_argument("--json", action="store_true")
    sub.add_parser("verify", help="walk the hash chain")
    return p


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(list(argv))
    if args.cmd == "phase":
        if args.name not in metrics.PHASES:
            print(f"unknown phase {args.name!r}; one of: {', '.join(metrics.PHASES)}")
            return 2
        if metrics.emit("phase", change=args.change, ref=args.name):
            return 0
        print("not written (rejected, locked or unwritable)")
        return 1
    if args.cmd == "collect":
        state = _state_dir()
        store = Path(args.store or os.environ.get("AGENT_CONTEXT_STORE")
                     or os.path.join(os.path.expanduser("~"), ".agent-context"))
        counts = collect(store=store, token_db=Path(args.token_db or state / "token-usage.db"),
                         claims_dir=Path(args.claims or state / "claims"))
        print(json.dumps(counts, sort_keys=True))
        return 0
    if args.cmd == "backfill":
        print(json.dumps(backfill([Path(f) for f in args.files]), sort_keys=True))
        return 0
    if args.cmd == "summary":
        try:
            summary = build_summary(state=Path(args.state) if args.state else _state_dir(),
                                    day=args.day, change=args.change, since=args.since)
        except ValueError:
            print(f"cannot read --since {args.since!r}: use 7d, 12h, YYYY-MM-DD or a UTC timestamp")
            return 2
        print(json.dumps(summary, sort_keys=True, indent=2) if args.json else render_text(summary), end="")
        return 0
    result = metrics.verify()
    if result.ok:
        print(f"ok: {result.lines} lines")
        return 0
    print(f"BROKEN: {result.bad_file} line {result.bad_line} ({result.reason})")
    return 1
