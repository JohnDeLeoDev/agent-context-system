
'Cross-cutting: search_all, check_integrity, version history, machine paths.\n\n`fstools` re-exports everything here, so callers and tests import `fstools as T`.'
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import time
from typing import Any

from . import graph, usage
from . import projects as P
from .audit import _audit_index, _parse_when, list_audit_observations
from .index import (
    _ALWAYS_INSTR_BUDGET,
    _BOOTSTRAP_CEILING,
    _COLD_MIN_DAYS,
    _DESC_MAX,
    _DUP_MIN_BYTES,
    _MEM_ROWS_BUDGET,
    _cold_always_loaded,
    _is_lazy,
    _prune_candidates,
    _roster_cost,
    _row_cost,
    _stale_always_loaded,
)
from .projects import _write_project_marker
from .refs import _PROSE_TYPES, _known_targets, _resolve_refs
from .store import NATURAL_KEY, parse_frontmatter, stable_uuid








OTHER_KINDS = ("skill", "command", "hook", "script")
OTHER_MAX = 3
OTHER_DESC_MAX = 100


def _other_row(r):
    desc = " ".join(str(r["description"] or "").split())
    if len(desc) > OTHER_DESC_MAX:
        desc = graph._cut(desc, OTHER_DESC_MAX - 1) + "…"
    return {"entity_type": r["entity_type"], "name": r["name"], "description": desc,
            **{k: r[k] for k in ("project", "workspace") if k in r}, "group": "other"}


SEARCH_KINDS = ("memory", "doc")




SEARCH_WEAK_SCORE = 5.0


def _record_miss(query, project, rows):
    top = -float(rows[0].get("rank") or 0.0) if rows else 0.0
    if top < SEARCH_WEAK_SCORE:
        usage.record_miss(query, project, top, len(rows))


def search_all(store, query, limit=20, project=None, kind=None):
    "Memories and docs ranked together, then the other group. `kind` narrows to one of\n    SEARCH_KINDS and returns that kind's own row shape (the memory rows carry `slug`, the\n    doc rows `path` and `title`), with no other group: this is what search_memories and\n    search_docs returned before they were folded in (policy). `project` limits every row to\n    global plus that project's scope."
    if kind is not None:
        if kind not in SEARCH_KINDS:
            return {"error": f"kind must be one of {', '.join(SEARCH_KINDS)}, got {kind!r}"}
        from . import docs, memory
        search = memory.search_memories if kind == "memory" else docs.search_docs
        found = search(store, query, project, limit)
        _record_miss(query, project, found)
        return found
    rows = store.search(query, types=SEARCH_KINDS, project=project, limit=limit)
    _record_miss(query, project, rows)
    other = store.search(query, types=OTHER_KINDS, project=project, limit=OTHER_MAX)
    for r in rows + other:
        usage.record(r["entity_type"], r["scope"], r["name"], hit=True)
    return rows + [_other_row(r) for r in other]








_MARKUP_TAG = re.compile(
    r"</\s*(?:antml:)?(?:observation|evidence|note|notes|resolution_note|machine"
    r"|severity|scope|project|recurred|observation_id|invoke|parameter"
    r"|function_calls)\s*>", re.IGNORECASE)

_MARKUP_FIELDS = ("observation", "evidence", "notes", "resolution_note")










_ARCHIVE_SEGMENTS = ("archive",)


def _is_archived(entity):
    'Is this entity a frozen record rather than live knowledge?'
    return graph.is_archived(entity)


def _archived_doc(ref):
    'Is this finding row, or "<kind> <key>" name, an archived doc?'
    if isinstance(ref, str):
        kind, _, key = ref.partition(" ")
    else:
        kind, key = ref.get("type"), ref.get("key")
    return kind == "doc" and _is_archived({"path": key})


def _orphans(store):
    'Memories and docs with no backlinks and no reads (context graph T5). Silent until\n    usage covers a full observation window, the gate usage_report calls enough_evidence:\n    a shorter window would call most of the store unread.'
    fleet = usage.load_fleet(store.root)
    if fleet.days < _COLD_MIN_DAYS:
        return []
    back = graph.graph_for(store).back
    rows = []
    for uid, e in list(store.entities.items()):
        kind = e.get("type")
        if kind not in ("memory", "doc") or back.get(uid) or _is_archived(e):
            continue
        key = str(e.get(NATURAL_KEY[kind]) or "")
        if kind == "doc" and not key.lower().endswith(".md"):
            continue
        if fleet.stats(kind, e.get("scope") or "global", key)["reads"]:
            continue
        rows.append({"type": kind, "scope": e.get("scope"), "key": key})
    return sorted(rows, key=lambda r: (r["type"], r["scope"] or "", r["key"]))




