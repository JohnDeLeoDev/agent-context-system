#!/usr/bin/env python3
'Roll Claude Code transcript token usage into durable local + fleet storage.\n\nWHY THIS EXISTS\n---------------\nClaude Code writes a complete `usage` block on every assistant message —\ninput, cache-write (5m and 1h separately), cache-read, output — alongside the\ndimensions worth slicing by: sessionId, cwd, gitBranch, model, effort,\nisSidechain. That is everything needed to answer "what is actually spending my\ntokens", and it is thrown away: transcripts are swept at `cleanupPeriodDays`\n(default 30). `~/.claude/metrics/costs.jsonl` and `stats-cache.json` are written\nby older code paths, carry no token counts, and are already months stale — they\nare not substitutes. Whatever is not rolled up before the sweep is gone.\n\nTRIGGERS. Wired to SessionStart, Stop (--min-interval 30) and SessionEnd; see\nthe token-usage-collect hook. Stop is what keeps the numbers near-real-time,\nincluding inside a session that runs for days; SessionStart catches a session\nthat was killed before either of the others fired. There is deliberately no\ntimer — those three leave no window one would catch.\n\nTwo traps, both handled here and both silent if you get them wrong:\n\n  1. Resumed sessions replay history into a new transcript, and counting the\n     replays inflates every total. Dedupe is on `requestId`,\n     globally, forever (hence the PRIMARY KEY rather than a per-run set).\n\n  2. A tool_use block and its tool_result can land in different incremental\n     chunks, or the tool_use can arrive on a request that dedupe discards. Name\n     resolution therefore cannot be an in-memory per-file dict; `tool_calls` is\n     persisted and joined against `tool_results` at report time.\n\nSTORAGE, two tiers, deliberately split\n--------------------------------------\n  local SQLite (state_dir/token-usage.db) — one row per request, one per tool\n    result. Machine-local, never git. ~80 MB/month at this fleet\'s volume. This\n    is what survives the 30-day sweep and what supports drill-down along\n    dimensions nobody thought to pre-aggregate.\n\n  store rollups (machines/<uuid>/token-usage/YYYY-MM.json) — daily aggregates,\n    so the evidence window is the fleet\'s, not one machine\'s. This script\n    never writes the store: it builds each month under\n    ~/.cache/agent-context/token-rollup and uploads it with the relay_report MCP\n    tool, and the daemon writes and commits it (policy). ls\n    uploads the same way as every relay machine.\n\nAmortization is computed at report time. A tool result\'s true cost is\nits cache-write plus a cache-read on every request behind it in the session, and\nthat tail keeps growing after the row is written. Storing a number here would\nfreeze it wrong. What is stored is `req_ordinal` (position in the transcript)\nand the file\'s running `req_count`; the tail falls out of the subtraction.\n\nObservations guarded: #206, #371, #392, #444.'
from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_mcp

SCHEMA_VERSION = 3












_REFUSAL_NAMEPAT = r'(?:BLOCKED|Blocked|REWROTE) by [`]?[a-z][a-z0-9-]+'
_REFUSAL_PATHFORM = r'PreToolUse:\S+ hook error: \[[^\]]*hooks/[A-Za-z0-9._-]+\.(?:sh|py)\]?'
_REFUSAL_PAT = re.compile(
    _REFUSAL_NAMEPAT + r'|' + _REFUSAL_PATHFORM + r'|Denied by preToolUse hook')

ROLLUP_TOOL_MIN_CALLS = 3   






DERIVATION_VERSION = 2









