'The store\'s link graph, derived in memory from entity bodies (context graph T2).\n\nFiles stay the source of truth; nothing here is stored. Each entity\'s links are parsed\nonce per version and cached by uuid, outside `store.entities`, so a sync `reload()`\nre-parses only the files that changed. Resolution and backlinks are rebuilt from that\ncache whenever `store.graph_generation` moves, which `_index`, `_forget` and `delete`\nbump.\n\nSections (T4): a `[[target#Heading]]` link is an edge to the entity, with its anchor\nrecorded beside the edges for check_integrity. Headings are parsed by sections.py on\nfirst need and cached here under the same version stamp as the links. The plan and its\ncriteria: get_doc("context-graph/plan.md").\n\nTyped links (T5): one frontmatter key per relation (`supersedes`, `part_of`, `sibling`,\n`enforced_by`, `contradicts`) holds a list of "[[target]]" strings. A typed target resolves\nlike a body link, and also to a skill, command or script by name. A hook target\nrequires `hook:<name>`; bare hook names retain their integrity-claim behavior. `rels` records the\nrelations of each (source, target) pair beside `out` and `back`, which keep their shape.\nHooks and scripts can carry explicit typed relations, but their executable bodies are\nnever scanned for prose links. An entity named under\n`supersedes` is superseded: it leaves other entities\' cards and explore\nexpansion, and a read of it carries `superseded_by`.'
from __future__ import annotations

import re
import unicodedata
import weakref
from pathlib import Path

from . import sections, usage
from .refs import (
    _LINKISH,
    _PLACEHOLDER,
    _scan_markdown_doc_links,
    _scan_pointers,
    _scan_wikilink_anchors,
)
from .store import NATURAL_KEY



CARD_KINDS = ("memory", "doc", "skill", "command")

SOURCE_KINDS = ("memory", "doc", "instruction", "skill", "command")


NODE_KINDS = (*SOURCE_KINDS, "script", "hook")

RELATIONS = ("mentions", "supersedes", "part_of", "sibling", "enforced_by", "contradicts",
             "client_of", "depends_on")
NAV_RELATIONS = ("in_scope",)

TYPED = RELATIONS[1:]


_TYPED_KINDS = ("memory", "doc", "title", "skill", "command", "script")
CARD_CAP = 12


EXPAND_MAX_BYTES = 64 * 1024
_DESC_MAX = 72
_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_GRAPHS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_ZWJ = "‍"


def _body(e):
    return str((e.get("script_body") if e.get("type") in ("script", "hook")
                else e.get("body")) or "")


def is_archived(e):
    'A frozen doc, identified by directory segments rather than name substrings.'
    path = e.get("path") or ""
    return any(segment == "archive" or segment.endswith("-archive")
               for segment in path.split("/")[:-1])


def _key(e):
    return str(e.get(NATURAL_KEY.get(e.get("type") or "", "name")) or "")


def _stamp(e, body):
    return (e.get("_mtime"), len(body), hash(body), repr([e.get(r) for r in TYPED]))


def _parse(body, source_path=None):
    'Wikilinks, pointer calls and local Markdown doc links in one body.'
    text = _FENCE.sub(lambda m: " " * len(m.group(0)), body)
    links = [(t, a) for t, a in _scan_wikilink_anchors(text) if not _PLACEHOLDER.search(t)]
    pointers = [(k, t, None if not p or _PLACEHOLDER.search(p) else p)
                for k, t, p in _scan_pointers(text) if not _PLACEHOLDER.search(t)]
    markdown = _scan_markdown_doc_links(body, source_path)
    return links, pointers, markdown


def _typed(e):
    '[(relation, target, anchor|None)] from the entity\'s relation keys. A value holds\n    "[[target]]" strings; a bare "target" is accepted, and so is one string in place of a\n    list, the shape a single Obsidian property can come back in.'
    out = []
    for rel in TYPED:
        value = e.get(rel)
        items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        for item in items:
            if not isinstance(item, str) or not item.strip():
                continue
            if "[[" in item:
                found = _scan_wikilink_anchors(item)
            else:
                t, _, anchor = item.strip().split("|", 1)[0].partition("#")
                found = [(t.strip(), anchor.strip() or None)] if t.strip() else []
            out.extend((rel, t, a) for t, a in found if not _PLACEHOLDER.search(t))
    return out


