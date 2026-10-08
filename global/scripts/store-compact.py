#!/usr/bin/env python3
'Compact the agent-context store: reap mechanical garbage, and hand back a\nwork-list for everything that needs judgment.\n\nThe split is the whole design. Two kinds of "shrink the store" work exist:\n\n  JUDGMENT — rewriting a 200-char description down to 140, merging two memories\n  that overlap, deciding a worklog has stabilized or a handoff is spent. Compressing\n  prose is exactly\n  where information gets lost, so a script must not do it. This tool only\n  reports those, each with the MCP call that would fix it, for an agent (or a\n  human) to act on.\n\nNothing here ever edits a memory/doc BODY. The only size lever it recommends\nthat is lossless by construction is `upsert_memory(slug, load_behavior="lazy")` —\nthe memory stays fully readable via get_memory, it just stops paying for an\nindex row in every session.\n\n    store-compact.py              report only (default)\n    store-compact.py --apply      also archive spent observations and handoffs\n    store-compact.py --json       machine-readable, for the audit loop\n\nAGENT_CONTEXT_STORE names the store to act on (default ~/.agent-context).\n\nObservations guarded: #483.'
import json, os, re, sys, uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
OBS_DIR = os.path.join(STORE, "global/audit-observations")
OBS_ARCHIVE_DIR = os.path.join(STORE, "global/audit-observations-archive")
OBS_LEGACY = os.path.join(STORE, "global/audit-observations.json")   


def load_observations():
    'Active observations: one file each; falls back to the legacy aggregate on a\n    store that has not been migrated yet (a daemon < build 18 owns it).'
    if os.path.isdir(OBS_DIR):
        out = []
        for fn in sorted(os.listdir(OBS_DIR)):
            if fn.endswith(".json"):
                try:
                    out.append(json.load(open(os.path.join(OBS_DIR, fn), encoding="utf-8")))
                except (OSError, ValueError):
                    pass
        return out
    if os.path.exists(OBS_LEGACY):
        d = json.load(open(OBS_LEGACY, encoding="utf-8"))
        return d if isinstance(d, list) else d.get("observations", [])
    return []


OBS_REAP_DAYS = 30      
OBS_SPENT_STATUSES = ("resolved", "discarded")
OBS_STALE_DAYS = 30     
WORKLOG_STALE_DAYS = 45 
HANDOFF_SPENT_DAYS = 7  
SPENT_STATUS = re.compile(r"^[*][*]Status:[*][*] *(consumed|stale)(?![A-Za-z0-9_])", re.M)
STATUS_LINE = re.compile(r"^[*][*]Status:[*][*](.*)$", re.M)
STATUS_DATE = re.compile(r"(?<![0-9])([0-9]{4}-[0-9]{2}-[0-9]{2})(?![0-9])")
CODE_SPAN = re.compile(r"`[^`\n]*`")



HUSK = re.compile(r"\b(RETIRED|SUPERSEDED|DEPRECATED|OBSOLETE)\b")

NOW = datetime.now(timezone.utc)


def age_days(stamp):
    'Days since an ISO8601 stamp; None if unparseable.'
    if not stamp:
        return None
    try:
        d = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (NOW - d).days


def handoff_age(meta, body):
    'handoff age.'
    m = STATUS_LINE.search(body)
    if m:
        status = CODE_SPAN.sub("", m.group(1).split("**Opened:**")[0])
        ages = [a for a in map(age_days, STATUS_DATE.findall(status))
                if a is not None and a >= 0]
        if ages:
            return min(ages)
    return age_days(meta.get("updated_at"))


def frontmatter(path):
    'Minimal frontmatter read — store entities are always single-line JSON\n    values (emit_frontmatter writes them), so a full YAML parser is overkill.'
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return {}, ""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        try:
            meta[k.strip()] = json.loads(v.strip())
        except ValueError:
            meta[k.strip()] = v.strip()
    return meta, text[end + 5:]