PRICES = {
    "claude-fable-5":    ((None, (10.0, 50.0)),),
    "claude-mythos-5":   ((None, (10.0, 50.0)),),
    "claude-opus-5":     ((None, (5.0, 25.0)),),
    "claude-opus-4-8":   ((None, (5.0, 25.0)),),
    "claude-opus-4-7":   ((None, (5.0, 25.0)),),
    "claude-opus-4-6":   ((None, (5.0, 25.0)),),
    "claude-sonnet-5":   (("2026-08-31", (2.0, 10.0)), (None, (3.0, 15.0))),
    "claude-sonnet-4-6": ((None, (3.0, 15.0)),),
    "claude-haiku-4-5":  ((None, (1.0, 5.0)),),
}
FALLBACK_PRICE = (5.0, 25.0)   

_WORKTREE = re.compile(r"/\.(?:agents|claude)/worktrees/[^/]+")
_DATED = re.compile(r"-\d{8}$")

_OTHER_HOME = re.compile(r"^/(?:Users|home|var/services/homes)/[^/]+/")







UPLOAD_PLACEHOLDER_UUID = "00000000-0000-4000-8000-000000000000"


def store_is_checkout(store: Path) -> bool:
    return (store / ".git").exists() or (store / "server").is_dir()


def upload_month(month: str, usage: object, tools: object = None) -> None:
    'Upload one month\'s `usage` and `tools` blocks via relay_report(kind="token_usage").\n    Raises on any failure; the caller decides what that means for the local cache (leave it\n    stale so the next run retries).'
    report = {"hostname": socket.gethostname(), "home_dir": str(Path.home()), "usage": usage}
    if tools:
        report["tools"] = tools
    body = json.dumps(report)
    store_mcp.call("relay_report", {"kind": "token_usage", "uuid_hint": UPLOAD_PLACEHOLDER_UUID,
                                    "month": month, "body": body})


def upload_rollups(out_dir: Path) -> None:
    "Upload every month file whose bytes changed since it last uploaded clean.\n\n    Never raises: a failure is printed to stderr and that month's cache entry is left stale\n    (or absent) so the next run retries it. A 200 (written true or false) means ls now reflects\n    this content, so the hash is cached either way."
    cache_path = out_dir / ".uploaded.json"
    cache = {}
    with contextlib.suppress(OSError, ValueError):
        loaded = json.loads(cache_path.read_text())
        if isinstance(loaded, dict):
            cache = loaded
    changed = False
    for p in sorted(out_dir.glob("*.json")):
        if p.name == cache_path.name:
            continue
        month = p.stem
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if cache.get(month) == digest:
            continue
        try:
            payload = json.loads(raw)
            usage, tools = payload["usage"], payload.get("tools")
        except (ValueError, KeyError, TypeError, AttributeError):
            print(f"token-usage: {p} is malformed, skipping upload", file=sys.stderr)
            continue
        try:
            upload_month(month, usage, tools)
        except Exception as exc:
            print(f"token-usage: upload of {month} failed: {exc}", file=sys.stderr)
            continue
        cache[month] = digest
        changed = True
    if changed:
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
        os.replace(tmp, cache_path)


def sweep_legacy_token_usage(store: Path) -> None:
    'Remove machines/*/token-usage/*.json left from when this machine was a checkout, and any\n    now-empty token-usage or machines/<uuid> dir. Never touches machines/ itself. Safe to call\n    every invocation; a no-op once already swept.'
    for tu_dir in sorted((store / "machines").glob("*/token-usage")):
        for f in sorted(tu_dir.glob("*.json")):
            with contextlib.suppress(OSError):
                f.unlink()
        with contextlib.suppress(OSError):
            tu_dir.rmdir()
        with contextlib.suppress(OSError):
            tu_dir.parent.rmdir()


def price_for(model: str, day: str = ""):
    '(input, output) $/MTok for `model` as it was priced on `day` (YYYY-MM-DD).\n\n    Strips a dated suffix (claude-haiku-4-5-20251001); unknown models fall back to\n    the Opus tier and are reported by the caller so a new model cannot silently\n    price itself. An empty `day` takes the last (current) tier.'
    key = _DATED.sub("", model or "")
    tiers = PRICES.get(key) or PRICES.get(model or "")
    if not tiers:
        return FALLBACK_PRICE
    if day:
        for until, rates in tiers:
            if until is None or day <= until:
                return rates
    return tiers[-1][1]