def _scoped_target(written):
    'Return (scope, kind, key), or None for the established unqualified syntax.'
    if "::" not in written:
        return None
    scope_part, separator, remainder = written.partition("::")
    kind, kind_separator, key = remainder.partition(":")
    if not separator or not kind_separator or not key:
        return None
    if scope_part == "global":
        scope = "global"
    elif scope_part.startswith("project:") and scope_part[len("project:"):]:
        scope = scope_part
    elif scope_part.startswith("workspace:") and scope_part[len("workspace:"):]:
        scope = f"ws:{scope_part[len('workspace:'):]}"
    else:
        return None
    if kind not in NODE_KINDS:
        return None
    return scope, kind, key


def _scoped_target_error(written):
    if "::" not in written:
        return None
    parsed = _scoped_target(written)
    if parsed is not None:
        return None
    return ("scoped target must use [[project:Name::kind:key]], "
            "[[workspace:Name::kind:key]], or [[global::kind:key]] with a graph kind "
            f"and nonempty key, got '{written}'")


def _vault_path(store, e):
    'The Obsidian vault path for an entity file, when it has one.'
    path = e.get("_path")
    if not path or Path(path).name.endswith(".meta.toml"):
        return None
    try:
        relative = Path(path).relative_to(store.root)
    except ValueError:
        return None
    return relative.as_posix()


def _vault_paths(store):
    'Vault-relative entity paths indexed to their source entities.'
    paths = {}
    for uid, e in store.entities.items():
        path = _vault_path(store, e)
        if not path:
            continue
        paths[path] = uid
        if path.endswith(".md") and e.get("type") not in ("script", "hook"):
            paths.setdefault(path[:-3], uid)
    return paths


def _typed_target_uid(store, written, source_scope):
    'Resolve one typed target for write-time Obsidian serialization.'
    paths = _vault_paths(store)
    if written in paths:
        return paths[written]
    scoped = _scoped_target(written)
    if scoped is not None:
        scope, kind, key = scoped
        return store.by_key.get((kind, scope, key))
    kind, separator, bare = written.partition(":")
    target = bare if separator and bare and kind in ("script", "hook") else written
    kinds = (kind,) if separator and bare and kind in ("script", "hook") else (
        _TYPED_KINDS if _LINKISH.match(written) else ("title",))
    titles = {(e.get("scope") or "global", e["title"]): uid
              for uid, e in store.entities.items()
              if e.get("type") == "doc" and e.get("title")}
    for scope in _Graph._chain(store, source_scope):
        for candidate in kinds:
            uid = (titles.get((scope, target)) if candidate == "title"
                   else store.by_key.get((candidate, scope, target)))
            if uid is not None:
                if (len(kinds) > 1 and store.entities[uid].get("type") in ("script", "hook")
                        and store.by_key.get(("script", scope, target)) is not None
                        and store.by_key.get(("hook", scope, target)) is not None):
                    return None
                return uid
    if source_scope == "global":
        found = {uid for uid, e in store.entities.items() if (
            (e.get("type") in kinds and _key(e) == target)
            or ("title" in kinds and e.get("type") == "doc" and e.get("title") == target))}
        if len(found) == 1:
            return found.pop()
    return None