def scope_dirs(sub):
    '(scope, directory) for `sub` in global, every workspace and every project.\n\n    The workspace tier is included: without it an entity scoped to a workspace is\n    invisible to this script and the reported corpus leaves out whole scopes.\n    Memories and handoffs both walk scopes through here, so the two cannot drift.'
    def _names(parent):
        d = os.path.join(STORE, parent)
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    return ([("global", os.path.join(STORE, "global", sub))]
            + [(f"ws:{w}", os.path.join(STORE, "workspaces", w, sub))
               for w in _names("workspaces")]
            + [(f"project:{p}", os.path.join(STORE, "projects", p, sub))
               for p in _names("projects")])


def memories():
    'Every memory file in the store, with its scope.'
    for scope, root in scope_dirs("memory"):
        if not os.path.isdir(root):
            continue
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".md"):
                continue
            meta, body = frontmatter(os.path.join(root, fn))
            if meta.get("type") == "memory":
                meta["scope"] = scope
                meta["_body"] = body
                yield meta


def scope_arg(scope):
    'The scope argument for an MCP call against this scope: `project=` for a project,\n    `workspace=` for a workspace. Passing `project=` for a workspace would name a\n    project that does not exist.'
    if scope == "global":
        return ""
    kind, name = scope.split(":", 1)
    return f', {"workspace" if kind == "ws" else "project"}="{name}"'


def spent_handoffs():
    'spent handoffs.'
    for scope, root in scope_dirs("docs"):
        d = os.path.join(root, "handoffs")
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            meta, body = frontmatter(os.path.join(d, fn))
            if meta.get("type") != "doc":
                continue
            m = SPENT_STATUS.search(body)
            age = handoff_age(meta, body)
            if m and age is not None and age >= HANDOFF_SPENT_DAYS:
                yield scope, fn, m.group(1), age, os.path.join(d, fn)





UUID_NS = uuid.UUID("a6f7c2e0-0000-5000-a000-000000000001")
HANDOFF_ARCHIVE = "archive/handoffs"


def stable_uuid(typ, scope, key):
    return str(uuid.uuid5(UUID_NS, f"{typ}|{scope}|{key}"))


def _archived_rel(rel):
    "Same rule as the server's graph.is_archived: a path segment that is exactly\n    `archive` or ends in `-archive`."
    return any(s == "archive" or s.endswith("-archive") for s in rel.split(os.sep))


def _rel_scope(rel):
    "The scope a store-relative path belongs to (the server's _scope_from_relpath)."
    parts = rel.split(os.sep)
    if parts[0] == "projects" and len(parts) > 1:
        return f"project:{parts[1]}"
    if parts[0] == "workspaces" and len(parts) > 1:
        return f"ws:{parts[1]}"
    return "global"


def _live_texts(skip):
    '(store-relative path, text) of every live Markdown file in the store: not under\n    an archive segment, not a dot-directory, and not one of the paths in `skip`.'
    out = []
    for top in ("global", "projects", "workspaces"):
        for dirpath, dirnames, files in os.walk(os.path.join(STORE, top)):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in files:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, STORE)
                if not fn.endswith(".md") or full in skip or _archived_rel(rel):
                    continue
                try:
                    out.append((rel, open(full, encoding="utf-8").read()))
                except (OSError, UnicodeDecodeError):
                    pass
    return out


def _pointer_re(fn):
    ' pointer re.'
    f, stem = re.escape(fn), re.escape(fn[:-len(".md")])
    return re.compile(rf"""get_doc\(\s*["']handoffs/{f}["']"""
                      rf"""(?:\s*,\s*(?:(project|workspace)\s*=\s*)?["']([^"']*)["'])?"""
                      rf"|\[\[handoffs/{stem}(?:\.md)?(?:[#|][^\]]*)?\]\]"
                      rf"|\]\([^)\s]*handoffs/{f}(?:#[^)]*)?\)")


def _pointer_scopes(m, holder_scope):
    'The scopes a pointer can resolve to: the project or workspace it names, else the\n    scope of the file that holds it; plus global, where get_doc falls back. A pointer\n    names a path, not a scope, so matching on the file name alone would keep same-named\n    handoffs in every other project too.'
    kw, name = m.group(1), m.group(2)
    if name:
        return {f"ws:{name}" if kw == "workspace" else f"project:{name}", "global"}
    return {holder_scope, "global"}


