
'The always-loaded bootstrap index: row encoding, budgets, prune/cold/stale reports, usage report.\n\n`fstools` re-exports everything here, so callers and tests import `fstools as T`.'
from __future__ import annotations

import re
from datetime import UTC

from . import usage






_TYPE_ABBR = {"feedback": "f", "project": "p", "reference": "r", "user": "u"}




_MEM_ROWS_BUDGET = 14_000



_ALWAYS_INSTR_BUDGET = 15_000


_DUP_MIN_BYTES = 600







_BOOTSTRAP_CEILING = 60_000




_STABILIZED_AFTER_DAYS = 30
_STABILIZED_RE = re.compile(
    r"\b(landed|deployed|shipped|complete|completed|merged|fixed|"
    r"resolved|retired|remediated)\b", re.IGNORECASE)



_UNFINISHED_RE = re.compile(
    r"\b(pending|not deployed|undeployed|outstanding|in progress|wip|todo|blocked|"
    r"awaiting|not yet)\b", re.IGNORECASE)




_LAZY_FIX = "upsert_memory(slug, load_behavior='lazy')"
_LAZY_FIX_CAVEAT = ("deletion is a human call, not this check's: age alone can't tell a "
                     "settled invariant from a dead one")



_PAYS_REFRAIN = "every session on every machine pays this"

_MEM_LEGEND = (
    "Rows: `<type> <slug> — <description>`, one per line; type f=feedback p=project "
    "r=reference u=user. `lazy` lists slugs only — those memories are not loaded but are "
    "addressable: get_memory(slug[, project]). Read any body with get_memory; search bodies "
    "with search_all(query, kind=\"memory\"[, project])."
)


def _mem_row(e):
    'One memory as a single index line.'
    t = _TYPE_ABBR.get(e.get("memory_type") or "", "?")
    return f"{t} {e.get('slug')} — {e.get('description') or ''}".rstrip()


def _is_lazy(e):
    
    
    return e.get("load_behavior") != "always"












_IRREVERSIBLE = re.compile(
    r"push-to-deploy|\bdeploys?\b|\bdeployment\b|production|irreversible|"
    r"destructive|cannot be undone|rotat(e|ing|ion)|wipe|data loss", re.IGNORECASE)


def _guards_irreversible(e):
    return bool(_IRREVERSIBLE.search(
        f"{e.get('slug') or ''} {e.get('description') or ''}"))














_INCIDENT = re.compile(
    r"GOTCHA|\bTRAP\b|\bincident\b|\boutage\b|\bregression\b|\bhazard\b|"
    r"\bproduction\b|\bobs #\d+|\bpost-?mortem\b|\bbroke\b|\bsilently\b", re.IGNORECASE)


def _records_an_incident(e):
    return bool(_INCIDENT.search(e.get("body") or ""))


def _must_stay_loaded(e):
    'A memory no age- or coldness-based check may propose demoting.'
    return _guards_irreversible(e) or _records_an_incident(e)


def _row_cost(e):
    'Bootstrap cost of a loaded memory: its rendered row plus the newline.'
    return len(_mem_row(e)) + 1


def _roster_cost(e):
    'Bootstrap cost of a tiered-out memory: its slug plus the separator.'
    return len(e.get("slug") or "") + 1


def _memory_cost(e):
    'What one memory costs the bootstrap, whichever tier it is in.\n\n    Single definition on purpose. _index_health and check_integrity both report this\n    number, and a report drifting from the thing it measures is the exact failure that\n    let the index grow unnoticed in the first place.'
    return _roster_cost(e) if _is_lazy(e) else _row_cost(e)