class _Graph:
    def __init__(self):
        self.generation = None
        self.parsed = {}      
        self.size = {}        
        self.out = {}         
        self.back = {}        
        self.ambiguous = []   
        self.anchors = []     
        self.heads = {}       
        self.rels = {}        
        self.typed = []       
        self.superseded = {}  
        self.cycles = []      
        self.collisions = []  

    def rebuild(self, store):
        'Call under `store.lock`.'
        self.generation = store.graph_generation
        ents = store.entities
        parsed, size = {}, {}
        for uid, e in ents.items():
            typ = e.get("type")
            if typ not in NODE_KINDS:
                continue
            body = _body(e)
            stamp = _stamp(e, body)
            hit = self.parsed.get(uid)
            if hit is None or hit[0] != stamp:
                hit = (stamp, *(_parse(body, e.get("path") if typ == "doc" else None)
                                if typ in SOURCE_KINDS else ([], [], [])), _typed(e))
                size[uid] = len(body.encode())
            else:
                size[uid] = self.size[uid]
            parsed[uid] = hit
        self.parsed, self.size = parsed, size
        self.heads = {u: h for u, h in self.heads.items() if u in parsed}
        self._resolve(store)

    def _resolve(self, store):
        ents, by_key = store.entities, store.by_key
        titles, anywhere, paths = {}, {}, _vault_paths(store)
        for (typ, scope, key), uid in by_key.items():
            if typ in NODE_KINDS and uid in self.parsed:
                anywhere.setdefault((typ, key), []).append(uid)
                if typ == "doc" and ents[uid].get("title"):
                    titles.setdefault((scope, ents[uid]["title"]), uid)
                    anywhere.setdefault(("title", ents[uid]["title"]), []).append(uid)

        def at(kinds, target, scope):
            uid = paths.get(target)
            if uid in self.parsed:
                return uid
            for k in kinds:
                uid = titles.get((scope, target)) if k == "title" else by_key.get((k, scope, target))
                if uid in self.parsed:
                    return uid
            return None

        chains = {}
        out, back, ambiguous, anchors = {}, {}, [], []
        rels, typed_hits, superseded, collisions = {}, [], {}, []
        for uid, (_stamp, links, pointers, markdown, typed) in self.parsed.items():
            if not links and not pointers and not markdown and not typed:
                continue
            own = ents[uid].get("scope") or "global"
            refs: list[tuple[tuple[str, ...], str, str | None, str | None, str, str]] = [
                (("memory", "doc", "title") if _LINKISH.match(t) else ("title",), t,
                 None, a, "mentions", t) for t, a in links]
            refs += [(("doc", "title") if k == "doc" else (k,), t, p, None, "mentions", t)
                     for k, t, p in pointers]
            refs += [(("doc",), t, None, a, "mentions", t) for t, a in markdown]
            for rel, written, anchor in typed:
                scoped = _scoped_target(written)
                if scoped is not None:
                    scope, kind, target = scoped
                    refs.append(((kind,), target, f"!{scope}", anchor, rel, written))
                    continue
                kind, sep, bare = written.partition(":")
                qualified = sep and bare and kind in ("script", "hook")
                kinds = (kind,) if qualified else (
                    _TYPED_KINDS if _LINKISH.match(written) else ("title",))
                refs.append((kinds, bare if qualified else written, None, anchor, rel,
                             written))
            targets = []
            for kinds, target, project, anchor, rel, written in refs:
                start = f"project:{project}" if project else own
                if project and project.startswith("!"):
                    start = project[1:]
                    chain = [start]
                else:
                    if start not in chains:
                        chains[start] = self._chain(store, start)
                    chain = chains[start]
                hit = next((u for s in chain if (u := at(kinds, target, s))), None)
                if hit is None and not project and chain == ["global"]:
                    
                    
                    found = sorted({u for k in kinds for u in anywhere.get((k, target), ())})
                    if len(found) == 1:
                        hit = found[0]
                    elif found:
                        ambiguous.append((uid, written, sorted({ents[u]["scope"] for u in found})))
                if (hit and rel != "mentions" and len(kinds) > 1
                        and ents[hit]["type"] in ("script", "hook")):
                    
                    
                    where = ents[hit].get("scope") or "global"
                    script = by_key.get(("script", where, target))
                    hook = by_key.get(("hook", where, target))
                    if script in self.parsed and hook in self.parsed:
                        collisions.append((uid, written, where, [script, hook]))
                        hit = None
                if hit and "title" in kinds and "memory" in kinds and ents[hit]["type"] == "memory":
                    
                    
                    where = ents[hit].get("scope") or "global"
                    doc = titles.get((where, target))
                    if doc in self.parsed and doc != hit:
                        collisions.append((uid, written, where, [hit, doc]))
                if rel != "mentions":
                    typed_hits.append((uid, rel, written, hit))
                if hit and anchor:
                    anchors.append((uid, hit, written, anchor))
                if hit and hit != uid:
                    if hit not in targets:
                        targets.append(hit)
                    rels.setdefault((uid, hit), set()).add(rel)
                    if rel == "supersedes" and uid not in superseded.setdefault(hit, []):
                        superseded[hit].append(uid)
            if targets:
                out[uid] = targets
                for t in targets:
                    back.setdefault(t, []).append(uid)
        
        
        roots = {"global": by_key.get(("doc", "global", "agent-context-store.md"))}
        for uid, e in ents.items():
            if uid not in self.parsed or is_archived(e):
                continue
            scope = e.get("scope") or "global"
            if scope not in roots:
                name = scope.partition(":")[2]
                roots[scope] = by_key.get(("doc", scope, f"{name}.md"))
        for uid in self.parsed:
            e = ents[uid]
            if is_archived(e):
                continue
            scope = e.get("scope") or "global"
            root = roots.get(scope)
            if uid == root:
                if scope.startswith("project:"):
                    ws = store._ws_scope(scope[len("project:"):])
                    root = (roots.get(ws) if ws else None) or roots.get("global")
                elif scope.startswith("ws:"):
                    root = roots.get("global")
                else:
                    root = None
            if root and root != uid and root in self.parsed:
                targets = out.setdefault(uid, [])
                if root not in targets:
                    targets.append(root)
                    back.setdefault(root, []).append(uid)
                rels.setdefault((uid, root), set()).add("in_scope")
        self.out, self.back, self.ambiguous, self.anchors = out, back, ambiguous, anchors
        
        
        
        edges = {}
        for (source, target), have in rels.items():
            if "supersedes" in have:
                edges.setdefault(source, []).append(target)
        cycles = _cycles(edges)
        for group in cycles:
            for member in group:
                superseded.pop(member, None)
        self.rels, self.typed, self.cycles = rels, typed_hits, cycles
        self.superseded, self.collisions = superseded, collisions

    @staticmethod
    def _chain(store, scope):
        'Scopes a link resolves through, nearest first: project, workspace, global.'
        if scope.startswith("project:"):
            ws = store._ws_scope(scope[len("project:"):])
            return [scope, *([ws] if ws else []), "global"]
        if scope.startswith("ws:"):
            return [scope, "global"]
        return ["global"]


