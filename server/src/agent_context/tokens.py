
'Read side of token-usage tracking: what actually spent the tokens.\n\n`usage.py` answers "is this always-loaded memory slot earning its bytes". This\nmodule answers the larger question sitting behind it — where the token spend\ngoes at all, across conversations, projects, models, and tools — and it is the\nsame shape of answer, so it is the same shape of machinery: per-machine files\nin the store, one writer each, merged at read time so the evidence window is\nthe fleet\'s rather than one laptop\'s.\n\nWrites happen elsewhere. `global/scripts/token-usage-collect.py` owns every\nwrite: it parses `~/.claude/projects/**/*.jsonl` incrementally into a local\nSQLite database and publishes daily rollups to\n`machines/<uuid>/token-usage/YYYY-MM.json`. That script is stdlib-only and\nstandalone precisely because it runs from a SessionEnd hook under a bare\npython3, with no access to this package or its venv. Nothing here writes.\n\nTwo sources:\n\n  rollups  — daily aggregates by (day, project, model, effort, sidechain) and\n             by (day, project, tool). Small, git-synced, fleet-wide. This is the\n             default source and the only one that can see other machines.\n\n  local DB — one row per request and per tool result. Machine-local, never\n             synced (it is too large for git). It is the\n             only source that can answer session-level questions or compute\n             amortization, and it says nothing about any other machine.\n\nA report never silently mixes them: `source` names which one answered, and\nanything drawn from the local DB is labeled this-machine-only.\n\nWhy amortization is not a dollar decomposition\n----------------------------------------------\nA tool result is not paid for once. It is paid on the cache-write that admits\nit, and again as a cache-read on every subsequent request in the session that\ncarries it. So a large Read late in a long session costs little, and the same\nRead early in the session costs a lot; no per-call token count shows that\ndifference.\n\nWhat this computes is therefore a marginal cost: for each tool result,\n`est_tokens x input_price x (1.25 + 0.1 x tail)`, where `tail` is the number of\nrequests behind it in that transcript. Summed over all tools it exceeds the\nbilled total, because those tails overlap each other and the system\nprompt: every request\'s cache-read is claimed in full by each of the results\nriding inside it. That overlap makes it a valid ranking and an invalid\ndecomposition. Reports therefore lead with `context_share_pct`; the dollar\nfigure is normalized onto real input spend and named `attributed_usd` so it is\nnever read as billed.'
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import paths





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



_MIN_DAYS = 7

GROUPS = ("project", "model", "effort", "day", "sidechain", "tool", "session")
_LOCAL_ONLY = ("session",)


def _price(model: str, day: str = ""):
    "(input, output) $/MTok as of `day` — kept in step with the collector's\n    price_for(). Rates are effective-dated because they change: pricing a request\n    from inside an intro-rate window at the later rate overstates it."
    m = (model or "").rsplit("-", 1)
    base = model or ""
    if len(m) == 2 and m[1].isdigit() and len(m[1]) == 8:
        base = m[0]
    tiers = PRICES.get(base) or PRICES.get(model or "")
    if not tiers:
        return FALLBACK_PRICE
    if day:
        for until, rates in tiers:
            if until is None or day <= until:
                return rates
    return tiers[-1][1]


def db_path() -> Path:
    return paths.state_dir() / "token-usage.db"




def _iter_rollups(store_root, machine_uuid=None, *, per_machine=False):
    'Yield (machine, payload) for every readable rollup file. A corrupt or\n    half-written file is skipped, never fatal — same discipline as load_fleet.'
    root = Path(store_root) / "machines"
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for d in dirs:
        if per_machine and machine_uuid and d.name != machine_uuid:
            continue
        for f in sorted((d / "token-usage").glob("*.json")):
            try:
                payload = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("usage"):
                yield d.name, payload


def _rows(block):
    'Rollup blocks are {columns: [...], rows: [[...]]} — bare arrays, because\n    at a few thousand rows a month the repeated keys were most of the bytes.'
    cols = (block or {}).get("columns") or []
    for r in (block or {}).get("rows") or []:
        yield dict(zip(cols, r))


def _cutoff(since_days: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - max(1, since_days) * 86400))


def _blank():
    return {"requests": 0, "input": 0, "cache_write": 0, "cache_read": 0,
            "output": 0, "cost_usd": 0.0, "cost_input_usd": 0.0}


