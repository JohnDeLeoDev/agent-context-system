
'Audit observations (one JSON file each) and the live human digest.\n\n`fstools` re-exports everything here, so callers and tests import `fstools as T`.'
from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC

from . import identity
from .paths import write_atomic



_AUDIT_DIR = "audit-observations"
_AUDIT_ARCHIVE_DIR = "audit-observations-archive"


def _audit_dir(store, archive=False):
    return os.path.join(store.root, "global", _AUDIT_ARCHIVE_DIR if archive else _AUDIT_DIR)


def _audit_legacy_path(store, archive=False):
    name = "audit-observations-archive.json" if archive else "audit-observations.json"
    return os.path.join(store.root, "global", name)


def _audit_file(store, oid, archive=False):
    return os.path.join(_audit_dir(store, archive), f"{int(oid):04d}.json")


def _audit_read_dir(d):
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue   
        if isinstance(rec, dict) and rec.get("id") is not None:
            out.append(rec)
    return out


def _audit_write(store, rec, archive=False):
    "Atomically (re)write one observation's file.\n\n    Goes through the shared writer, which removes its temp file on failure: a stray\n    `NNNN.json.tmp` would be swept in by `git add -A`. Serializing first also means a\n    non-JSON value in `rec` fails before any file is touched."
    p = _audit_file(store, rec["id"], archive)
    write_atomic(p, json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
    store._arm_commit(p)


def _audit_id_floor_from_git(store):
    'Highest observation id visible in any ref, or 0.\n\n    The working tree is not the fleet. A machine whose sync is behind or deferred\n    allocates from what it can see locally, and every other machine does the same, so\n    two of them can hand the same number to different defects. The result is an\n    add/add conflict git cannot merge, and whose obvious resolution (take one side)\n    silently destroys a real observation.\n\n    Reading refs closes the ordinary case: a machine that has fetched sees the ids its\n    peers have already used, even when it has not integrated them. It cannot close the\n    case of two machines genuinely offline from each other at the same moment, which is\n    what store._resolve_observation_collisions is for. Two mechanisms because neither is complete:\n    this one prevents, the other repairs.\n\n    Same approach as release-server.py, which takes its build floor from git history.\n\n    Never raises and never blocks a filing: a store with no git, no refs, or a failing\n    git call falls back to the local maximum. A cheap prevention that can refuse to\n    record an observation would cost more than the collision does.'
    import re
    import subprocess
    pat = re.compile(rf"global/{_AUDIT_DIR}(?:-archive)?/(\d+)\.json$")
    best = 0
    try:
        refs = subprocess.run(
            ("git", "-C", store.root, "for-each-ref", "--format=%(objectname)"),
            capture_output=True, text=True, timeout=15)
        
        
        
        shas = sorted(set((refs.stdout or "").split()))
        if not shas:
            return 0
        for sha in shas:
            out = subprocess.run(
                ("git", "-C", store.root, "ls-tree", "-r", "--name-only", sha,
                 "--", f"global/{_AUDIT_DIR}", f"global/{_AUDIT_DIR}-archive"),
                capture_output=True, text=True, timeout=30)
            for line in (out.stdout or "").splitlines():
                m = pat.search(line.strip())
                if m:
                    best = max(best, int(m.group(1)))
    except (OSError, subprocess.SubprocessError):
        return 0
    return best


_AUDIT_FILE_RE = re.compile(r"^(\d+)\.json$")


def _audit_local_used_ids(store):
    "Every id this machine holds on disk, active and archive.\n\n    From filenames, not parsed contents. A record whose JSON does not load still spends\n    its number, and the case where that matters is precisely the one this feeds: the\n    renumberer runs mid-merge, where a conflicted file holds git's markers and parses as\n    nothing. A content scan reports an empty store there and the allocator restarts at 1.\n\n    Shared so the allocation sites cannot drift apart: a renumberer that handed out ids\n    already in use would create a fresh add/add conflict on the next machine to sync."
    out = []
    for archive in (False, True):
        d = _audit_dir(store, archive)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            m = _AUDIT_FILE_RE.match(fn)
            if m:
                out.append(int(m.group(1)))
    return out


def _audit_next_id(store):
    "The next free id: above everything on disk AND everything any ref has spent.\n    Both halves are required -- refs alone miss this machine's unpushed filings, disk\n    alone misses ids a peer spent since the last integration."
    return max([*_audit_local_used_ids(store), _audit_id_floor_from_git(store)],
               default=0) + 1


def _audit_id_taken(store, oid):
    'Is `oid` spent in either directory (active or archive)?'
    return any(os.path.exists(_audit_file(store, oid, a)) for a in (False, True))


def _audit_stamp(o):
    return str(o.get("updated_at") or o.get("resolved_date") or o.get("created_at") or "")


def _audit_migrate_legacy(store, archive=False):
    'Split the legacy aggregate file into per-record files, then delete it.\n\n    Idempotent and re-runnable: an old daemon on another host keeps writing the\n    aggregate until it redeploys, and git can bring that file back. Whenever\n    it exists, every record it holds is imported unless the per-file copy is at least\n    as new (or the id already lives in the archive), then the aggregate is removed.'
    legacy = _audit_legacy_path(store, archive)
    if not os.path.exists(legacy):
        return 0
    with store.lock:
        try:
            with open(legacy, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return 0
        rows = d if isinstance(d, list) else d.get("observations", [])
        have = {r["id"]: r for r in _audit_read_dir(_audit_dir(store, archive))}
        archived = set() if archive else {r["id"] for r in _audit_read_dir(_audit_dir(store, True))}
        n = 0
        for o in rows:
            if not isinstance(o, dict) or o.get("id") is None or o["id"] in archived:
                continue
            cur = have.get(o["id"])
            if cur is None or _audit_stamp(o) > _audit_stamp(cur):
                _audit_write(store, o, archive)
                n += 1
        os.remove(legacy)
        store._arm_commit(legacy)
        return n


def _audit_load(store):
    "Every active observation, sorted by id. Runs the legacy import first so a\n    build-17-or-older writer's file is absorbed rather than shadowed."
    _audit_migrate_legacy(store, archive=False)
    _audit_migrate_legacy(store, archive=True)
    return sorted(_audit_read_dir(_audit_dir(store)), key=lambda o: int(o.get("id", 0)))


def archive_audit_observation(store, observation_id):
    "Move one observation's file from the active set to the archive (what\n    store-compact.py does for spent records). Returns the record or None."
    with store.lock:
        src = _audit_file(store, observation_id)
        if not os.path.exists(src):
            return None
        with open(src, encoding="utf-8") as fh:
            rec = json.load(fh)
        
        
        dst = _audit_file(store, observation_id, archive=True)
        if os.path.exists(dst):
            with open(dst, encoding="utf-8") as fh:
                if fh.read() != json.dumps(rec, indent=1, ensure_ascii=False) + "\n":
                    raise ValueError(
                        f"archive already holds a different observation #{observation_id}")
        _audit_write(store, rec, archive=True)
        os.remove(src)
        store._arm_commit(src)   
        return rec





_SEVERITIES = ("blocker", "high", "normal", "low")
_SEV_RANK = {s: i for i, s in enumerate(_SEVERITIES)}


_REVERIFY_AFTER_DAYS = 14



_DUP_CONTAINMENT = 0.35
_DUP_STOPWORDS = set(
    ["the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were", "it", "its", "this", "that", "for", "with", "on", "at", "by", "as", "be", "been", "from", "not", "no", "cannot", "can", "could", "should", "would", "will", "has", "have", "had", "does", "do", "did", "so", "if", "then", "than", "but", "which", "what", "when", "where", "who", "whom", "whose", "there", "their", "they", "them", "you", "your", "we", "our", "us", "my", "one", "two", "also", "only", "just", "still", "even", "now", "new", "old", "more", "most", "less", "any", "all", "each", "other", "same", "such", "own", "very", "much", "many", "few"])


def _audit_tokens(text):
    ws = re.findall(r"[a-z][a-z0-9_.\-/]{2,}", (text or "").lower())
    return {w for w in ws if w not in _DUP_STOPWORDS}


def _audit_near_duplicates(data, observation, scope, project, limit=3):
    'Open observations that look like the same defect as `observation`.\n\n    This surfaces the candidates; update_audit_observation is the route that makes\n    re-filing unnecessary.'
    new_toks = _audit_tokens(observation)
    if len(new_toks) < 8:                     
        return []
    out = []
    for o in data:
        if o.get("status") != "open":
            continue
        if o.get("scope") != scope or o.get("project") != project:
            continue
        t = _audit_tokens(o.get("observation"))
        if not t:
            continue
        c = len(new_toks & t) / min(len(new_toks), len(t))
        if c >= _DUP_CONTAINMENT:
            out.append({"id": o.get("id"), "overlap": round(c, 2), **_audit_index(o)})
    return sorted(out, key=lambda d: -d["overlap"])[:limit]


def _this_machine():
    ' this machine.'
    from . import session
    if session.unknown_caller():
        return "unknown"
    return identity.session_machine_id() or identity.session_machine_uuid()


def _audit_age_days(o):
    from datetime import datetime
    raw = str(o.get("observed_date") or o.get("created_at") or "")[:10]
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - d).days


def _audit_decorate(o):
    'Add the derived fields the drain needs: how old it is, whether it has gone long\n    enough unverified that its claims should be re-checked against the code before\n    anyone acts on them, and whether it was filed on a different machine.\n\n    That last one exists because an observation\'s prose says "this machine" or names a\n    host, and both silently re-bind to whoever reads it later.'
    age = _audit_age_days(o)
    d = dict(o)
    d["age_days"] = age
    d["needs_reverify"] = bool(o.get("status") == "open" and age is not None
                               and age >= _REVERIFY_AFTER_DAYS)
    seen_on = o.get("machine")
    if seen_on:
        d["observed_elsewhere"] = seen_on != _this_machine()
    d["summary"] = _audit_summary(d)
    return d


def _audit_clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _audit_summary(o):
    "One scannable line whose status and resolution come before the prose.\n\n    A list row is read at whatever width the reader's view or prefix happens to be. In\n    that view a resolved observation and an open one are identical, because `status` is\n    a separate key and `resolution_note` sits past thousands of characters of\n    `observation`.\n\n    So the fields that decide whether to act go first, and the prose last, clipped\n    rather than dropped, since the full text is still on the record beside this."
    parts = [f"#{o.get('id')}",
             f"[{(o.get('status') or 'open').upper()} {o.get('severity') or 'normal'}]",
             str(o.get("observed_date") or "")[:10]]
    if o.get("needs_reverify"):
        parts.append("NEEDS-REVERIFY")
    if o.get("observed_elsewhere"):
        parts.append(f"seen-on:{o.get('machine')}")
    head = " ".join(p for p in parts if p)
    note = _audit_clip(o.get("resolution_note"), 200)
    if note:
        head += f" · RESOLUTION: {note}"
    return f"{head} · {_audit_clip(o.get('observation'), 200)}"


def list_audit_observations(store, project=None, status=None, severity=None,
                            needs_reverify=None):
    out = [_audit_decorate(o) for o in _audit_load(store)]
    if project:
        out = [o for o in out if o.get("project") == project]
    if status:
        out = [o for o in out if o.get("status") == status]
    if severity:
        out = [o for o in out if (o.get("severity") or "normal") == severity]
    if needs_reverify is not None:
        out = [o for o in out if o.get("needs_reverify") is bool(needs_reverify)]
    
    out.sort(key=lambda o: (_SEV_RANK.get(o.get("severity") or "normal", 2),
                            str(o.get("observed_date") or "")))
    return out







_AUDIT_AUTO_COMPACT_BYTES = 200_000
_AUDIT_COMPACT_KEYS = ("id", "status", "severity", "project", "scope", "observed_date",
                       "machine", "recurrences", "age_days", "needs_reverify", "summary")
_AUDIT_TOUCH_FIELDS = ("observed_date", "resolved_date", "updated_at", "last_seen")


def _parse_when(v) -> float | None:
    'Seconds since the epoch for a date or datetime string; None when unreadable.'
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.timestamp()


def _touched_within(o, since_days, now) -> bool:
    "Any of the record's dates falls inside the window: a five-month-old item that\n    was resolved yesterday is part of yesterday."
    cutoff = now - float(since_days) * 86400
    return any((_parse_when(o.get(k)) or -1) >= cutoff for k in _AUDIT_TOUCH_FIELDS)


def list_view(store, project=None, status=None, severity=None, needs_reverify=None,
              since_days=None, limit=None, compact=False, now=None,
              auto_compact_bytes=_AUDIT_AUTO_COMPACT_BYTES) -> dict:
    'The tool-facing list: {observations, total, shown, hint}.\n\n    - since_days keeps a row when any of its dates is inside the window.\n    - limit slices after the drain-order sort, so a cut list is still worst-first.\n    - compact rows carry exactly _AUDIT_COMPACT_KEYS; `summary` already leads with\n      status and resolution, so a compact row is safe to act on.\n    - With no filter at all, a result over `auto_compact_bytes` comes back compact\n      with `hint` saying so. Nothing is ever dropped silently: `total` and `shown`\n      are always there.'
    t = time.time() if now is None else now
    rows = list_audit_observations(store, project, status, severity, needs_reverify)
    if since_days is not None:
        rows = [o for o in rows if _touched_within(o, since_days, t)]
    total = len(rows)
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    hint = None
    unfiltered = not any((project, status, severity, needs_reverify is not None,
                          since_days is not None, limit is not None))
    if not compact and unfiltered:
        size = len(json.dumps(rows, default=str))
        if size > auto_compact_bytes:
            compact = True
            hint = (f"{total} observations, {size} bytes unfiltered -- rows returned "
                    f"compact. Narrow with status=, since_days=, severity= or limit=; a "
                    f"filtered call returns full records.")
    if compact:
        rows = [{k: o.get(k) for k in _AUDIT_COMPACT_KEYS} for o in rows]
    return {"observations": rows, "total": total, "shown": len(rows), "hint": hint}


_AUDIT_SCOPES = ("universal", "project")
_TOOL_MARKUP = re.compile(r"</?(observation|parameter|scope|project|evidence)>|<parameter name=")


def _audit_malformed(observation, scope, project, evidence):
    'Server-side twin of the audit-observation-guard hook: the rules a malformed\n    observation is refused on, enforced where every harness writes.\n\n    The hook only fires in Claude Code. pi, opencode and copilot write through the same\n    MCP tool with no hook. Each condition is a pure serialization defect with no\n    legitimate exception, and re-issuing correctly costs one tool call, so refusing is\n    strictly cheaper than the repair it replaces.'
    problems = []
    if _TOOL_MARKUP.search(observation or ""):
        problems.append("the observation text contains tool-call markup (`</observation>`, "
                        "`<parameter name=…>`) — everything after it was swallowed into the "
                        "string instead of landing in its own field.")
    if not (evidence or "").strip():
        problems.append("`evidence` is empty — it is the field the next audit quotes; pass a "
                        "file:line reference or a quoted excerpt.")
    if scope not in _AUDIT_SCOPES:
        problems.append(f'`scope` is "{scope}" (expected "universal" or "project").')
    elif scope == "project" and not project:
        problems.append('`scope` is "project" but `project` is null, so this would land '
                        'outside every project-scoped audit — name the project.')
    return problems


def _refresh_observation_coverage(store) -> None:
    "Kick off `observation-coverage.py --health`, detached, right after a write that can\n    change what it reports (a new recurrence, or a status change).\n\n    Without this, the verdict observation-coverage last wrote stands until the next\n    SessionStart probe (invariant-probe.py) runs, so sync-fault-notice.py could cite a\n    stale verdict. Detached and fully redirected, same as invariant-probe.py's\n    SessionStart hook, so a slow run never holds up the write. SERVER_TASK_ENV is set\n    because this already runs inside the daemon: without it the script would try to\n    forward itself back over MCP to the process calling it. Best-effort: a write must never fail because this could not start."
    script = os.path.join(store.root, "global", "scripts", "observation-coverage.py")
    if not os.path.isfile(script):
        return
    from .store_tasks import SERVER_TASK_ENV
    env = dict(os.environ, **{SERVER_TASK_ENV: "1", "AGENT_CONTEXT_STORE": str(store.root)})
    with contextlib.suppress(OSError):
        subprocess.Popen([sys.executable, script, "--health"], cwd=store.root, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def add_audit_observation(store, observation, scope="project", project=None, evidence=None,
                          observed_date=None, severity="normal"):
    from .store import _now
    problems = _audit_malformed(observation, scope, project, evidence)
    if problems:
        return {"error": "add_audit_observation rejected as malformed: " + " ".join(problems)
                + " Nothing was written. Re-issue with observation, scope, project and "
                  "evidence as separate tool arguments."}
    if severity not in _SEVERITIES:
        severity = "normal"
    
    
    
    with contextlib.suppress(Exception):
        store._maybe_fetch_for_guard()
    with store.lock:
        data = _audit_load(store)
        dups = _audit_near_duplicates(data, observation, scope, project)
        
        
        
        
        nid = _audit_next_id(store)
        rec = {"id": nid, "observation": observation, "scope": scope, "project": project,
               "evidence": evidence, "status": "open", "severity": severity,
               "machine": _this_machine(),
               "observed_date": observed_date or _now(), "created_at": _now()}
        _audit_write(store, rec)
        out = dict(rec)
        if dups:
            out["possible_duplicate_of"] = dups
            out["warning"] = (
                "This looks like an observation already open. If it is the same defect, call "
                f"update_audit_observation({dups[0]['id']}, recurred=True, evidence=...) instead "
                "of filing a new one, and resolve this one as a duplicate.")
        return out


def update_audit_observation(store, observation_id, note=None, severity=None, evidence=None,
                             recurred=False, status=None, machine=None):
    'Amend an open observation in place. `recurred=True` records that the same defect\n    was hit again, which saves re-filing it.'
    from .store import _now
    with store.lock:
        data = _audit_load(store)
        for o in data:
            if o.get("id") != observation_id:
                continue
            if severity in _SEVERITIES:
                o["severity"] = severity
            if status:
                o["status"] = status
            if machine:
                
                
                
                o["machine"] = machine
            if evidence:
                prev = o.get("evidence")
                o["evidence"] = f"{prev}\n---\n{evidence}" if prev else evidence
            if note:
                prev = o.get("notes") or ""
                o["notes"] = f"{prev}\n[{_now()}] {note}".strip()
            if recurred:
                o["recurrences"] = int(o.get("recurrences") or 1) + 1
                o["last_seen"] = _now()
            o["updated_at"] = _now()
            _audit_write(store, o)
            if recurred or status:
                _refresh_observation_coverage(store)
            return _audit_decorate(o)
        return {"error": f"observation {observation_id} not found"}


def resolve_audit_observation(store, observation_id, status="resolved", resolution_note=None):
    from .store import _now
    with store.lock:
        data = _audit_load(store)
        for o in data:
            if o.get("id") == observation_id:
                seen_on, here = o.get("machine"), _this_machine()
                o["status"] = status
                o["resolution_note"] = resolution_note
                o["resolved_date"] = _now()
                o["resolved_on"] = here
                _audit_write(store, o)
                _refresh_observation_coverage(store)
                out = dict(o)
                
                
                
                if seen_on and seen_on != here and status == "resolved":
                    out["warning"] = (
                        f"Filed on `{seen_on}`, resolved from `{here}`. If the fix is "
                        f"host-specific, verify on {seen_on} directly, or re-open with "
                        f'update_audit_observation({observation_id}, status="open", note=...).')
                return out
        return {"error": f"observation {observation_id} not found"}


def _audit_index(o):
    'Compact open-observation row for the session bootstrap — enough to notice an\n    open item and judge relevance, without paying for the full observation+evidence\n    prose (which loads into every session otherwise). Fetch the full record on demand\n    via list_audit_observations(project, status="open").'
    text = " ".join((o.get("observation") or "").split())
    row = {"id": o.get("id"), "scope": o.get("scope"), "project": o.get("project"),
           "observed_date": o.get("observed_date"),
           "summary": (text[:137] + "…") if len(text) > 138 else text}
    
    
    sev = o.get("severity") or "normal"
    if sev != "normal":
        row["severity"] = sev
    
    
    
    seen_on = o.get("machine")
    if seen_on and seen_on != _this_machine():
        row["observed_on"] = seen_on
    if int(o.get("recurrences") or 1) > 1:
        row["recurrences"] = int(o["recurrences"])
    return row


def _audit_row_text(o) -> str:
    'One bootstrap row as text: `#id sev scope date [seen-on:m] [xN] — summary`.\n\n    The same trade the memory index made: a JSON object per row spends far more\n    characters repeating key names than a line does (agent-context-bootstrap-cost-invariants).\n    Fields that say nothing are omitted, as _audit_index omitted them.'
    row = _audit_index(o)
    parts = [f"#{row.get('id')}", row.get("severity") or "normal",
             row.get("project") or row.get("scope") or "universal",
             str(row.get("observed_date") or "")[:10]]
    if row.get("observed_on"):
        parts.append(f"seen-on:{row['observed_on']}")
    if row.get("recurrences"):
        parts.append(f"x{row['recurrences']}")
    return " ".join(p for p in parts if p) + " — " + row["summary"]






_DIGEST_PATH_RE = re.compile(r"^inbox/machines/([^/]+)/audit-digest\.md$")


def _first_sentence(text, limit=220):
    text = " ".join((text or "").split())
    cut = text.find(". ")
    s = text[:cut + 1] if 0 < cut < limit else text
    return (s[:limit - 1] + "…") if len(s) > limit else s


def _audit_digest_rows(store):
    'Every open or triaged observation as a compact row, worst then oldest — the\n    drain order. `needs_reverify` marks claims nobody has checked against the code\n    for _REVERIFY_AFTER_DAYS.'
    out = []
    for o in list_audit_observations(store, None):
        if o.get("status") not in ("open", "triaged"):
            continue
        note = " ".join(str(o.get("notes") or "").split())
        out.append({
            "id": o.get("id"), "severity": o.get("severity") or "normal",
            "status": o.get("status"), "age_days": o.get("age_days"),
            "project": o.get("project") or ("universal" if o.get("scope") == "universal"
                                            else o.get("scope")),
            "machine": o.get("machine") or "",
            "summary": _first_sentence(o.get("observation")),
            "recurrences": int(o.get("recurrences") or 1),
            "needs_reverify": bool(o.get("needs_reverify")),
            "last_note": note[-200:],
        })
    out.sort(key=lambda r: (_SEV_RANK.get(r["severity"], 2), -(r["age_days"] or 0)))
    return out


def audit_digest(store, to):
    '{path, title, rows, body} for the machine named `to` (the inbox convention is\n    the fleet machine_id). Always returns; `rows` is empty when nothing is open.'
    from datetime import datetime
    rows = _audit_digest_rows(store)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    path = f"inbox/machines/{to}/audit-digest.md"
    title = f"Audit digest — {len(rows)} observation(s) waiting on a human"
    lines = [
        "---", "from: agent-context (rendered live)", f"to: {to}", "status: open",
        "priority: normal", f"created: {today}", f"updated: {today}", "generated: true", "---", "",
        f"# {title}", "",
        ("**Request** — decide each item below: fix it, assign it, or resolve it as won't-fix. "
         "These are the observations the bootstrap no longer shows (only blocker/high ride in "
         "sessions). This digest is rendered from the observation files every time it is read, "
         "so it is never stale and there is nothing to archive — it disappears from the inbox "
         "when nothing is open."),
        "",
        ("**Why** — the pad fills faster than it drains: items older than 14 days are unverified "
         "claims, and store-code defects have no owner unless a person assigns one. Project code "
         "defects do not belong here at all — raise them with user directly (there is no task "
         "tracker) and resolve the observation with that note."),
        "",
        ("**Acceptance criteria** — every row has an owner or a resolution; the store-code rows "
         "are either fixed via `release-server.py` or resolved with a note."),
        "",
        ("**Read the `seen on` column.** A **bold** host is not this machine: the defect was "
         "observed there, and **unknown** means it predates host stamping — either way local "
         "behavior neither confirms nor refutes it. Check that host, or resolve on a code fix "
         "that reaches the whole fleet, never on \"it works here\". Set a missing host with "
         "`update_audit_observation(id, machine=\"<id>\")`."),
        "",
        "| id | sev | status | age | scope | seen on | recur | summary |",
        "|---|---|---|---|---|---|---|---|",
    ]
    here = _this_machine()
    for r in rows:
        flag = " ⚠stale" if r["needs_reverify"] else ""
        age = f"{r['age_days']}d" if r["age_days"] is not None else "?"
        rec = str(r["recurrences"]) if r["recurrences"] > 1 else ""
        summ = r["summary"].replace("|", "\\|")
        
        
        
        
        if not r["machine"]:
            host = "**unknown**"
        elif r["machine"] == here:
            host = r["machine"]
        else:
            host = f"**{r['machine']}**"
        lines.append(f"| #{r['id']} | {r['severity']} | {r['status']} | {age}{flag} | "
                     f"{r['project']} | {host} | {rec} | {summ} |")
    if not rows:
        lines.append("| — | | | | | | | nothing open |")
    notes = [r for r in rows if r["last_note"]]
    if notes:
        lines += ["", "## Latest notes", ""]
        lines += [f"- **#{r['id']}** — {r['last_note']}" for r in notes]
    lines += ["", ("**Links** — `list_audit_observations(status=\"open\")` for full text + evidence; "
                   "resolve with `resolve_audit_observation(id, resolution_note=…)`; route a code "
                   "defect out with the same call."), ""]
    return {"path": path, "title": title, "rows": rows, "body": "\n".join(lines)}