def _cycles(edges):
    'Groups of two or more nodes that reach each other through `edges` (source ->\n    [target]), each sorted, in a stable order. Self-links never reach `edges`.'
    def reach(start):
        seen, stack = set(), [start]
        while stack:
            for n in edges.get(stack.pop(), ()):
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        return seen

    reached = {u: reach(u) for u in edges}
    groups = {frozenset({u} | {v for v in r if u in reached.get(v, ())})
              for u, r in reached.items() if u in r}
    return sorted(sorted(g) for g in groups if len(g) > 1)


def graph_for(store):
    'The current graph for `store`, rebuilt first if the index changed.'
    with store.lock:
        g = _GRAPHS.get(store)
        if g is None:
            g = _GRAPHS[store] = _Graph()
        if g.generation != store.graph_generation:
            g.rebuild(store)
        return g


def coverage(store):
    'Structural coverage now, independent of the usage window for pruning.'
    g = graph_for(store)
    ents = store.entities
    nodes = set(g.size)
    adjacent = {uid: set(g.out.get(uid, ())) | set(g.back.get(uid, ())) for uid in nodes}
    semantic = {uid: set() for uid in nodes}
    for (source, target), relations in g.rels.items():
        if relations - {"in_scope"}:
            semantic[source].add(target)
            semantic[target].add(source)
    unseen = set(nodes)
    sizes = []
    while unseen:
        start = unseen.pop()
        pending = [start]
        size = 0
        while pending:
            uid = pending.pop()
            size += 1
            neighbors = adjacent[uid] & unseen
            unseen.difference_update(neighbors)
            pending.extend(neighbors)
        sizes.append(size)
    by_kind = {}
    by_scope = {}
    isolated = []
    semantic_isolated_prose_items = []
    scopes = set()
    for uid in sorted(nodes):
        e = ents[uid]
        kind = e["type"]
        scope = e.get("scope") or "global"
        archived = is_archived(e)
        if not archived:
            scopes.add(scope)
        for group, key in ((by_kind, kind), (by_scope, scope)):
            row = group.setdefault(key, {"nodes": 0, "isolated_active": 0,
                                         "semantic_isolated_active": 0})
            row["nodes"] += 1
            if not archived and not adjacent[uid]:
                row["isolated_active"] += 1
            if not archived and not semantic[uid]:
                row["semantic_isolated_active"] += 1
        if not adjacent[uid]:
            isolated.append({**_row(e), "archived": archived})
        if not archived and not semantic[uid] and kind not in ("script", "hook"):
            semantic_isolated_prose_items.append(_row(e))
    missing_roots = []
    for scope in sorted(scopes):
        name = scope.partition(":")[2]
        path = "agent-context-store.md" if scope == "global" else f"{name}.md"
        if ("doc", scope, path) not in store.by_key:
            missing_roots.append({"scope": scope, "expected_path": path})
    return {
        "nodes": len(nodes),
        "edges": sum(len(v) for v in g.out.values()),
        "semantic_edges": sum(bool(rels - {"in_scope"}) for rels in g.rels.values()),
        "components": len(sizes),
        "largest_component": max(sizes, default=0),
        "isolated_active": sum(not r["archived"] for r in isolated),
        "isolated_archived": sum(r["archived"] for r in isolated),
        "semantic_isolated_active": sum(not is_archived(ents[u]) and not semantic[u]
                                        for u in nodes),
        "semantic_isolated_executable": sum(
            ents[u]["type"] in ("script", "hook") and not semantic[u] for u in nodes),
        "semantic_isolated_prose": len(semantic_isolated_prose_items),
        "semantic_isolated_prose_items": semantic_isolated_prose_items,
        "zero_inbound_active": sum(not is_archived(ents[u]) and not g.back.get(u)
                                   for u in nodes),
        "by_kind": by_kind,
        "by_scope": by_scope,
        "isolated": isolated,
        "missing_scope_roots": missing_roots,
    }