def _add(acc, r):
    acc["requests"] += int(r.get("requests") or 0)
    acc["input"] += int(r.get("input") or 0)
    acc["cache_write"] += int(r.get("cache_w5") or 0) + int(r.get("cache_w1h") or 0)
    acc["cache_read"] += int(r.get("cache_read") or 0)
    acc["output"] += int(r.get("output") or 0)
    acc["cost_usd"] += float(r.get("cost_usd") or 0.0)
    
    
    
    acc["cost_input_usd"] += float(r.get("cost_input_usd") or 0.0)


def _fleet(store_root, machine_uuid, *, per_machine, since_days, project, group_by):
    cut = _cutoff(since_days)
    totals, groups, tools = _blank(), {}, {}
    machines, first_day = set(), None
    for machine, payload in _iter_rollups(store_root, machine_uuid, per_machine=per_machine):
        for r in _rows(payload.get("usage")):
            day = r.get("day") or ""
            if day < cut:
                continue
            if project and r.get("project") != project:
                continue
            machines.add(machine)
            first_day = day if first_day is None else min(first_day, day)
            _add(totals, r)
            if group_by == "sidechain":
                key = "subagent" if r.get("sidechain") else "main"
            elif group_by == "tool":
                key = None
            else:
                key = r.get(group_by) or "(none)"
            if key is not None:
                _add(groups.setdefault(key, _blank()), r)
        for r in _rows(payload.get("tools")):
            day = r.get("day") or ""
            if day < cut or (project and r.get("project") != project):
                continue
            t = tools.setdefault(r.get("tool") or "?",
                                 {"calls": 0, "result_bytes": 0, "est_tokens": 0, "errors": 0})
            for k in ("calls", "result_bytes", "est_tokens", "errors"):
                t[k] += int(r.get(k) or 0)
    return totals, groups, tools, sorted(machines), first_day




def _connect():
    p = db_path()
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _local_sessions(con, cut, project, limit):
    q = ("SELECT root_session AS k, COUNT(*) requests, SUM(cost_usd) cost_usd, "
         "  MIN(day) first_day, MAX(day) last_day, SUM(output_tokens) output, "
         "  SUM(cache_read) cache_read, "
         "  (SELECT project FROM requests r2 WHERE r2.root_session=r.root_session "
         "     GROUP BY project ORDER BY COUNT(*) DESC LIMIT 1) project, "
         "  (SELECT model FROM requests r3 WHERE r3.root_session=r.root_session "
         "     GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1) model "
         "FROM requests r WHERE day >= ?")
    args = [cut]
    if project:
        q += " AND project = ?"
        args.append(project)
    q += " GROUP BY root_session ORDER BY cost_usd DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(q, args)]


def _amortized(con, cut, project, limit):
    'Marginal cost per tool: the cache-write that admits the result plus a\n    cache-read on every request still behind it in that transcript.\n\n    `tail` comes from files.req_count - req_ordinal rather than a stored number,\n    because the tail keeps growing after the row is written; freezing it at\n    collect time would bake in whatever the session length happened to be.'
    
    
    q = ("SELECT c.tool tool, c.model model, c.day day, COUNT(*) calls, "
         "  COALESCE(SUM(r.est_tokens),0) est_tokens, "
         "  COALESCE(SUM(r.est_tokens * (1.25 + 0.1 * MAX(0, f.req_count - c.req_ordinal - 1))),0) w "
         "FROM tool_calls c "
         "LEFT JOIN tool_results r USING(tool_use_id) "
         "JOIN files f ON f.path = c.src "
         "WHERE c.day >= ?")
    args = [cut]
    if project:
        q += " AND c.project = ?"
        args.append(project)
    q += " GROUP BY c.tool, c.model, c.day"
    agg = {}
    for r in con.execute(q, args):
        a = agg.setdefault(r["tool"], {"tool": r["tool"], "calls": 0, "est_tokens": 0, "_w": 0.0})
        a["calls"] += r["calls"]
        a["est_tokens"] += r["est_tokens"]
        a["_w"] += (r["w"] or 0.0) * _price(r["model"], r["day"])[0] / 1e6
    total = sum(a["_w"] for a in agg.values()) or 1.0
    rows = sorted(agg.values(), key=lambda a: -a["_w"])[:limit]
    return rows, total