def _mem_block(entries, budget=_MEM_ROWS_BUDGET):
    "Split one scope's memories into loaded rows plus a slug-only roster.\n\n    A `lazy` memory is rostered by slug rather than dropped. The slug is the cheapest\n    possible pointer (~35 chars against ~130 for a full row) and it preserves the one\n    property that matters: the agent can SEE the memory exists. Dropping it entirely\n    would leave knowledge reachable only by guessing a search term for something you\n    don't know is there — the failure mode that makes aggressive tiering dangerous.\n\n    If the loaded rows still exceed `budget`, the least-recently-updated ones demote\n    into that same roster. The index therefore degrades in fidelity, never in reach."
    loaded, rostered = [], []
    for e in entries:
        (rostered if _is_lazy(e) else loaded).append(e)
    
    loaded.sort(key=lambda e: (e.get("updated_at") or "", e.get("slug") or ""), reverse=True)
    rows, used, demoted = [], 0, []
    for e in loaded:
        r = _mem_row(e)
        if used + len(r) + 1 > budget:
            demoted.append(e)
            continue
        rows.append(r)
        used += len(r) + 1
    block = {"rows": "\n".join(sorted(rows))}
    roster = sorted(s for s in ((e.get("slug") or "") for e in rostered + demoted) if s)
    if roster:
        block["lazy"] = " ".join(roster)
    if demoted:
        block["over_budget"] = (
            f"{len(demoted)} memories exceeded the {budget}-char index budget and were demoted to "
            f"`lazy` above (slug only). Prune stabilized worklogs or tier narrow ones out with "
            f"{_LAZY_FIX}. See check_integrity().bootstrap_footprint.")
    return block


def _scope_memories(store, project):
    "Memories at exactly `project`'s scope (no global fallthrough)."
    scope = store._scope_for(project)
    return [e for e in store.entities.values()
            if e.get("type") == "memory" and e.get("scope") == scope]


def _prune_candidates(store, scopes=None):
    'Loaded `project` memories that read as finished worklogs and have gone quiet.\n\n    Both conditions are required. A stabilized-sounding description on a memory\n    touched this week is live work, not an archive — flagging it would train agents\n    to ignore this list. Report-only by design: deleting a memory is destructive and\n    stays a human decision; this only surfaces the backlog so the prune rule stops\n    depending on someone remembering it.'
    from datetime import datetime, timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=_STABILIZED_AFTER_DAYS)).strftime("%Y-%m-%d")
    out = []
    for e in store.entities.values():
        if e.get("type") != "memory" or e.get("memory_type") != "project":
            continue
        if _is_lazy(e):
            continue  
        if scopes is not None and e.get("scope") not in scopes:
            continue
        if str(e.get("updated_at") or "")[:10] >= cutoff:
            continue
        desc = e.get("description") or ""
        if _UNFINISHED_RE.search(desc):
            continue  
        if not _STABILIZED_RE.search(desc):
            continue
        if _must_stay_loaded(e):
            continue   
        out.append({"slug": e.get("slug"), "scope": e.get("scope"),
                    "updated_at": str(e.get("updated_at") or "")[:10]})
    return sorted(out, key=lambda d: (d["updated_at"], d["slug"]))


def _stale_always_loaded(store, scopes=None):
    'Always-loaded `project` memories untouched for _STABILIZED_AFTER_DAYS — whatever\n    their description says.\n\n    The weaker sibling of `_prune_candidates`, which additionally wants the description\n    to sound finished. A `project` memory is a live worklog by definition; a month of silence is evidence enough to ask, and listing the\n    description lets the next audit tier or delete each one in a single call.'
    from datetime import datetime, timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=_STABILIZED_AFTER_DAYS)).strftime("%Y-%m-%d")
    out = []
    for e in store.entities.values():
        if e.get("type") != "memory" or e.get("memory_type") != "project" or _is_lazy(e):
            continue
        if scopes is not None and e.get("scope") not in scopes:
            continue
        if str(e.get("updated_at") or "")[:10] >= cutoff:
            continue
        if _must_stay_loaded(e):
            continue   
        out.append({"slug": e.get("slug"), "scope": e.get("scope"),
                    "updated_at": str(e.get("updated_at") or "")[:10],
                    "description": (e.get("description") or "")[:_DESC_MAX],
                    "fix": f"{_LAZY_FIX}: {_LAZY_FIX_CAVEAT}"})
    return sorted(out, key=lambda d: (d["updated_at"], d["slug"]))


_COLD_MIN_DAYS = 30