def headings(store, e):
    "The entity's headings (sections.parse), parsed once per body version and kept\n    with the link parse, so a 683 KB ledger is not re-parsed on every read."
    g = graph_for(store)
    body = _body(e)
    stamp = _stamp(e, body)
    with store.lock:
        hit = g.heads.get(e["uuid"])
        if hit is None or hit[0] != stamp:
            hit = g.heads[e["uuid"]] = (stamp, sections.parse(body))
        return hit[1]


def read(store, e, out, section=None):
    "A read's result for `e`, cut to `section` or carrying a toc (sections.shape). A read\n    of a superseded entity also carries `superseded_by`."
    by = superseded_by(store, e)
    if by:
        out["superseded_by"] = by
    return sections.shape(out, _body(e), section, lambda: headings(store, e),
                          f"{e.get('type')} '{_key(e)}'")


def _fmt(n):
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _continues(ch):
    'Does `ch` belong to the character before it? Marks, joiners, variation selectors,\n    skin tone modifiers and tag characters do.'
    return (unicodedata.category(ch) in ("Mn", "Mc", "Me") or ch in "‌‍"
            or "\U0001f3fb" <= ch <= "\U0001f3ff" or "\U000e0020" <= ch <= "\U000e007f")


def _regional(ch):
    return "\U0001f1e6" <= ch <= "\U0001f1ff"


def _mid_flag(text, i):
    'Does a cut before text[i] fall between the two regional indicators of a flag?'
    if not _regional(text[i]):
        return False
    k = i
    while k > 0 and _regional(text[k - 1]):
        k -= 1
    return (i - k) % 2 == 1


def _cut(text, n):
    'The first `n` code points of `text`, shortened until the cut splits nothing a reader\n    sees as one character. An approximation of UAX #29 grapheme clusters, which the\n    standard library does not provide (T2 review: a code-point cut split emoji).'
    i = min(n, len(text))
    while 0 < i < len(text) and (_continues(text[i]) or text[i - 1] == _ZWJ
                                 or _mid_flag(text, i)):
        i -= 1
    return text[:i]


def _card(arrow, rel, e, size, count=None):
    'One card line. `count`, when given, is the section count of a large target.'
    kind, key = e.get("type"), _key(e)
    desc = e.get("title") if kind == "doc" else e.get("description")
    shown = _fmt(size) + (f", {count} section{'' if count == 1 else 's'}" if count else "")
    line = f"{arrow} {rel} {kind} {key} ({shown})"
    desc = " ".join(str(desc or "").split())
    if desc and desc != key:
        line += ": " + (desc if len(desc) <= _DESC_MAX else _cut(desc, _DESC_MAX - 1) + "…")
    return line


def _count(store, g, uid):
    'The section count a card shows: only for a large body a section read can address.'
    e = store.entities[uid]
    if e.get("type") not in sections.KINDS or g.size.get(uid, 0) <= sections.TOC_MIN_BYTES:
        return None
    return len(headings(store, e))


def _reads(e):
    return usage.stats(e.get("type") or "", e.get("scope") or "global", _key(e))["reads"]


def _label(g, source, target, rel=None):
    "The relation a card shows for one pair: the filter's when given, else the first\n    typed relation in RELATIONS order, else mentions."
    if rel is not None:
        return rel
    have = g.rels.get((source, target), ())
    return next((r for r in TYPED if r in have),
                "mentions" if "mentions" in have else "in_scope")


def _neighbors(g, store, uid, rel=None, limit=None):
    '(arrow, uuid) around one node in card order: typed out-links, then mention out-links,\n    each by their backlink count; then backlinks by reads, ties by key. `rel` keeps only\n    pairs that carry it. A superseded neighbor is left out unless the pair is its supersedes\n    link. `limit` skips ranking what cannot be shown.'
    ents = store.entities

    def keep(pair, n):
        have = g.rels.get(pair, ())
        return (rel is None or rel in have) and (n not in g.superseded or "supersedes" in have)

    outs = sorted((t for t in g.out.get(uid, ()) if t in ents and keep((uid, t), t)),
                  key=lambda t: (_label(g, uid, t) == "in_scope",
                                 _label(g, uid, t) == "mentions", -len(g.back.get(t, ())),
                                 _key(ents[t]), ents[t]["type"]))
    pairs = [("→", t) for t in outs]
    backs = [s for s in g.back.get(uid, ()) if s in ents and keep((s, uid), s)]
    if limit is not None and len(pairs) >= limit:
        return pairs + [("←", s) for s in backs]
    backs.sort(key=lambda s: (-_reads(ents[s]), _key(ents[s]), ents[s]["type"]))
    return pairs + [("←", s) for s in backs]