def token_report(store, *, group_by="project", since_days=30, project=None,
                 limit=25, per_machine=False, machine_uuid=None) -> dict[str, Any]:
    'Where the token spend went, over the last `since_days`.\n\n    Returns a plain dict[str, Any] deliberately: each return\n    statement\'s shape genuinely differs (an error pointer, the no-data pointer,\n    the full report), and a rigid return type would claim a common shape this\n    function does not have. Callers narrow the specific keys they read.\n\n    Fleet-merged from the per-machine rollups by default. `group_by="session"`\n    and the amortized tool block come from the local database only, and say so.'
    if group_by not in GROUPS:
        return {"error": f"group_by must be one of {', '.join(GROUPS)}"}
    cut = _cutoff(since_days)

    totals, groups, tools, machines, first_day = _fleet(
        store.root, machine_uuid, per_machine=per_machine,
        since_days=since_days, project=project, group_by=group_by)

    if not totals["requests"]:
        return {
            "window_days": since_days,
            "requests": 0,
            "source": "none",
            "note": ("No rollups found. The collector has not run on any machine yet — "
                     "run `~/.agent-context/global/scripts/token-usage-collect.py` (it also installs "
                     "nothing; the SessionEnd hook and daily cron call the same script)."),
        }

    tot_cost = totals["cost_usd"] or 1.0
    out_groups = []
    if group_by != "tool":
        for k, v in sorted(groups.items(), key=lambda kv: -kv[1]["cost_usd"])[:limit]:
            out_groups.append({
                group_by: k,
                "requests": v["requests"],
                "cost_usd": round(v["cost_usd"], 2),
                "share_pct": round(100.0 * v["cost_usd"] / tot_cost, 1),
                "output_tokens": v["output"],
                "cache_read_tokens": v["cache_read"],
            })

    tool_total = sum(t["est_tokens"] for t in tools.values()) or 1
    out_tools = [{
        "tool": k,
        "calls": v["calls"],
        "result_tokens": v["est_tokens"],
        "context_share_pct": round(100.0 * v["est_tokens"] / tool_total, 1),
        "errors": v["errors"],
    } for k, v in sorted(tools.items(), key=lambda kv: -kv[1]["est_tokens"])[:limit]]

    days = since_days
    if first_day:
        with contextlib.suppress(ValueError):
            days = min(since_days, round(
                (time.time() - time.mktime(time.strptime(first_day, "%Y-%m-%d"))) / 86400) + 1)

    report = {
        "window_days": since_days,
        "days_with_data": days,
        "enough_evidence": days >= _MIN_DAYS,
        "source": "this machine's rollups" if per_machine else "fleet rollups",
        "machines_reporting": len(machines),
        "machines": machines,
        "project_filter": project,
        "totals": {
            "requests": totals["requests"],
            "cost_usd": round(totals["cost_usd"], 2),
            
            
            
            "cost_input_usd": round(totals["cost_input_usd"], 2),
            "input_tokens": totals["input"],
            "cache_write_tokens": totals["cache_write"],
            "cache_read_tokens": totals["cache_read"],
            "output_tokens": totals["output"],
        },
        "group_by": group_by,
        "groups": out_groups,
        "tools_by_context": out_tools,
    }

    con = None
    with contextlib.suppress(Exception):
        con = _connect()
    if con is not None:
        with contextlib.suppress(Exception):
            if group_by == "session":
                report["groups"] = [{
                    "session": r["k"][:8],
                    "project": (r["project"] or "").rsplit("/", 1)[-1],
                    "model": r["model"],
                    "requests": r["requests"],
                    "cost_usd": round(r["cost_usd"] or 0.0, 2),
                    "days": f"{r['first_day']}..{r['last_day']}",
                    "output_tokens": r["output"],
                } for r in _local_sessions(con, cut, project, limit)]
                report["source"] = "local database (this machine only)"
            rows, marginal_total = _amortized(con, cut, project, limit)
            
            
            
            
            
            
            input_spend = totals["cost_input_usd"] or totals["cost_usd"]
            report["tools_by_amortized_context"] = [{
                "tool": r["tool"],
                "calls": r["calls"],
                "result_tokens": r["est_tokens"],
                "context_share_pct": round(100.0 * r["_w"] / marginal_total, 1),
                "attributed_usd": round(input_spend * r["_w"] / marginal_total, 2),
            } for r in rows]
            report["amortized_note"] = (
                "Rank by context_share_pct; attributed_usd is normalized onto real "
                "spend, not a billed figure — the tails overlap, so the raw marginal "
                "sum exceeds the billed total by design.")
        con.close()

    report["note"] = (
        ("Counts are this machine's only. " if per_machine else
         "Counts are merged across every machine's rollups at "
         "machines/<uuid>/token-usage/. ") +
        f"Zeros mean little until days_with_data >= {_MIN_DAYS}.")
    return report