def handoff_holders(spent):
    '{(scope, file): [live paths that point at it]} for the spent handoffs a live file\n    still points at. Another spent handoff is not live: a finished chain moves whole.'
    texts = _live_texts({s[4] for s in spent})
    out = {}
    for scope, fn, *_ in spent:
        pat = _pointer_re(fn)
        held = [rel for rel, text in texts
                if any(scope in _pointer_scopes(m, _rel_scope(rel)) for m in pat.finditer(text))]
        if held:
            out[(scope, fn)] = held
    return out








_WIKILINK = re.compile(r"\[\[([^\]\[]+)\]\]")
_CODE_SPAN = re.compile(r"```.*?```|~~~.*?~~~|``.+?``|`[^`\n]+`", re.DOTALL)
_DOC_POINTER = re.compile(r"""\bget_doc\(\s*["']([^"']+)["']"""
                          r"""|\bget_entity\(\s*["']doc["']\s*,\s*["']([^"']+)["']""")


def _store_rel(path):
    return os.path.relpath(path, STORE).replace(os.sep, "/")


def _known_targets(gone):
    "(wikilink targets, doc pointer targets) as multisets, the server's _known_targets\n    read from the files: every Markdown file's store-relative path without `.md`, every\n    doc path and title, every memory slug. `gone` is the set of files a move removes;\n    they are left out, so the result is the store after the move."
    from collections import Counter
    wiki, docs = Counter(), Counter()
    for top in ("global", "projects", "workspaces"):
        for dirpath, _dirs, files in os.walk(os.path.join(STORE, top)):
            for fn in files:
                full = os.path.join(dirpath, fn)
                if not fn.endswith(".md") or full in gone:
                    continue
                wiki[_store_rel(full)[:-3]] += 1
                meta, _ = frontmatter(full)
                t = meta.get("type")
                keys = ([meta.get("path"), meta.get("title")] if t == "doc"
                        else [meta.get("slug")] if t == "memory" else [])
                for k in keys:
                    if isinstance(k, str) and k:
                        wiki[k] += 1
                        if t == "doc":
                            docs[k] += 1
    return wiki, docs


def _rewrite_maps(moves):
    '{old target: new target} for wikilinks and for doc pointers: each name a moved\n    handoff answered to (its store-relative file path and its doc path) that would\n    resolve to nothing after the moves, which is what check_integrity would report as\n    dangling. A doc path another scope still holds keeps resolving and is left alone.'
    if not moves:
        return {}, {}
    gone = {m["src"] for m in moves}
    wiki_after, docs_after = _known_targets(gone)
    wiki, ptr = {}, {}
    for m in moves:
        
        old_path = m.get("old_path") or f"handoffs/{m['fn']}"
        new_path = m.get("new_path") or f"{HANDOFF_ARCHIVE}/{m['fn']}"
        wiki[_store_rel(m["src"])[:-3]] = _store_rel(m["dst"])[:-3]
        if not wiki_after[old_path]:
            wiki[old_path] = new_path
        if not docs_after[old_path]:
            ptr[old_path] = new_path
    return wiki, ptr


def _rewrite_body(body, wiki_map, ptr_map):
    '(new body, [(old, new)]) with each mapped wikilink target and doc pointer\n    target replaced. `#anchor` and `|label` are kept byte for byte.'
    edits = []
    stripped = _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), body)
    for m in _WIKILINK.finditer(stripped):
        inner = body[m.start(1):m.end(1)]
        head, bar, label = inner.partition("|")
        tpart, hash_, anchor = head.partition("#")
        new_t = wiki_map.get(tpart.strip())
        if new_t:
            new = "[[" + tpart.replace(tpart.strip(), new_t, 1) + hash_ + anchor + bar + label + "]]"
            edits.append((m.start(), m.end(), new))
    for m in _DOC_POINTER.finditer(body):
        g = 1 if m.group(1) is not None else 2
        new_t = ptr_map.get(m.group(g))
        if new_t:
            new = body[m.start():m.start(g)] + new_t + body[m.end(g):m.end()]
            edits.append((m.start(), m.end(), new))
    edits.sort()
    out, changes, pos = [], [], 0
    for start, end, new in edits:
        if start < pos:
            continue            
        out.append(body[pos:start])
        out.append(new)
        changes.append((body[start:end], new))
        pos = end
    out.append(body[pos:])
    return "".join(out), changes