_FORK_LEN_RATIO_MIN = 0.1





_FORK_SIMILARITY_MIN = 0.6


def _is_fork(global_body, project_body):
    'True when `project_body` is substantially the same text as `global_body` —\n    a genuine fork, not two entities that happen to share a kind and key.'
    a, b = len(global_body), len(project_body)
    if a == 0 or b == 0:
        return False
    if min(a, b) / max(a, b) < _FORK_LEN_RATIO_MIN:
        return False
    return difflib.SequenceMatcher(None, global_body, project_body).ratio() >= _FORK_SIMILARITY_MIN


def _observations_with_markup(store):
    out = []
    for o in list_audit_observations(store, status=None):
        hit = [f for f in _MARKUP_FIELDS
               if isinstance(o.get(f), str) and _MARKUP_TAG.search(o[f])]
        if hit:
            out.append({"id": o.get("id"), "fields": hit, "status": o.get("status"),
                        "fix": "the record's text swallowed a tool call; re-file the "
                               "affected field(s) with update_audit_observation, or "
                               "repair the JSON and say so in a note"})
    return out


def _observation_ids_reused(store):
    'An id that names two different observations — one active, one archived.\n\n    An id is the only handle a record has: prose cites "#290", the digest keys on it,\n    and resolve_audit_observation takes nothing else. Two records sharing one means a\n    resolution can land on the wrong defect.\n\n    Reported, not repaired, like everything else here: choosing which of two\n    real observations keeps the number is a human\'s call.'
    from .audit import _audit_dir, _audit_read_dir
    active = {int(o.get("id", 0)): o for o in _audit_read_dir(_audit_dir(store))}
    out = []
    for o in _audit_read_dir(_audit_dir(store, archive=True)):
        oid = int(o.get("id", 0))
        if oid not in active:
            continue
        out.append({
            "id": oid,
            "active": _first_words(active[oid].get("observation")),
            "archived": _first_words(o.get("observation")),
            "fix": "two unrelated records share this number. Renumber the ACTIVE one "
                   "to a free id (the archived record is the one prose and resolution "
                   "notes already cite) and correct any reference to it.",
        })
    return sorted(out, key=lambda d: d["id"])


def _first_words(text, limit=90):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


_FILE_KINDS = ("memory", "doc", "instruction", "skill", "command", "agent_definition")