def _cold_always_loaded(store):
    'Always-loaded memories that no session has read, or even matched in a search,\n    for a full observation window.\n\n    This is the demotion queue for the tiering rule. Tiering is decided once, at write\n    time, on a guess about future usefulness, and every session on every machine pays\n    for that guess. `_prune_candidates` catches worklogs that say they are finished;\n    this catches the ones nothing says anything about.\n\n    Report-only, and conservative:\n      * silent until telemetry has been collecting for the full window, or a freshly\n        instrumented machine would declare the entire index cold;\n      * a memory written inside the window is skipped for the same reason;\n      * a search hit counts as use, not just a body fetch, so something that is merely\n        findable-and-relevant is not called dead.\n\n    "Never read" is evidence, not proof: a memory can earn its slot purely by being\n    visible in the bootstrap index. So this proposes, and a human (or the audit loop)\n    decides.'
    from datetime import datetime, timedelta
    fleet = usage.load_fleet(store.root)    
    since = fleet.since
    now = datetime.now(UTC)
    if (now.timestamp() - since) < _COLD_MIN_DAYS * 86400:
        return []                       
    cutoff = (now - timedelta(days=_COLD_MIN_DAYS)).strftime("%Y-%m-%d")
    out = []
    for e in store.entities.values():
        if e.get("type") != "memory" or _is_lazy(e):
            continue
        if str(e.get("updated_at") or "")[:10] >= cutoff:
            continue
        st = fleet.stats("memory", e.get("scope"), e.get("slug"))
        if st["reads"] or st["hits"]:
            continue
        
        
        
        
        if _must_stay_loaded(e):
            continue
        out.append({"slug": e.get("slug"), "scope": e.get("scope"),
                    "updated_at": str(e.get("updated_at") or "")[:10],
                    "row_bytes": _row_cost(e),
                    "fix": _LAZY_FIX})
    return sorted(out, key=lambda d: (-d["row_bytes"], d["slug"]))


def usage_report(store, project=None, limit=40, per_machine=False):
    "What the always-loaded index costs against what it is used for.\n\n    Sorted coldest-first, so the top of the list is the bytes every session\n    pays with the least evidence of return. Counts are the fleet's: every machine's\n    synced snapshot plus this machine's live counters — and `tracking_days` runs from\n    the oldest machine's window start; it is the honest qualifier on every zero in\n    here, so read it before acting on one. `per_machine=True` narrows to this machine."
    view = usage.load_fleet(store.root, per_machine=per_machine)
    since, days = view.since, view.days
    scope = store._scope_for(project) if project else None
    rows, cold_bytes, total_bytes = [], 0, 0
    for e in store.entities.values():
        if e.get("type") != "memory":
            continue
        sc = e.get("scope")
        if scope is not None and sc not in ("global", scope):
            continue
        st = view.stats("memory", sc, e.get("slug"))
        cost = _memory_cost(e)
        total_bytes += cost
        if not _is_lazy(e) and not st["reads"] and not st["hits"]:
            cold_bytes += cost
        rows.append({"slug": e.get("slug"), "scope": sc,
                     "tier": "lazy" if _is_lazy(e) else "always",
                     "reads": st["reads"], "hits": st["hits"],
                     "bootstrap_bytes": cost})
    rows.sort(key=lambda r: (r["reads"], r["hits"], -r["bootstrap_bytes"]))
    return {
        "tracking_days": round(days, 1),
        "tracking_since": since,
        "enough_evidence": days >= _COLD_MIN_DAYS,
        "machines_reporting": len(view.machines),
        "machines": view.machines,
        "memories": len(rows),
        "always_loaded_bytes": total_bytes,
        "cold_always_loaded_bytes": cold_bytes,
        
        
        "search_misses": usage.misses(),
        "note": (("Counts are this machine's only. " if per_machine else
                  "Counts are summed across every machine's synced snapshot "
                  "(machines/<uuid>/usage.json) plus this machine's live counters; "
                  "tracking_since is the oldest machine's window start. ")
                 + "A search hit counts as use. Zeros mean nothing until tracking_days >= "
                 f"{_COLD_MIN_DAYS}."),
        "coldest_first": rows[:limit],
    }