_TYPED_LINE = re.compile(r"^(supersedes|part_of|sibling|enforced_by|contradicts|client_of|"
                         r"depends_on): ", re.M)


def _rewrite_front(front, wiki_map):
    '(new frontmatter, [(old, new)]) with each typed-link target a move renames\n    replaced; a trailing `.md` is kept. Every other frontmatter line is left alone.'
    changes = []

    def one(m):
        target = m.group(1)
        stem = target[:-3] if target.endswith(".md") else target
        new_t = wiki_map.get(stem)
        if not new_t:
            return m.group(0)
        new = "[[" + new_t + target[len(stem):] + "]]"
        changes.append((m.group(0), new))
        return new

    lines = [_WIKILINK.sub(one, ln) if _TYPED_LINE.match(ln) else ln
             for ln in front.split("\n")]
    return "\n".join(lines), changes


def plan_rewrites(moves):
    '[{file, full, text, changes}] for every live Markdown file holding a reference\n    the moves would leave dangling: wikilinks and doc pointers in the body, typed links\n    in the frontmatter. Archived files and the docs being moved are frozen records and\n    are skipped.'
    wiki_map, ptr_map = _rewrite_maps(moves)
    if not wiki_map and not ptr_map:
        return []
    out = []
    for rel, text in _live_texts({m["src"] for m in moves}):
        split = 0
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            split = end + 5 if end != -1 else 0
        front, typed = _rewrite_front(text[:split], wiki_map)
        body, changes = _rewrite_body(text[split:], wiki_map, ptr_map)
        changes = typed + changes
        if changes:
            out.append({"file": rel, "full": os.path.join(STORE, rel),
                        "text": front + body,
                        "changes": [{"from": a, "to": b} for a, b in changes]})
    return out


def _rekey(text, scope, new_path):
    'The frontmatter with `path` and `uuid` set for new_path; every other byte kept.'
    end = text.find("\n---\n", 4)
    lines = text[4:end].splitlines()
    path_line = f"path: {json.dumps(new_path)}"
    uuid_line = f"uuid: {json.dumps(stable_uuid('doc', scope, new_path))}"
    lines = [path_line if ln.startswith("path:") else
             uuid_line if ln.startswith("uuid:") else ln for ln in lines]
    if uuid_line not in lines:
        lines.insert(0, uuid_line)
    if path_line not in lines:
        lines.append(path_line)
    return "---\n" + "\n".join(lines) + text[end:]


def plan_handoffs():
    'plan handoffs.'
    spent = list(spent_handoffs())
    holders = handoff_holders(spent)
    moves, kept = [], []
    for scope, fn, _status, _age, src in spent:
        ref = f"{scope}/handoffs/{fn}"
        if (scope, fn) in holders:
            kept.append({"ref": ref, "reason": "pointed at by " + ", ".join(holders[(scope, fn)])})
            continue
        dst = os.path.join(os.path.dirname(os.path.dirname(src)), *HANDOFF_ARCHIVE.split("/"), fn)
        with open(src, encoding="utf-8") as fh:
            new = _rekey(fh.read(), scope, f"{HANDOFF_ARCHIVE}/{fn}")
        finish = False
        if os.path.exists(dst):
            
            
            with open(dst, encoding="utf-8") as fh:
                finish = fh.read() == new
            if not finish:
                kept.append({"ref": ref, "reason": f"{os.path.relpath(dst, STORE)} already exists"})
                continue
        moves.append({"ref": ref, "scope": scope, "fn": fn, "src": src, "dst": dst,
                      "text": new, "finish": finish})
    return moves, kept


def _public(rewrites):
    return [{"file": r["file"], "changes": r["changes"]} for r in rewrites]


def apply_handoffs():
    'Move each spent handoff plan_handoffs() chose, then rewrite the live references\n    the moves would leave dangling.'
    moves, kept = plan_handoffs()
    rewrites = plan_rewrites(moves)
    return {"moved": write_moves(moves, rewrites), "kept": kept,
            "rewrites": _public(rewrites)}