def state_dir() -> Path:
    "Mirrors agent_context.paths.state_dir — deliberately re-derived rather than\n    imported, because this script runs from Claude Code hooks under a bare python3\n    with no access to the server's venv."
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "agent-context"
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        d = Path(base) / "agent-context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def rollup_dir() -> Path:
    "Where this machine's monthly rollups wait for upload. TOKEN_ROLLUP_DIR overrides it,\n    so a test never writes, or uploads from, the real cache."
    return Path(os.environ.get("TOKEN_ROLLUP_DIR")
                or Path.home() / ".cache" / "agent-context" / "token-rollup")


def store_root() -> Path:
    return Path(os.environ.get("AGENT_CONTEXT_ROOT") or (Path.home() / ".agent-context"))


@functools.lru_cache(maxsize=8192)
def _repo_root(path: str) -> str:
    'Nearest enclosing git repo root, or "" when there is none.'
    stop = {Path.home(), Path("/")}
    p = Path(path)
    for cand in (p, *p.parents):
        try:
            if (cand / ".git").exists():
                return str(cand)
        except OSError:
            return ""
        if cand in stop:
            break
    return ""


def _tilde(path: str) -> str:
    "Replace this machine's home with `~`."
    h = str(Path.home())
    if path == h:
        return "~"
    if path.startswith(h + "/"):
        return "~" + path[len(h):]
    return _OTHER_HOME.sub("~/", path)   


def project_of(cwd: str) -> str:
    'Stable project identity for a working directory.\n\n    Worktree first (a feature branch is not its own project), then up to the repo\n    root, then $HOME -> ~. Order matters: stripping the worktree segment yields a\n    path under the main checkout, which is what the repo-root walk then resolves.'
    if not cwd:
        return "(unknown)"
    path = _WORKTREE.sub("", cwd)
    if path.startswith("~"):                      
        path = str(Path.home()) + path[1:]
    return _tilde(_repo_root(path) or path)