def cards(store, e):
    'The `links` lines for one read: at most CARD_CAP cards, then an overflow line.'
    g = graph_for(store)
    uid = e["uuid"]
    pairs = _neighbors(g, store, uid, limit=CARD_CAP)
    ents = store.entities
    lines = [_card(arrow, _label(g, *((uid, n) if arrow == "→" else (n, uid))), ents[n],
                   g.size.get(n, 0), _count(store, g, n))
             for arrow, n in pairs[:CARD_CAP]]
    if len(pairs) > CARD_CAP:
        lines.append(f'+{len(pairs) - CARD_CAP} more: explore("{e.get("type")}", "{_key(e)}")')
    return lines


def explore(store, kind, key, depth=1, rel=None, budget_bytes=4000, project=None,
            workspace=None):
    'Cards around one entity, breadth-first to `depth`, cut before `budget_bytes`.\n\n    A card below depth 1 starts with the key of the node it was reached from. A body over\n    EXPAND_MAX_BYTES is carded but its neighbors are not walked, unless it is the start; so\n    is a superseded entity. Returns None when the entity does not exist, like the other\n    readers.'
    if kind not in NODE_KINDS:
        return {"error": f"unknown kind '{kind}' (expected one of: {', '.join(NODE_KINDS)})"}
    
    if type(depth) is not int or depth not in (1, 2):
        return {"error": f"depth must be 1 or 2, got {depth!r}"}
    if rel is not None and rel not in (*RELATIONS, *NAV_RELATIONS):
        return {"error": f"unknown rel '{rel}' (expected one of: "
                         f"{', '.join((*RELATIONS, *NAV_RELATIONS))})"}
    e = store.get(kind, key, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return None
    g = graph_for(store)
    ents = store.entities
    seen, frontier, found = {e["uuid"]}, [e["uuid"]], []
    for level in range(1, depth + 1):
        nxt = []
        for parent in frontier:
            if level > 1 and (g.size.get(parent, 0) > EXPAND_MAX_BYTES
                              or parent in g.superseded):
                continue
            for arrow, n in _neighbors(g, store, parent, rel):
                if n not in seen:
                    seen.add(n)
                    found.append((parent, level > 1, arrow, n))
                    nxt.append(n)
        frontier = nxt
    out, used = [], 0
    for parent, below, arrow, n in found:
        pair = (parent, n) if arrow == "→" else (n, parent)
        line = _card(arrow, _label(g, *pair, rel), ents[n], g.size.get(n, 0),
                     _count(store, g, n))
        if below:
            line = f"{_key(ents[parent])} {line}"
        cost = len(line.encode())
        if used + cost > budget_bytes:
            break
        out.append(line)
        used += cost
    return {"start": f"{kind} {_key(e)}", "scope": e.get("scope"), "depth": depth,
            "cards": out, "truncated": len(found) - len(out)}


def ambiguous_links(store):
    'Links from a global source whose target exists in several other scopes, and not at\n    global. No edge is drawn for them; check_integrity reports them.'
    g = graph_for(store)
    rows, seen = [], set()
    for uid, target, scopes in g.ambiguous:
        e = store.entities.get(uid)
        if e is None or (uid, target) in seen:
            continue
        seen.add((uid, target))
        rows.append({"source_type": e.get("type"), "scope": e.get("scope"), "source": _key(e),
                     "target": target, "candidates": scopes})
    
    
    ents = store.entities
    for uid, target, scope, found in g.collisions:
        e = ents.get(uid)
        if e is None or (uid, target) in seen:
            continue
        seen.add((uid, target))
        rows.append({"source_type": e.get("type"), "scope": e.get("scope"), "source": _key(e),
                     "target": target, "candidates": [scope],
                     "matches": sorted(_name(ents[u]) for u in found if u in ents)})
    return sorted(rows, key=lambda r: (r["source_type"] or "", r["source"], r["target"]))


def dangling_anchors(store):
    'Section links, `[[target#Heading]]`, whose heading the resolved target lacks.\n\n    A block reference (`#^id`) is not a heading and is skipped; `#A#B` is checked on B. A\n    target that resolves to nothing is a dangling link, reported there instead.'
    g = graph_for(store)
    ents, rows, seen, known = store.entities, [], set(), {}
    for uid, hit, target, anchor in g.anchors:
        e, t = ents.get(uid), ents.get(hit)
        heading = anchor.rsplit("#", 1)[-1].strip()
        if e is None or t is None or heading.startswith("^") or (uid, target, anchor) in seen:
            continue
        seen.add((uid, target, anchor))
        if hit not in known:
            known[hit] = {h[1].casefold() for h in headings(store, t)}
        if sections.norm(heading) not in known[hit]:
            rows.append({"source_type": e.get("type"), "scope": e.get("scope"),
                         "source": _key(e), "target": target, "anchor": anchor})
    return sorted(rows, key=lambda r: (r["source_type"] or "", r["source"], r["target"],
                                       r["anchor"]))


def unaddressable_large_bodies(store):
    'Bodies over sections.TOC_MIN_BYTES with no heading, which a reader can only take\n    whole. A generated one is fixed at its generator, not by hand. A doc whose path does\n    not end in `.md` is a sidecar (benchmark JSON, a text listing) that cannot take\n    headings, so it is not reported.'
    g = graph_for(store)
    rows = []
    for uid, n in list(g.size.items()):
        e = store.entities.get(uid)
        if (n > sections.TOC_MIN_BYTES and e is not None and e.get("type") in sections.KINDS
                and not (e["type"] == "doc" and not _key(e).lower().endswith(".md"))
                and not headings(store, e)):
            rows.append({"type": e.get("type"), "scope": e.get("scope"), "key": _key(e),
                         "bytes": n})
    return sorted(rows, key=lambda r: (r["type"] or "", r["scope"] or "", r["key"]))




def _name(e):
    return f"{e.get('type')} {_key(e)}"


def _row(e):
    return {"type": e.get("type"), "scope": e.get("scope"), "key": _key(e)}


def _bare_target(t):
    "A typed target's entity name: brackets, an `|alias` and a `#section` removed,\n    parsed the way `_typed` reads the same string back off the file."
    inner = t.strip()
    if inner.startswith("[[") and inner.endswith("]]"):
        inner = inner[2:-2]
    return inner.split("|", 1)[0].partition("#")[0].strip()


def _link_suffix(t):
    'The anchor and alias from a supplied wikilink, in Obsidian order.'
    inner = t.strip()
    if inner.startswith("[[") and inner.endswith("]]"):
        inner = inner[2:-2]
    target, alias_separator, alias = inner.partition("|")
    _name, anchor_separator, anchor = target.partition("#")
    return (f"#{anchor}" if anchor_separator else "") + (
        f"|{alias}" if alias_separator else "")


def link_fields(links, key=None, kind=None, scope=None, store=None) -> tuple[dict, dict | None]:
    'An upsert\'s `links={relation: [targets]}` as frontmatter fields: each target a\n    "[[target]]" string, and `[]` to remove the key. Returns (fields, None), or\n    ({}, error): the caller returns the error and writes nothing.'
    if links is None:
        return {}, None
    if not isinstance(links, dict):
        return {}, {"error": "links must map a relation to a list of targets, e.g. "
                             '{"supersedes": ["old-slug"]}'}
    fields = {}
    for rel, targets in links.items():
        if rel not in TYPED:
            return {}, {"error": f"unknown relation '{rel}' (expected one of: "
                                 f"{', '.join(TYPED)}; body links are mentions)"}
        if (not isinstance(targets, (list, tuple))
                or not all(isinstance(t, str) and t.strip() for t in targets)):
            return {}, {"error": f"links['{rel}'] must be a list of target names, "
                                 f"got {targets!r}"}
        stripped = [t.strip() for t in targets]
        for t in stripped:
            error = _scoped_target_error(_bare_target(t))
            if error:
                return {}, {"error": error}
        serialized = []
        for t in stripped:
            target = _bare_target(t)
            target_uid = _typed_target_uid(store, target, scope) if store is not None else None
            if key is not None:
                scoped = _scoped_target(target)
                source_uid = (store.by_key.get((kind, scope, key)) if store is not None
                              and kind is not None and scope is not None else None)
                is_self = target == key if scoped is None else (
                    scope is not None and kind is not None
                    and scoped == (scope, kind, key))
                is_self = is_self or (source_uid is not None and target_uid == source_uid)
                if is_self:
                    return {}, {"error": f"links['{rel}'] names the entity itself "
                                         f"('{key}'). A relation joins two entities, and "
                                         f"resolution never matches one to itself, so this "
                                         f"would be written and then read as resolving to "
                                         f"nothing. Nothing was written."}
            canonical = (_vault_path(store, store.entities[target_uid])
                         if store is not None and target_uid is not None else None)
            serialized.append(f"[[{canonical or target}{_link_suffix(t)}]]")
        fields[rel] = serialized
    return fields, None


def superseded_by(store, e):
    '"<kind> <key>" for each entity that names `e` under `supersedes`.'
    g = graph_for(store)
    ents = store.entities
    return sorted(_name(ents[s]) for s in g.superseded.get(e.get("uuid"), ()) if s in ents)


def unresolved_links(store, e):
    'The typed link targets of `e` that resolve to nothing, as written.'
    if not any(e.get(r) for r in TYPED):
        return []
    g = graph_for(store)
    missing = []
    for source, _rel, target, hit in g.typed:
        if source == e.get("uuid") and hit is None and target not in missing:
            missing.append(target)
    return missing


def dangling_link_warning(store, e):
    'A write warning naming the typed link targets of `e` that resolve to nothing.'
    missing = unresolved_links(store, e)
    if not missing:
        return None
    g = graph_for(store)
    collisions = {target for uid, target, _scope, found in g.collisions
                  if uid == e.get("uuid") and {store.entities[u]["type"] for u in found}
                  == {"script", "hook"}}
    ambiguous = [target for target in missing if target in collisions]
    absent = [target for target in missing if target not in collisions]
    warnings = []
    if ambiguous:
        warnings.append(f"ambiguous typed link target(s): {', '.join(ambiguous[:5])}; "
                        "use hook:<name> or script:<name> (e.g. "
                        f"hook:{ambiguous[0]})")
    if absent:
        more = f" (+{len(absent) - 5} more)" if len(absent) > 5 else ""
        warnings.append(f"{len(absent)} typed link target(s) resolve to nothing: "
                        f"{', '.join(absent[:5])}{more}")
    return "; ".join(warnings)










def split_candidates(store, min_backlinks=3):
    'Memories, docs, skills and commands over sections.TOC_MIN_BYTES with `min_backlinks`\n    or more backlinks: large, and read from several places, so worth splitting.'
    g = graph_for(store)
    ents = store.entities
    rows = []
    for uid, n in list(g.size.items()):
        e = ents.get(uid)
        backs = len(g.back.get(uid, ()))
        if (e is None or e.get("type") not in CARD_KINDS or n <= sections.TOC_MIN_BYTES
                or backs < min_backlinks
                or (e["type"] == "doc" and not _key(e).lower().endswith(".md"))):
            continue
        rows.append({**_row(e), "bytes": n, "backlinks": backs})
    return sorted(rows, key=lambda r: (-r["backlinks"], -r["bytes"], r["type"] or "", r["key"]))


def superseded_still_linked(store):
    'Superseded entities that an entity other than their superseders still links. A\n    source that is itself superseded is not counted.'
    g = graph_for(store)
    ents = store.entities
    rows = []
    for uid, by in g.superseded.items():
        e = ents.get(uid)
        if e is None:
            continue
        linked = sorted({_name(ents[s]) for s in g.back.get(uid, ())
                         if s in ents and s not in by and s not in g.superseded})
        if linked:
            rows.append({**_row(e), "superseded_by": sorted(_name(ents[s]) for s in by if s in ents),
                         "linked_from": linked})
    return sorted(rows, key=lambda r: (r["type"] or "", r["scope"] or "", r["key"]))


def supersede_cycles(store):
    'Entities that supersede each other around a loop. None of them counts as\n    superseded, so the loop is reported here for someone to break.'
    g = graph_for(store)
    ents = store.entities
    rows = [{"entities": sorted(_name(ents[u]) for u in group if u in ents)}
            for group in g.cycles]
    return sorted((r for r in rows if len(r["entities"]) > 1), key=lambda r: r["entities"])


def enforced_by_shared(store):
    'enforced by shared.'
    g = graph_for(store)
    ents = store.entities
    groups = {}
    for source, rel, target, hit in g.typed:
        if rel != "enforced_by" or source not in ents:
            continue
        label = _name(ents[hit]) if hit in ents else target
        groups.setdefault(label, set()).add(_name(ents[source]))
    return sorted(({"target": t, "sources": sorted(s)} for t, s in groups.items() if len(s) > 1),
                  key=lambda r: r["target"])