def write_moves(moves, rewrites):
    'Write each planned move and rewrite; returns the refs moved. A move marked\n    `finish` already has its destination, so only its source is removed.'
    moved = []
    for m in moves:
        if not m["finish"]:
            os.makedirs(os.path.dirname(m["dst"]), exist_ok=True)
            tmp = m["dst"] + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(m["text"])
            os.replace(tmp, m["dst"])
        os.remove(m["src"])
        moved.append(m["ref"])
    for r in rewrites:
        tmp = r["full"] + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(r["text"])
        os.replace(tmp, r["full"])
    return moved


def scan():
    'Everything worth acting on, split by who may act.'
    reap, work = [], []

    for o in load_observations():
        age = age_days(o.get("resolved_date") or o.get("created_at"))
        if o.get("status") in OBS_SPENT_STATUSES and age is not None and age >= OBS_REAP_DAYS:
            reap.append(o)
        elif o.get("status") == "open" and age is not None and age >= OBS_STALE_DAYS:
            work.append({
                "kind": "stale-observation", "ref": f"observation #{o['id']}",
                "detail": f"open {age}d — triage or resolve",
                "fix": f'resolve_audit_observation({o["id"]}, status="resolved"|"triaged", resolution_note=...)',
                "saving": "audit backlog"})

    loaded = []
    for m in memories():
        slug, scope, desc = m.get("slug", "?"), m["scope"], m.get("description", "") or ""
        lazy = m.get("load_behavior") != "always"
        arg = scope_arg(scope)
        if not lazy:
            loaded.append((slug, scope, desc))

        
        
        
        
        

        husk_match = HUSK.search(desc)
        if not lazy and husk_match:
            work.append({
                "kind": "husk-description", "ref": f"{scope}/{slug}",
                "detail": f"index row self-labels as spent: {husk_match.group(0)}",
                "fix": f'verify the successor exists, then delete_entity("memory", "{slug}"{arg}) '
                       f"— or rewrite the description to the durable invariant",
                "saving": "one index row"})

        age = age_days(m.get("updated_at"))
        if (not lazy and m.get("memory_type") == "project"
                and age is not None and age >= WORKLOG_STALE_DAYS):
            work.append({
                "kind": "stabilized-worklog", "ref": f"{scope}/{slug}",
                "detail": f"project worklog, untouched {age}d — the store's prune-on-stabilize rule applies",
                "fix": f'upsert_memory("{slug}", load_behavior="lazy"{arg}) — lossless, '
                       f"still readable via get_memory; delete only if it holds no durable invariant",
                "saving": "one index row, zero information"})

    spent = list(spent_handoffs())
    holders = handoff_holders(spent)
    for scope, fn, status, age, _path in spent:
        held = holders.get((scope, fn))
        detail = f"handoff marked {status}, spent {age}d"
        fix = (f"store-compact.py --apply moves it to doc path "
               f'"{HANDOFF_ARCHIVE}/{fn}"{scope_arg(scope)}')
        if held:
            detail += f"; kept in place, pointed at by {', '.join(held)}"
            fix = f"re-point or drop the pointer in {', '.join(held)}, then {fix}"
        work.append({"kind": "spent-handoff", "ref": f"{scope}/handoffs/{fn}",
                     "detail": detail, "fix": fix,
                     "saving": "one stale doc out of search results"})

    
    
    def words(s):
        return {w for w in re.findall(r"[a-z0-9]{4,}", s.lower())}
    for i, (sa, sca, da) in enumerate(loaded):
        wa = words(da)
        if len(wa) < 4:
            continue
        for sb, scb, db in loaded[i + 1:]:
            wb = words(db)
            if len(wb) < 4 or sca != scb:
                continue
            overlap = len(wa & wb) / min(len(wa), len(wb))
            if overlap >= 0.5:
                work.append({
                    "kind": "possible-duplicate", "ref": f"{sca}/{sa} + {sb}",
                    "detail": f"descriptions share {overlap:.0%} of their terms",
                    "fix": "read both bodies; if they cover one topic, fold the unique halves "
                           "into one and delete_entity('memory', …) the other",
                    "saving": "one index row + duplicated body"})

    order = {"husk-description": 0, "long-description": 1, "stabilized-worklog": 2,
             "possible-duplicate": 3, "spent-handoff": 4, "stale-observation": 5}
    work.sort(key=lambda w: order.get(w["kind"], 9))
    return reap, work