DDL = """
CREATE TABLE IF NOT EXISTS requests (
  request_id    TEXT PRIMARY KEY,
  ts            TEXT NOT NULL,
  day           TEXT NOT NULL,
  session_id    TEXT,
  root_session  TEXT,
  project       TEXT,
  git_branch    TEXT,
  model         TEXT,
  effort        TEXT,
  sidechain     INTEGER NOT NULL DEFAULT 0,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_w5      INTEGER NOT NULL DEFAULT 0,
  cache_w1h     INTEGER NOT NULL DEFAULT 0,
  cache_read    INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd      REAL    NOT NULL DEFAULT 0,
  -- Everything except output. The amortized tool ranking normalizes against
  -- INPUT-side spend: a tool result is context, and context is never billed as
  -- output tokens, so folding output in would inflate every tool's dollars.
  cost_input_usd REAL   NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_requests_day     ON requests(day);
CREATE INDEX IF NOT EXISTS ix_requests_project ON requests(project);
CREATE INDEX IF NOT EXISTS ix_requests_session ON requests(root_session);

-- Persisted rather than held per-file in memory: a tool_use and its result can
-- straddle two incremental chunks, and the tool_use may sit on a request that
-- dedupe drops.
CREATE TABLE IF NOT EXISTS tool_calls (
  tool_use_id TEXT PRIMARY KEY,
  day         TEXT,
  session_id  TEXT,
  project     TEXT,
  model       TEXT,
  tool        TEXT NOT NULL,
  src         TEXT NOT NULL,
  req_ordinal INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tool_calls_tool ON tool_calls(tool);
CREATE INDEX IF NOT EXISTS ix_tool_calls_day  ON tool_calls(day);

CREATE TABLE IF NOT EXISTS tool_results (
  tool_use_id  TEXT PRIMARY KEY,
  result_bytes INTEGER NOT NULL DEFAULT 0,
  est_tokens   INTEGER NOT NULL DEFAULT 0,
  is_error     INTEGER NOT NULL DEFAULT 0,
  -- A hook refusal, not a tool failure: is_error=1 but the tool never ran.
  -- See _REFUSAL_PAT. Always 0 when is_error=0.
  refused      INTEGER NOT NULL DEFAULT 0
);

-- One row per transcript. `offset` is a byte position, so a growing file is
-- resumed rather than re-parsed; `req_count` is the denominator the amortization
-- tail is measured against.
CREATE TABLE IF NOT EXISTS files (
  path      TEXT PRIMARY KEY,
  inode     INTEGER,
  size      INTEGER NOT NULL DEFAULT 0,
  offset    INTEGER NOT NULL DEFAULT 0,
  req_count INTEGER NOT NULL DEFAULT 0,
  mtime     REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    
    
    
    
    
    durable = os.environ.get("AGENT_CONTEXT_FSYNC", "1").strip().lower() not in (
        "0", "false", "no", "off")
    con.execute("PRAGMA synchronous=%s" % ("NORMAL" if durable else "OFF"))
    con.executescript(DDL)
    
    
    cols = {r[1] for r in con.execute("PRAGMA table_info(requests)")}
    if "cost_input_usd" not in cols:
        con.execute("ALTER TABLE requests ADD COLUMN cost_input_usd REAL NOT NULL DEFAULT 0")
    tr_cols = {r[1] for r in con.execute("PRAGMA table_info(tool_results)")}
    if "refused" not in tr_cols:
        con.execute("ALTER TABLE tool_results ADD COLUMN refused INTEGER NOT NULL DEFAULT 0")
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    con.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('tracking_since',?)", (str(time.time()),))
    con.commit()
    return con


def cost_of(model: str, day: str, u: dict) -> tuple:
    '-> (cost_usd, cost_input_usd, w5, w1h). Prefers the explicit 5m/1h split;\n    older records only carry the flat total, which is then treated as 5m.'
    inp, out = price_for(model, day)
    cc = u.get("cache_creation") or {}
    w5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    if not (w5 or w1):
        w5 = int(u.get("cache_creation_input_tokens") or 0)
    in_usd = (
        int(u.get("input_tokens") or 0) * inp
        + w5 * inp * 1.25
        + w1 * inp * 2.0
        + int(u.get("cache_read_input_tokens") or 0) * inp * 0.1
    ) / 1e6
    out_usd = int(u.get("output_tokens") or 0) * out / 1e6
    return in_usd + out_usd, in_usd, w5, w1


_CD_PREFIX = re.compile(r"^\s*(?:cd|pushd)\s+\S+\s*(?:&&|;|\n)\s*")
_NOISE_TOKENS = ("sudo", "time", "env", "command", "exec", "nohup")


def tool_label(name: str, inp) -> str:
    '`Bash` and `Agent` are too coarse to act on — a Bash that shells out to\n    `grep` and one that runs a 40 MB build log are the same row otherwise.\n\n    The leading `cd <dir> &&` has to go first: it is the single most common way a\n    Bash call opens, and labeling on it would make `Bash:cd` the top tool\n    while saying nothing.'
    if not isinstance(inp, dict):
        return name
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        prev = None
        while cmd != prev:                    
            prev, cmd = cmd, _CD_PREFIX.sub("", cmd)
        for tok in cmd.split():
            if tok in _NOISE_TOKENS or "=" in tok:
                continue
            return "Bash:" + tok.rsplit("/", 1)[-1][:24]
        return "Bash:?"
    if name in ("Agent", "Task"):
        return "Agent:" + str(inp.get("subagent_type") or "default")
    if name == "Skill":
        return "Skill:" + str(inp.get("skill") or "?")
    return name


def scan(con: sqlite3.Connection, projects_root: Path, *, verbose=False, full=False) -> dict:
    stats: dict[str, Any] = dict(files=0, files_read=0, bytes=0, requests=0, dup=0,
                                  tool_calls=0, tool_results=0, unknown_models=set())
    cur = con.cursor()
    known = {r[0]: r for r in cur.execute("SELECT path,inode,size,offset,req_count FROM files")}

    for path in sorted(projects_root.rglob("*.jsonl")):
        stats["files"] += 1
        sp = str(path)
        try:
            st = path.stat()
        except OSError:
            continue
        prev = known.get(sp)
        offset, req_count = 0, 0
        if prev and not full:
            _, inode, size, off, rc = prev
            
            if inode == st.st_ino and st.st_size == size:
                continue
            if inode == st.st_ino and st.st_size >= size:
                offset, req_count = off, rc
            
            

        root_session = path.stem
        if path.parent.name == "subagents":
            root_session = path.parent.parent.name

        try:
            fh = path.open("r", errors="replace")
        except OSError:
            continue
        with fh:
            fh.seek(offset)
            chunk = fh.read()
            
            
            cut = chunk.rfind("\n")
            if cut < 0:
                continue
            consumed = chunk[: cut + 1]
            new_offset = offset + len(consumed.encode("utf-8", "replace"))
            stats["files_read"] += 1
            stats["bytes"] += len(consumed)

            reqs, tcalls, tresults = [], [], []
            for line in consumed.splitlines():
                if '"usage"' not in line and '"tool_result"' not in line and '"tool_use"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                etype = e.get("type")
                if etype == "assistant":
                    m = e.get("message") or {}
                    model = m.get("model") or "(unknown)"
                    day = (e.get("timestamp") or "")[:10]
                    sid = e.get("sessionId") or e.get("session_id") or root_session
                    proj = project_of(e.get("cwd") or "")
                    u = m.get("usage")
                    if u:
                        req_count += 1
                        rid = e.get("requestId") or m.get("id")
                        if rid:
                            if model != "<synthetic>" and _DATED.sub("", model) not in PRICES:
                                stats["unknown_models"].add(model)
                            usd, in_usd, w5, w1 = cost_of(model, day, u)
                            reqs.append((
                                rid, e.get("timestamp") or "", day, sid, root_session, proj,
                                e.get("gitBranch"), model, e.get("effort") or "(none)",
                                1 if e.get("isSidechain") else 0,
                                int(u.get("input_tokens") or 0), w5, w1,
                                int(u.get("cache_read_input_tokens") or 0),
                                int(u.get("output_tokens") or 0), usd, in_usd,
                            ))
                    
                    
                    
                    for b in (m.get("content") or ()):
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                            tcalls.append((b["id"], day, sid, proj, model,
                                           tool_label(b.get("name") or "?", b.get("input")),
                                           sp, req_count))
                elif etype == "user":
                    m = e.get("message") or {}
                    content = m.get("content")
                    if not isinstance(content, list):
                        continue
                    for b in content:
                        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                            continue
                        tid = b.get("tool_use_id")
                        if not tid:
                            continue
                        c = b.get("content")
                        text = c if isinstance(c, str) else json.dumps(c, default=str)
                        n = len(text)
                        is_err = 1 if b.get("is_error") else 0
                        refused = 1 if (is_err and _REFUSAL_PAT.search(text)) else 0
                        tresults.append((tid, n, n // 4, is_err, refused))

            before = con.total_changes
            cur.executemany(
                "INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", reqs)
            inserted = con.total_changes - before
            stats["requests"] += inserted
            stats["dup"] += len(reqs) - inserted
            cur.executemany("INSERT OR IGNORE INTO tool_calls VALUES (?,?,?,?,?,?,?,?)", tcalls)
            cur.executemany("INSERT OR IGNORE INTO tool_results VALUES (?,?,?,?,?)", tresults)
            stats["tool_calls"] += len(tcalls)
            stats["tool_results"] += len(tresults)
            cur.execute(
                "INSERT INTO files(path,inode,size,offset,req_count,mtime) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET inode=excluded.inode, size=excluded.size, "
                "offset=excluded.offset, req_count=excluded.req_count, mtime=excluded.mtime",
                (sp, st.st_ino, st.st_size, new_offset, req_count, st.st_mtime))
            con.commit()
            if verbose:
                print(f"  {path.name[:40]:40s} +{inserted:5d} req  off={new_offset}")

    stats["unknown_models"] = sorted(stats["unknown_models"])
    return stats




def rollup(con: sqlite3.Connection, out_dir: Path, months=None) -> list:
    'Daily aggregates, one file per month, one writer per file.\n\n    Columns are written once in a header and rows are bare arrays: at ~40 tools x\n    30 days the object-per-row form was most of the bytes, and this lands in git.'
    cur = con.cursor()
    if months is None:
        months = [r[0] for r in cur.execute(
            "SELECT DISTINCT substr(day,1,7) FROM requests WHERE day<>'' ORDER BY 1")]
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for mo in months:
        usage_cols = ["day", "project", "model", "effort", "sidechain", "requests",
                      "input", "cache_w5", "cache_w1h", "cache_read", "output", "cost_usd",
                      "cost_input_usd"]
        usage_rows = [list(r) for r in cur.execute(
            "SELECT day, project, model, effort, sidechain, COUNT(*), "
            "  SUM(input_tokens), SUM(cache_w5), SUM(cache_w1h), SUM(cache_read), "
            "  SUM(output_tokens), ROUND(SUM(cost_usd),6), ROUND(SUM(cost_input_usd),6) "
            "FROM requests WHERE substr(day,1,7)=? "
            "GROUP BY 1,2,3,4,5 ORDER BY 1,2,3", (mo,))]
        
        
        
        
        tool_cols = ["day", "project", "tool", "calls", "result_bytes", "est_tokens", "errors",
                    "refusals"]
        raw = list(cur.execute(
            "SELECT c.day, c.project, c.tool, COUNT(*), "
            "  COALESCE(SUM(r.result_bytes),0), COALESCE(SUM(r.est_tokens),0), "
            "  COALESCE(SUM(r.is_error),0), COALESCE(SUM(r.refused),0) "
            "FROM tool_calls c LEFT JOIN tool_results r USING(tool_use_id) "
            "WHERE substr(c.day,1,7)=? GROUP BY 1,2,3 ORDER BY 1,2,3", (mo,)))
        tool_rows, other = [], {}
        for day, proj, tool, calls, rb, et, errs, refs in raw:
            if calls >= ROLLUP_TOOL_MIN_CALLS:
                tool_rows.append([day, proj, tool, calls, rb, et, errs, refs])
            else:
                o = other.setdefault((day, proj), [0, 0, 0, 0, 0])
                o[0] += calls
                o[1] += rb
                o[2] += et
                o[3] += errs
                o[4] += refs
        for (day, proj), o in sorted(other.items()):
            tool_rows.append([day, proj, "(other)", *o])
        tool_rows.sort(key=lambda r: (r[0], r[1], r[2]))
        payload = {
            "schema": SCHEMA_VERSION,
            "month": mo,
            "generated_at": time.time(),
            "usage": {"columns": usage_cols, "rows": usage_rows},
            "tools": {"columns": tool_cols, "rows": tool_rows},
        }
        blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        p = out_dir / f"{mo}.json"
        
        
        with contextlib.suppress(OSError):
            old = json.loads(p.read_text())
            old.pop("generated_at", None)
            new = json.loads(blob)
            new.pop("generated_at", None)
            if old == new:
                continue
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(blob)
        os.replace(tmp, p)
        written.append(str(p))
    return written




def recompute(con: sqlite3.Connection) -> dict:
    'Re-derive cost and project identity for every row already stored.\n\n    Rows are priced and named at INGEST time, so a corrected rate or a corrected\n    notion of "project" would otherwise apply only to transcripts not yet swept —\n    leaving the historical majority wrong forever, which is the opposite of why\n    this database exists. Every input needed is already columnar (token counts,\n    model, day), so the recomputation is exact rather than a re-parse.\n\n    Idempotent: project_of re-expands a leading `~` before resolving, so running\n    this twice is a no-op rather than a progressive mangling.'
    cur = con.cursor()
    out = {"requests": 0, "tool_calls": 0, "cost_delta_usd": 0.0, "projects_merged": 0}

    rows = cur.execute(
        "SELECT request_id, day, model, project, input_tokens, cache_w5, cache_w1h, "
        "       cache_read, output_tokens, cost_usd FROM requests").fetchall()
    updates, before, after = [], 0.0, 0.0
    for rid, day, model, proj, i, w5, w1, cr, o, was in rows:
        u = {"input_tokens": i, "cache_read_input_tokens": cr, "output_tokens": o,
             "cache_creation": {"ephemeral_5m_input_tokens": w5,
                                "ephemeral_1h_input_tokens": w1}}
        usd, in_usd, _, _ = cost_of(model, day, u)
        updates.append((usd, in_usd, project_of(proj or ""), rid))
        before += was or 0.0
        after += usd
    cur.executemany("UPDATE requests SET cost_usd=?, cost_input_usd=?, project=? "
                    "WHERE request_id=?", updates)
    out["requests"] = len(updates)
    out["cost_delta_usd"] = round(after - before, 2)

    
    tc = cur.execute("SELECT DISTINCT project FROM tool_calls").fetchall()
    mapping = [(project_of(p or ""), p) for (p,) in tc]
    cur.executemany("UPDATE tool_calls SET project=? WHERE project IS ?", mapping)
    out["tool_calls"] = len(tc)
    out["projects_merged"] = len(tc) - len({m[0] for m in mapping})
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('derivation_version',?)",
                (str(DERIVATION_VERSION),))
    con.commit()
    return out


def derivation_is_stale(con: sqlite3.Connection) -> bool:
    'True when stored rows were derived by an older pricing/identity rule.\n\n    A database that predates the marker entirely counts as stale — that is the\n    upgrade path from every machine collecting before this existed.'
    row = con.execute("SELECT v FROM meta WHERE k='derivation_version'").fetchone()
    return (row[0] if row else None) != str(DERIVATION_VERSION)











_STALE_LOCK_SECS = 900


def stamp_path(db_path: Path) -> Path:
    return db_path.with_suffix(".stamp")


def rollup_stamp_path(db_path: Path) -> Path:
    'Separate stamp for the STORE rollup. Collection and publication are throttled\n    independently: collection must keep up with every turn (transcripts are swept at\n    cleanupPeriodDays and uncollected spend is gone for good), while the rollup is a\n    derived aggregate the fleet reads occasionally.'
    return db_path.with_suffix(".rollup.stamp")


def stamp_is_fresh(stamp: Path, min_interval: float) -> bool:
    'True when `stamp` was touched less than `min_interval` seconds ago.'
    if min_interval <= 0:
        return False
    try:
        return (time.time() - stamp.stat().st_mtime) < min_interval
    except OSError:
        return False


def recently_ran(db_path: Path, min_interval: float) -> bool:
    "True when a completed run is younger than `min_interval` seconds.\n\n    The Stop hook fires on every assistant turn. Without this guard a long\n    session forks a full transcript walk hundreds of times to insert a\n    handful of rows each. With it, collection is bounded to once per interval\n    and the rest of the turns cost one stat() call.\n\n    The stamp is touched on every completed run, including one that inserted\n    nothing — keying off the database's own mtime instead would mean an idle\n    scan never advanced it, so the guard would never trip precisely when there\n    is nothing to do."
    return stamp_is_fresh(stamp_path(db_path), min_interval)


@contextlib.contextmanager
def single_writer(path: Path):
    fd = None
    try:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            age = time.time() - path.stat().st_mtime if path.exists() else _STALE_LOCK_SECS
            if age < _STALE_LOCK_SECS:
                yield False
                return
            with contextlib.suppress(OSError):
                path.unlink()
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        yield True
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--projects", default=hp.projects_dir())
    ap.add_argument("--db", default=None)
    ap.add_argument("--full", action="store_true", help="re-read every transcript from byte 0")
    ap.add_argument("--no-rollup", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", "-q", action="store_true", help="print only on error (hook use)")
    ap.add_argument("--min-interval", type=float, default=0.0, metavar="SECS",
                    help="no-op if a run completed less than SECS ago (Stop hook uses 30)")
    ap.add_argument("--rollup-min-interval", type=float, default=3600.0, metavar="SECS",
                    help="refresh the STORE rollup at most this often (default 3600). "
                         "Collection into the local DB is never throttled by this.")
    ap.add_argument("--force-rollup", action="store_true",
                    help="rewrite the rollup regardless of --rollup-min-interval")
    ap.add_argument("--recompute", action="store_true",
                    help="re-price and re-identify every stored row, then rewrite the "
                         "rollups (run after a rate change or a project-identity change)")
    a = ap.parse_args(argv)

    db_path = Path(a.db) if a.db else state_dir() / "token-usage.db"
    projects = Path(a.projects)
    if not projects.is_dir():
        print(f"no transcripts at {projects}", file=sys.stderr)
        return 0

    if recently_ran(db_path, a.min_interval):
        if not a.quiet:
            print(f"token-usage: a run completed < {a.min_interval:g}s ago; skipping")
        return 0

    t0 = time.time()
    with single_writer(db_path.with_suffix(".lock")) as acquired:
        if not acquired:
            if not a.quiet:
                print("token-usage: another collector is running; nothing to do")
            return 0
        con = connect(db_path)
        
        
        rc = recompute(con) if (a.recompute or derivation_is_stale(con)) else None
        st = scan(con, projects, verbose=a.verbose, full=a.full)
        
        
        
        
        
        
        
        
        
        
        
        
        
        force_rollup = a.force_rollup or bool(rc)
        store = store_root()
        out_dir = rollup_dir()
        if not store_is_checkout(store):
            
            sweep_legacy_token_usage(store)
        if a.no_rollup:
            written = []
        elif not force_rollup and stamp_is_fresh(rollup_stamp_path(db_path),
                                                 a.rollup_min_interval):
            written = []
            st["rollup_skipped"] = f"< {a.rollup_min_interval:g}s since last refresh"
        else:
            written = rollup(con, out_dir)
            rollup_stamp_path(db_path).touch()
            upload_rollups(out_dir)
        if rc:
            st["recomputed"] = rc
        con.close()
        
        stamp_path(db_path).touch()
    st["rollups"] = written
    st["db"] = str(db_path)
    st["elapsed"] = round(time.time() - t0, 2)

    if a.quiet and not a.json:
        return 0
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print(f"token-usage: {st['requests']} new requests ({st['dup']} dup skipped), "
              f"{st['files_read']}/{st['files']} transcripts read, "
              f"{st['tool_calls']} tool calls, {st['elapsed']}s")
        if st.get("recomputed"):
            rc = st["recomputed"]
            print(f"  recomputed {rc['requests']:,} requests and {rc['tool_calls']} project "
                  f"labels: cost delta ${rc['cost_delta_usd']:+,.2f}, "
                  f"{rc['projects_merged']} project(s) merged")
        if st["unknown_models"]:
            print(f"  unpriced models (using Opus-tier fallback): {', '.join(st['unknown_models'])}")
        for w in written:
            print(f"  rollup -> {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