def _has_store_uuid(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return "uuid" in parse_frontmatter(fh.read())[0]
    except OSError:
        return False


def _hand_moves_and_renames(store):
    'What editing the store as an Obsidian vault can leave behind, report-only.\n\n    - `path_derived_identity`: a uuid-less note indexed by its path (store.py\n      `_load_file`). Its first MCP write adds the frontmatter; the index flag can\n      outlive that write until the next reload, so the file is the authority.\n    - `uuid_scope_mismatch`: frontmatter uuid is not stable_uuid(type, scope, key),\n      the mark of a file moved across scope directories (or a key edited by hand).\n      Reads keep working; the next MCP write re-derives the uuid. `from_scope` names\n      the scope the uuid was minted in when one matches.\n    - `filename_key_mismatch`: the file is not where _file_for puts that key, the\n      mark of a rename. Resolution keeps using frontmatter; an MCP write moves it back.'
    scopes = {sc for (_, sc, _) in store.by_key} | {"global"}
    for top, prefix in (("projects", "project:"), ("workspaces", "ws:")):
        d = os.path.join(store.root, top)
        if os.path.isdir(d):
            scopes |= {prefix + n for n in os.listdir(d)}
    derived, moved, renamed = [], [], []
    for e in store.entities.values():
        typ = e.get("type")
        nk = NATURAL_KEY.get(typ or "")
        key = e.get(nk) if nk else None
        path, scope = e.get("_path"), e.get("scope")
        if key is None or not path or not scope:
            continue
        row = {"type": typ, "key": key, "scope": scope,
               "path": os.path.relpath(path, store.root)}
        if e.get("_path_derived") and not _has_store_uuid(path):
            derived.append({**row, "fix": "indexed from its path; any MCP write to it "
                                          "adds the store frontmatter"})
            continue
        if e.get("uuid") != stable_uuid(typ, scope, key):
            hit = dict(row)
            origin = next((s for s in sorted(scopes) if s != scope
                           and stable_uuid(typ, s, key) == e.get("uuid")), None)
            if origin:
                hit["from_scope"] = origin
            hit["fix"] = ("moved or re-keyed by hand; move it back, or rewrite it through "
                          "MCP at its new scope so the uuid is re-derived")
            moved.append(hit)
        if typ in _FILE_KINDS:
            expected = os.path.relpath(store._file_for(typ, scope, key), store.root)
            if expected != row["path"]:
                renamed.append({**row, "expected": expected,
                                "fix": "renamed by hand; rename it back to `expected`, "
                                       "or its next MCP write moves it there"})
    order = lambda x: (x["path"], x["type"])  
    return sorted(derived, key=order), sorted(moved, key=order), sorted(renamed, key=order)







_EMPHATIC = re.compile(r"(?<![A-Za-z0-9_'])(DO NOT|DON'T|NEVER|ALWAYS|MUST|IMPORTANT|CRITICAL|"
                       r"ONLY|EVERY|NOT|NO|STOP|WARNING|REQUIRED|MANDATORY)(?![A-Za-z0-9_'])")
_OPERATIVE_KINDS = ("instruction", "agent_definition", "skill", "command")

_CODE_FENCE = re.compile(r"(```|~~~).*?(?:\1|\Z)", re.DOTALL)
_CODE_SPAN = re.compile(r"`[^`\n]*`")



_DANGLING_WORDS = frozenset(("the", "a", "an", "to", "and", "of", "or", "for", "with", "by",
                             "is", "that", "from", "via", "as", "at"))
_TRAILING_WORD = re.compile(r"([A-Za-z]+)$")



_OBS_SPENT_DAYS = 30
_OBS_SPENT_STATUSES = ("resolved", "discarded")
_HANDOFF_SPENT_DAYS = 7
_HANDOFF_SPENT = re.compile(r"^[*][*]Status:[*][*] *(consumed|stale)(?![A-Za-z0-9_])", re.MULTILINE)
_STATUS_LINE = re.compile(r"^[*][*]Status:[*][*](.*)$", re.MULTILINE)
_STATUS_DATE = re.compile(r"(?<![0-9])([0-9]{4}-[0-9]{2}-[0-9]{2})(?![0-9])")



_MEMORY_MAX_BYTES = 6000




_AGENT_PARA_MIN = 80
_AGENT_SHARED_STEMS = ("**First call:", "**No narration")


def _entity_key(e):
    return e.get("slug") or e.get("path") or e.get("name") or e.get("title")


def _emphatic_capitals(store):
    rows = []
    for e in store.entities.values():
        t = e.get("type")
        if t == "memory":
            if e.get("load_behavior") != "always":
                continue
        elif t not in _OPERATIVE_KINDS:
            continue
        if _is_archived(e):
            continue
        if e.get("upstream"):
            
            
            continue
        text = f"{e.get('description') or ''}\n{e.get('body') or ''}"
        prose = _CODE_SPAN.sub("", _CODE_FENCE.sub("", text))
        words = {}
        for m in _EMPHATIC.finditer(prose):
            words[m.group(1)] = words.get(m.group(1), 0) + 1
        if words:
            rows.append({"kind": t, "scope": e.get("scope"), "key": _entity_key(e),
                         "words": words,
                         "fix": "state the rule and its reason in plain case; a hook "
                                "enforces what must hold (authoring.md)"})
    
    try:
        with open(os.path.join(store.root, "AGENTS.md"), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    words = {}
    for m in _EMPHATIC.finditer(_CODE_SPAN.sub("", _CODE_FENCE.sub("", text))):
        words[m.group(1)] = words.get(m.group(1), 0) + 1
    if words:
        rows.append({"kind": "root_file", "scope": "global", "key": "AGENTS.md",
                     "words": words,
                     "fix": "state the rule and its reason in plain case; edit the file in "
                            "a store worktree (authoring.md)"})
    rows.sort(key=lambda r: (r["kind"], r["scope"] or "", str(r["key"])))
    return rows


def _truncated_descriptions(store):
    rows = []
    for e in store.entities.values():
        d = e.get("description")
        if not isinstance(d, str) or _is_archived(e):
            continue
        d = d.rstrip()
        w = _TRAILING_WORD.search(d)
        if d.endswith(("…", "...")) or (w and w.group(1).lower() in _DANGLING_WORDS):
            rows.append({"kind": e.get("type"), "scope": e.get("scope"), "key": _entity_key(e),
                         "description": d,
                         "fix": "finish the sentence within 140 characters"})
    rows.sort(key=lambda r: (r["kind"] or "", r["scope"] or "", str(r["key"])))
    return rows


def _age_days(stamp, now):
    ts = _parse_when(stamp)
    return None if ts is None else int((now - ts) // 86400)


def _handoff_age(e, now):
    "Days since a handoff was spent: the newest date its Status line gives ahead of\n    the Opened field, else `updated_at`, which any metadata edit bumps. Mirrors\n    store-compact.py's handoff_age."
    m = _STATUS_LINE.search(str(e.get("body") or ""))
    if m:
        status = _CODE_SPAN.sub("", m.group(1).split("**Opened:**")[0])
        ages = [a for a in (_age_days(d, now) for d in _STATUS_DATE.findall(status))
                if a is not None and a >= 0]
        if ages:
            return min(ages)
    return _age_days(e.get("updated_at"), now)


def _spent_records_in_live_dirs(store):
    now = time.time()
    fix = ("store-compact.py --apply moves it to the archive; the daily janitor runs it, "
           "so check that the janitor is running")
    rows = []
    for o in list_audit_observations(store):
        if o.get("status") not in _OBS_SPENT_STATUSES:
            continue
        age = _age_days(o.get("resolved_date") or o.get("created_at"), now)
        if age is not None and age >= _OBS_SPENT_DAYS:
            rows.append({"kind": "observation", "id": o.get("id"), "status": o.get("status"),
                         "age_days": age, "fix": fix})
    for e in store.entities.values():
        path = e.get("path") or ""
        if e.get("type") != "doc" or not path.startswith("handoffs/") or _is_archived(e):
            continue
        m = _HANDOFF_SPENT.search(str(e.get("body") or ""))
        age = _handoff_age(e, now)
        if m and age is not None and age >= _HANDOFF_SPENT_DAYS:
            rows.append({"kind": "handoff", "scope": e.get("scope"), "path": path,
                         "status": m.group(1), "age_days": age, "fix": fix})
    rows.sort(key=lambda r: (r["kind"], r.get("id") if r["kind"] == "observation" else 0,
                             r.get("path") or ""))
    return rows


def _oversized_memories(store):
    rows = []
    for e in store.entities.values():
        if e.get("type") != "memory" or _is_archived(e):
            continue
        n = len(str(e.get("body") or "").strip().encode("utf-8"))
        if n > _MEMORY_MAX_BYTES:
            rows.append({"slug": e.get("slug"), "scope": e.get("scope"), "bytes": n,
                         "limit": _MEMORY_MAX_BYTES,
                         "fix": "keep current state in the memory; move history to an "
                                "archive doc and link it"})
    rows.sort(key=lambda r: (-r["bytes"], r["scope"] or "", r["slug"] or ""))
    return rows


def _repeated_agent_paragraphs(store):
    seen = {}
    for e in store.entities.values():
        if e.get("type") != "agent_definition" or _is_archived(e):
            continue
        for para in re.split(r"\n\s*\n", str(e.get("body") or "")):
            text = " ".join(para.split())
            if len(text) < _AGENT_PARA_MIN or text.startswith(_AGENT_SHARED_STEMS):
                continue
            seen.setdefault(text, set()).add(e.get("name"))
    rows = [{"paragraph": text[:120], "agents": sorted(names),
             "fix": "move it to worker-shared-rules.md and keep one pointer per worker"}
            for text, names in seen.items() if len(names) > 1]
    rows.sort(key=lambda r: (r["agents"], r["paragraph"]))
    return rows



VERIFY_OVERDUE_DAYS = 180
_STORE_ROOT_SEGMENTS = ("global", "projects", "workspaces", "server", "machines", "tests",
                        "templates", "shared-skills")


def _verification(store):
    "(overdue rows, missing-source rows, counts) over memories and live docs.\n\n    `verified_at` is the date someone last checked an entity's claims. `sources` are the\n    files those claims rest on: a store-relative path is checked on any machine, and an\n    absolute or ~-rooted one only where `hosts` is empty or names this machine."
    from . import machine
    here = machine.get_chezmoi_machine_id()
    today = time.strftime("%Y-%m-%d")
    overdue, missing = [], []
    counts = {"verified": 0, "never_verified": 0, "with_sources": 0}
    for e in store.entities.values():
        kind = e.get("type")
        if kind not in ("memory", "doc") or (kind == "doc" and _is_archived(e)):
            continue
        key = e.get(NATURAL_KEY[kind])
        row = {"type": kind, "scope": e.get("scope") or "global", "key": key}
        stamp = str(e.get("verified_at") or "")
        if stamp:
            counts["verified"] += 1
            try:
                age = (time.mktime(time.strptime(today, "%Y-%m-%d"))
                       - time.mktime(time.strptime(stamp[:10], "%Y-%m-%d"))) / 86400
            except ValueError:
                age = None
            if age is None or age > VERIFY_OVERDUE_DAYS:
                overdue.append({**row, "verified_at": stamp,
                                "age_days": None if age is None else int(age)})
        else:
            counts["never_verified"] += 1
        sources = e.get("sources")
        if not isinstance(sources, list) or not sources:
            continue
        counts["with_sources"] += 1
        hosts = e.get("hosts") if isinstance(e.get("hosts"), list) else []
        for source in sources:
            source = str(source).strip()
            if not source or " " in source:
                continue            
            if source.split("/")[0] in _STORE_ROOT_SEGMENTS:
                path = os.path.join(store.root, source)
            elif source.startswith(("/", "~/")) and (not hosts or (here and here in hosts)):
                path = os.path.expanduser(source)
            else:
                continue
            try:
                os.stat(path)
            except PermissionError:
                continue            
            except OSError:
                missing.append({**row, "source": source})
    overdue.sort(key=lambda r: (r["verified_at"], r["scope"], r["key"] or ""))
    missing.sort(key=lambda r: (r["scope"], r["key"] or "", r["source"]))
    return overdue, missing, counts




_SUMMARY_OMITS = ("split_candidates", "graph_coverage", "bootstrap_footprint")


def check_integrity(store, summary=False):
    'Scan the loaded index for known hazard classes and RETURN findings (no\n    repair): (a) a slug living at >1 scope (cross-scope dup); (b) files that failed\n    to load/parse; (c) dangling [[wikilinks]] — only link-shaped targets (slug/doc\n    path, no whitespace/punctuation) whose target is neither a memory slug nor a doc\n    path/title; prose-in-brackets and anything inside code spans/fences are ignored;\n    (d) descriptions over 140 chars; (i) a project/workspace entity of any kind that\n    substantially duplicates a global one of the same kind and key — a genuine fork,\n    not just a name collision — excluding genuine shims.'
    
    
    
    store.sweep_vanished()

    
    scopes_by_slug = {}
    for typ, scope, key in store.by_key:
        if typ == "memory":
            scopes_by_slug.setdefault(key, set()).add(scope)
    cross = [{"slug": s, "scopes": sorted(v)} for s, v in sorted(scopes_by_slug.items()) if len(v) > 1]

    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    _entity_kind = {"memory": ("memory", "slug"), "doc": ("docs", "path"),
                    "instruction": ("instructions", "title"), "skill": ("skills", "name"),
                    "command": ("commands", "name"), "agent_definition": ("agents", "name"),
                    "script": ("scripts", "name"), "hook": ("hooks", "name")}
    _shim_exec_re = re.compile(r"\b(?:exec|source|bash)\b[^\n]{0,80}global/")
    cross_entities = []
    for e in store.entities.values():
        scope = e.get("scope") or ""
        if not scope.startswith(("project:", "ws:")):
            continue
        spec = _entity_kind.get(e.get("type"))
        if not spec:
            continue
        kind_dir, key_field = spec
        key = e.get(key_field)
        if key is None:
            continue
        global_uid = store.by_key.get((e["type"], "global", key))
        if global_uid is None:
            continue
        body = str(e.get("body") or e.get("script_body") or "")
        if f"global/{kind_dir}/{key}" in body:
            continue  
        if "AGENT_CONTEXT_STORE" in body and _shim_exec_re.search(body):
            continue  
        global_e = store.entities[global_uid]
        global_body = str(global_e.get("body") or global_e.get("script_body") or "")
        if not _is_fork(global_body, body):
            continue  
        cross_entities.append({
            "kind": e["type"], "key": key, "scope": scope, "global_scope": "global",
            "fix": "the project copy must justify itself in one line, or be removed "
                   "in favour of the global one; a copy that only narrows "
                   "configuration usually belongs as a shim (exec/source the global "
                   "file) or as project instruction text instead",
        })
    cross_entities.sort(key=lambda x: (x["kind"], x["scope"], x["key"]))

    
    load_errors = list(getattr(store, "load_errors", []) or [])

    
    
    
    known = _known_targets(store)
    dangling, broken_pointers = [], []
    for e in store.entities.values():
        if e.get("type") not in _PROSE_TYPES:
            continue
        if _is_archived(e):
            continue                    
        body = str(e.get("body") or e.get("script_body") or "")
        src = {"source_type": e["type"], "scope": e.get("scope"),
               "source": e.get("slug") or e.get("path") or e.get("name")}
        for label, target, suggest in _resolve_refs(known, body):
            hit = {**src, "target": target}
            if suggest:
                hit["suggest"] = suggest
            if label.startswith("[["):
                if label != f"[[{target}]]":
                    hit["written_as"] = "title"
                dangling.append(hit)
            else:
                hit["call"] = label
                broken_pointers.append(hit)

    
    long_desc = []
    for e in store.entities.values():
        if e.get("type") != "memory":
            continue
        d = e.get("description") or ""
        if len(d) > _DESC_MAX:
            long_desc.append({"slug": e.get("slug"), "scope": e.get("scope"),
                              "length": len(d), "description": d[:80] + "…"})
    long_desc.sort(key=lambda x: -x["length"])

    
    long_entity_desc = []
    for e in store.entities.values():
        if e.get("type") not in ("script", "hook"):
            continue
        d = e.get("description") or ""
        if len(d) > _DESC_MAX:
            long_entity_desc.append({"kind": e.get("type"), "scope": e.get("scope"),
                                     "key": _entity_key(e), "length": len(d),
                                     "description": d[:80] + "…"})
    long_entity_desc.sort(key=lambda x: -x["length"])

    
    
    
    missing_lb = sorted(
        ({"slug": e.get("slug"), "scope": e.get("scope"),
          "fix": "upsert_memory(slug, load_behavior='lazy' or 'always')"}
         for e in store.entities.values()
         if e.get("type") == "memory" and e.get("load_behavior") not in ("always", "lazy")),
        key=lambda x: (x["scope"] or "", x["slug"] or ""))

    
    
    
    
    footprint = {}
    def _f(sc):
        return footprint.setdefault(sc, {"always_instruction_bytes": 0, "loaded_memory_count": 0,
                                         "memory_row_bytes": 0, "lazy_memory_count": 0,
                                         "lazy_roster_bytes": 0})
    for e in store.entities.values():
        t = e.get("type")
        sc = e.get("scope")
        if t == "memory":
            f = _f(sc)
            if _is_lazy(e):
                f["lazy_memory_count"] += 1
                f["lazy_roster_bytes"] += _roster_cost(e)
            else:
                f["loaded_memory_count"] += 1
                f["memory_row_bytes"] += _row_cost(e)
        elif t == "instruction" and e.get("load_behavior") == "always":
            _f(sc)["always_instruction_bytes"] += len(e.get("body") or "")
    for f in footprint.values():
        f["est_bootstrap_bytes"] = (f["always_instruction_bytes"] + f["memory_row_bytes"]
                                    + f["lazy_roster_bytes"])
        f["est_bootstrap_tokens"] = round(f["est_bootstrap_bytes"] / 4)
        
        
        
        f["memory_rows_budget"] = _MEM_ROWS_BUDGET
        f["over_budget"] = f["memory_row_bytes"] > _MEM_ROWS_BUDGET
    over_budget = sorted(sc for sc, f in footprint.items() if f["over_budget"])
    
    
    g_bytes = footprint.get("global", {}).get("est_bootstrap_bytes", 0)
    for sc, f in footprint.items():
        f["est_session_bytes"] = f["est_bootstrap_bytes"] + (0 if sc == "global" else g_bytes)

    
    
    
    
    by_body = {}
    for e in store.entities.values():
        b = str(e.get("body") or e.get("script_body") or "")
        if len(b) < _DUP_MIN_BYTES:
            continue
        if _is_archived(e):
            continue                    
        by_body.setdefault(hashlib.sha256(b.encode()).hexdigest(), []).append(
            {"type": e.get("type"), "scope": e.get("scope"),
             "name": e.get("name") or e.get("slug") or e.get("path")})
    duplicate_bodies = [{"copies": sorted(v, key=lambda d: (d["scope"] or "", d["name"] or ""))}
                        for v in sorted(by_body.values(), key=len, reverse=True) if len(v) > 1]

    
    
    
    orphan_scopes = []
    proot = os.path.join(store.root, "projects")
    if os.path.isdir(proot):
        for name in sorted(os.listdir(proot)):
            d = os.path.join(proot, name)
            if os.path.isdir(d) and not os.path.isfile(os.path.join(d, "project.toml")):
                held = sum(len(fs) for _, _, fs in os.walk(d))
                orphan_scopes.append({"scope": f"project:{name}", "files": held,
                                      "why": "no project.toml — unreachable by resolution"})

    
    
    over_instruction_budget = [
        {"scope": sc, "bytes": f["always_instruction_bytes"], "budget": _ALWAYS_INSTR_BUDGET}
        for sc, f in sorted(footprint.items())
        if f["always_instruction_bytes"] > _ALWAYS_INSTR_BUDGET]

    
    
    typed_graph = graph.graph_for(store)
    dangling_typed = []
    for uid, relation, target, hit in typed_graph.typed:
        source = store.entities.get(uid)
        if source is None or hit is not None or graph.is_archived(source):
            continue
        dangling_typed.append({
            "source_type": source["type"], "scope": source.get("scope"),
            "source": source.get("slug") or source.get("path") or source.get("name")
            or source.get("title"), "relation": relation, "target": target,
        })
    dangling_typed.sort(key=lambda row: (row["scope"] or "", row["source"] or "",
                                         row["relation"], row["target"]))

    findings = {
        "cross_scope_duplicate_slugs": cross,
        "cross_scope_duplicate_entities": cross_entities,
        "load_errors": load_errors,
        "dangling_links": dangling,
        "dangling_typed_links": dangling_typed,
        "broken_pointers": broken_pointers,
        
        
        "ambiguous_links": [r for r in graph.ambiguous_links(store)
                            if not (r["source_type"] == "doc"
                                    and _is_archived({"path": r["source"]}))],
        
        "dangling_anchors": [r for r in graph.dangling_anchors(store)
                             if not (r["source_type"] == "doc"
                                     and _is_archived({"path": r["source"]}))],
        
        
        "unaddressable_large_bodies": [r for r in graph.unaddressable_large_bodies(store)
                                       if not (r["type"] == "doc"
                                               and _is_archived({"path": r["key"]}))],
        
        
        "orphans": _orphans(store),
        "split_candidates": [r for r in graph.split_candidates(store) if not _archived_doc(r)],
        "superseded_still_linked": [
            {**r, "linked_from": live} for r in graph.superseded_still_linked(store)
            if (live := [s for s in r["linked_from"] if not _archived_doc(s)])],
        "supersede_cycles": graph.supersede_cycles(store),
        "enforced_by_shared": graph.enforced_by_shared(store),
        "duplicate_bodies": duplicate_bodies,
        "orphan_scopes": orphan_scopes,
        "over_instruction_budget": over_instruction_budget,
        "long_memory_descriptions": long_desc,
        "long_entity_descriptions": long_entity_desc,
        "missing_load_behavior": missing_lb,
        
        "emphatic_capitals": _emphatic_capitals(store),
        "truncated_descriptions": _truncated_descriptions(store),
        "spent_records_in_live_dirs": _spent_records_in_live_dirs(store),
        "oversized_memories": _oversized_memories(store),
        "repeated_agent_paragraphs": _repeated_agent_paragraphs(store),
        "over_budget_scopes": over_budget,
        
        "prune_candidates": _prune_candidates(store),
        
        
        "stale_always_loaded": _stale_always_loaded(store),
        
        
        "cold_always_loaded": _cold_always_loaded(store),
        
        
        
        
        "stale_open_observations": [
            {"id": o.get("id"), "age_days": o.get("age_days"),
             "severity": o.get("severity") or "normal", "project": o.get("project"),
             "summary": _audit_index(o)["summary"]}
            for o in list_audit_observations(store, status="open", needs_reverify=True)],
        
        
        
        
        
        "observations_with_markup": _observations_with_markup(store),
        
        
        
        "observation_ids_reused": _observation_ids_reused(store),
        
        
        
        
        "over_ceiling_sessions": sorted(
            sc for sc, f in footprint.items()
            if sc != "global"
            and f["est_bootstrap_bytes"] + g_bytes > _BOOTSTRAP_CEILING) +
            (["global"] if g_bytes > _BOOTSTRAP_CEILING else []),
    }
    
    (findings["path_derived_identity"], findings["uuid_scope_mismatch"],
     findings["filename_key_mismatch"]) = _hand_moves_and_renames(store)
    (findings["verification_overdue"], findings["missing_sources"],
     verification) = _verification(store)
    findings["summary"] = {k: len(v) for k, v in findings.items()}
    
    findings["verification"] = verification
    
    
    findings["graph_coverage"] = graph.coverage(store)
    findings["bootstrap_footprint"] = footprint
    
    
    
    
    try:
        from .session import _audit_bootstrap  
        findings["audit_block_bytes"] = len(json.dumps(_audit_bootstrap(store, None),
                                                       default=str))
    except Exception:
        findings["audit_block_bytes"] = 0
    findings["bootstrap_ceiling"] = _BOOTSTRAP_CEILING
    if summary:
        return {"summary": findings["summary"],
                **{k: v for k, v in findings.items()
                   if k in findings["summary"] and v and k not in _SUMMARY_OMITS},
                "verification": verification, "omitted": list(_SUMMARY_OMITS)}
    return findings


def get_version_history(store, entity_type, entity_id=None, key=None, project=None) -> Any:
    'Real history from git log of the entity\'s file: by uuid (`entity_id`), or by\n    `entity_type` (the kind) + `key` + `project` as get_entity takes them. Nothing found\n    is an error naming what was tried, never an empty list (which reads as "no history").'
    if entity_id is not None:
        tried = f"entity_id {entity_id!r}"
        e = store.entities.get(str(entity_id))
    elif key:
        tried = f"key {key!r}" + (f" in project {project!r}" if project else "")
        try:
            e = store.get(entity_type, key, project, scope=store.scope_for_read(project, None))
        except ValueError as exc:
            return {"error": f"no {entity_type} found for {tried}: {exc}"}
    else:
        return {"error": "get_version_history needs entity_id (the entity's uuid) or key "
                         "(with the kind in entity_type, and project when it is project-scoped)"}
    if not e:
        return {"error": f"no {entity_type} found for {tried}"}
    if e.get("type") != entity_type:
        return {"error": f"{tried} is a {e.get('type')}, not a {entity_type}"}
    if not e.get("_path"):
        return []
    rel = os.path.relpath(e["_path"], store.root)
    out: list[dict] = []
    rev = "HEAD"
    for _ in range(16):              
        r = subprocess.run(["git", "-C", store.root, "log", "--format=%h%x09%ad%x09%s",
                            "--date=short", rev, "--", rel], capture_output=True, text=True)
        rows = r.stdout.strip().splitlines()
        for ln in rows:
            h, _, rest = ln.partition("\t")
            d, _, summ = rest.partition("\t")
            out.append({"version": h, "date": d, "change_summary": summ})
        if not rows:
            break
        first = rows[-1].partition("\t")[0]
        src = _moved_from(store.root, first, rel)
        if not src:
            break
        rel, rev = src, f"{first}^"
    return out


def _moved_from(root: str, rev: str, rel: str) -> str | None:
    "The path `rel` had before commit `rev`, when `rev` is the rename_project move that\n    put it there. Only a move between project directories counts: `git log --follow`\n    would also pin an unrelated small file's history on a new one that merely looks\n    like it."
    r = subprocess.run(["git", "-C", root, "show", "-M30%", "--name-status", "--format=", rev],
                       capture_output=True, text=True)
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3 and parts[0].startswith("R") and parts[2] == rel \
                and _same_place_in_another_project(parts[1], rel):
            return parts[1]
    return None


def _same_place_in_another_project(a: str, b: str) -> bool:
    pa, pb = a.split("/"), b.split("/")
    if len(pa) != len(pb) or len(pa) < 3 or pa[0] != "projects" or pb[0] != "projects" \
            or pa[1] == pb[1]:
        return False
    ra, rb = pa[2:], pb[2:]
    
    return ra == rb or (ra[:-1] == rb[:-1] == ["docs"]
                        and ra[-1] == f"{pa[1]}.md" and rb[-1] == f"{pb[1]}.md")


def register_machine_path(store, cwd, project=None):
    'Resolve `cwd` to a project. With an explicit `project` (display_name), a cwd\n    that does not resolve by marker or remote gets the `.agents/project-id` marker\n    written at its repo root, so it resolves machine-agnostically from then on\n    (the remote-match fallback fails when every configured URL is a leg the store\n    never recorded).'
    res: dict = P.resolve_project(store, cwd)
    if "error" not in res or not project:
        return res
    from .project_resolve import get_repo_root
    target = next((e for e in store.entities.values() if e.get("type") == "project"
                   and (e.get("display_name") == project or e.get("uuid") == project)), None)
    if not target:
        return {"error": f"No project named '{project}' in the store (see list_entities(kind='project'))"}
    root = get_repo_root(cwd)
    if not root:
        return {"error": f"{cwd} is not inside a git worktree; cannot place a project marker"}
    if not _write_project_marker(root, target["uuid"], target.get("display_name")):
        return {"error": f"could not write {root}/.agents/project-id"}
    res = P.resolve_project(store, cwd)
    return res if "error" in res else {**res, "marker_written": True}


def translate_project_paths(store, from_machine=None):
    return {"ok": True, "note": "file-store resolves projects via git remote; no per-machine paths to translate"}