def _index_health(store, project):
    "Index hygiene for this session's scopes — omitted entirely when clean.\n\n    check_integrity() reports these into the daemon's startup log, which nothing able\n    to fix them reads. A compact count in the bootstrap hands the finding to the actor\n    that can act on it, and costs nothing in the clean case. Scoped to the session's own scopes so an\n    agent is never nagged about a project it isn't in.\n\n    Counts and slugs only — never the prose. This block exists to shrink the\n    payload; it must not become a second thing bloating it."
    scopes = {"global"} | ({store._scope_for(project)} if project else set())
    mems = [e for e in store.entities.values()
            if e.get("type") == "memory" and e.get("scope") in scopes]
    loaded = [e for e in mems if not _is_lazy(e)]

    long_desc = sorted(e.get("slug") for e in loaded if len(e.get("description") or "") > _DESC_MAX)
    over = sorted(sc for sc in scopes
                  if sum(_row_cost(e) for e in loaded if e.get("scope") == sc) > _MEM_ROWS_BUDGET)
    prunable = [d["slug"] for d in _prune_candidates(store, scopes)]
    total = (sum(len(e.get("body") or "") for e in store.get_instructions(project, "always"))
             + sum(_memory_cost(e) for e in mems))

    h = {}
    if long_desc:
        h["long_descriptions"] = long_desc[:8]
    if over:
        h["over_budget_scopes"] = over
    if prunable:
        h["prune_candidates"] = prunable[:8]
    if total > _BOOTSTRAP_CEILING:
        h["over_ceiling"] = f"{total} chars vs {_BOOTSTRAP_CEILING} ceiling"
    if not h:
        return None
    h["fix"] = (f"{_PAYS_REFRAIN.capitalize()}. Descriptions are a "
                f"≤{_DESC_MAX}-char relevance hook (upsert_memory(slug, description=...)); "
                f"stabilized or narrow worklogs get {_LAZY_FIX} ({_LAZY_FIX_CAVEAT}). "
                "check_integrity() for detail.")
    return h


def _index_budget_warning(store, project):
    "Warn at write time when a scope's always-loaded index has outgrown its budget.\n\n    Each individual write looks free, and the cost only shows up spread across every\n    future session. Reporting the running total on the write that crosses the line\n    makes that drift visible."
    mems = [e for e in _scope_memories(store, project) if not _is_lazy(e)]
    used = sum(_row_cost(e) for e in mems)
    if used <= _MEM_ROWS_BUDGET:
        return None
    return (f"always-loaded memory index for scope '{project or 'global'}' is now {used} chars across "
            f"{len(mems)} memories (budget {_MEM_ROWS_BUDGET}), and {_PAYS_REFRAIN}. Prune "
            f"stabilized worklogs (delete_entity('memory', slug)) or tier narrow ones out with "
            f"{_LAZY_FIX}.")


def _always_instruction_bytes(store, scope):
    'Bytes of always-loaded instruction body at one scope — what the bootstrap pays.'
    return sum(len(e.get("body") or "")
               for e in store.entities.values()
               if e.get("type") == "instruction"
               and e.get("scope") == scope
               and e.get("load_behavior") == "always")


def _instruction_budget_warning(store, scope, before=None):
    "Warn at write time when a scope's always-loaded instructions are over budget.\n\n    Memory rows warn the same way (`_index_budget_warning`); instruction bodies are the\n    more expensive half, so each edit gets a signal.\n\n    Pass `before` (the same measurement taken ahead of the write) to say whether this\n    write crossed the line or merely stayed past it. A warning, never a block: the\n    trim that fixes it is a separate judgment call about which prose is a rule."
    used = _always_instruction_bytes(store, scope)
    if used <= _ALWAYS_INSTR_BUDGET:
        return None
    over = used - _ALWAYS_INSTR_BUDGET
    crossed = before is not None and before <= _ALWAYS_INSTR_BUDGET
    lead = "this write PUT" if crossed else "this write left"
    delta = f", was {before}" if before is not None and before != used else ""
    return (f"{lead} always-loaded instructions for scope '{scope}' at {used} bytes{delta} — "
            f"{over} over the {_ALWAYS_INSTR_BUDGET} budget. {_PAYS_REFRAIN.capitalize()}, and "
            f"instruction bodies are never trimmed by the runtime. Move rationale into a lazy doc "
            f"and leave a pointer; check_integrity() for the per-scope breakdown.")


_DESC_MAX = 140