def apply_reap(reap):
    'Archive spent observations rather than deleting them — the resolution\n    notes are the only record of why a guardrail looks the way it does.'
    if not reap:
        return 0
    if not os.path.isdir(OBS_DIR):
        print("store-compact: observations not yet migrated to per-record files "
              "(daemon < build 18 still owns the aggregate) — nothing archived", file=sys.stderr)
        return 0
    os.makedirs(OBS_ARCHIVE_DIR, exist_ok=True)
    n = 0
    for o in reap:
        src = os.path.join(OBS_DIR, f"{int(o['id']):04d}.json")
        if os.path.exists(src):
            os.replace(src, os.path.join(OBS_ARCHIVE_DIR, os.path.basename(src)))
            n += 1
    return n


def selftest():
    'Guards the two things that would silently corrupt a run: the date math\n    and the reap/keep partition.'
    assert age_days(None) is None and age_days("not-a-date") is None
    assert age_days(NOW.isoformat()) == 0
    old_age = age_days("2026-01-01T00:00:00Z")
    assert old_age is not None and old_age > 100
    assert scope_arg("global") == "" and scope_arg("project:Foo") == ', project="Foo"'
    assert scope_arg("ws:W") == ', workspace="W"'
    old, new = "2020-01-01T00:00:00Z", NOW.isoformat()
    sample = [{"id": 1, "status": "resolved", "resolved_date": old},
              {"id": 2, "status": "resolved", "resolved_date": new},
              {"id": 3, "status": "open", "created_at": old},
              {"id": 4, "status": "open", "created_at": new}]
    reaped = [o for o in sample
              if o["status"] == "resolved"
              and (age := age_days(o.get("resolved_date"))) is not None and age >= OBS_REAP_DAYS]
    assert [o["id"] for o in reaped] == [1], reaped
    stale = [o for o in sample
             if o["status"] == "open"
             and (age := age_days(o.get("created_at"))) is not None and age >= OBS_STALE_DAYS]
    assert [o["id"] for o in stale] == [3], stale
    print("selftest ok")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    reap, work = scan()
    archived, would_rewrite = None, None
    if "--apply" in sys.argv:
        h = apply_handoffs()
        archived = {"observations": apply_reap(reap), "handoffs": len(h["moved"]),
                    "moved": h["moved"], "kept": h["kept"], "rewrites": h["rewrites"]}
    else:
        would_rewrite = _public(plan_rewrites(plan_handoffs()[0]))

    if "--json" in sys.argv:
        doc = {"reapable": len(reap), "work": work}
        if archived is not None:
            doc["archived"] = archived
        else:
            doc["would_rewrite"] = would_rewrite
        print(json.dumps(doc, indent=2))
        return
    print(f"store-compact — {len(reap)} spent record(s) reapable, {len(work)} item(s) need judgment\n")
    for w in work:
        print(f"[{w['kind']}] {w['ref']}\n    {w['detail']}\n    fix: {w['fix']}\n    saves: {w['saving']}\n")
    if not work:
        print("nothing needs judgment.\n")
    if archived is not None:
        print(f"archived {archived['observations']} spent observation(s) -> {OBS_ARCHIVE_DIR}/")
        print(f"archived {archived['handoffs']} spent handoff(s) -> docs/{HANDOFF_ARCHIVE}/")
        for k in archived["kept"]:
            print(f"  kept {k['ref']}: {k['reason']}")
        _print_rewrites("rewrote", archived["rewrites"])
    else:
        _print_rewrites("--apply would rewrite", would_rewrite)
        if reap:
            print(f"(re-run with --apply to archive {len(reap)} spent observation(s))")


def _print_rewrites(verb, rewrites):
    if not rewrites:
        return
    n = sum(len(r["changes"]) for r in rewrites)
    print(f"{verb} {n} reference(s) to archived handoffs in {len(rewrites)} file(s):")
    for r in rewrites:
        for c in r["changes"]:
            print(f"  {r['file']}: {c['from']} -> {c['to']}")


if __name__ == "__main__":
    
    
    if os.environ.get("AGENT_CONTEXT_SERVER_TASK") == "1":
        sys.exit(main())
    import store_task
    store_task.main_or_forward("store-compact", main)
