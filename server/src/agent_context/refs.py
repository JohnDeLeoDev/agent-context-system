'[[wikilink]] / get_doc(...) pointer resolution shared by the write path and the audit.\n\nSplit out of fstools.py (build 25); `fstools` re-exports everything so callers and\ntests keep importing `fstools as T`.'
from __future__ import annotations

import difflib
import os
import posixpath
import re
from urllib.parse import unquote

_WIKILINK = re.compile(r"\[\[([^\]\[]+)\]\]")


_MD_DOC_LINK = re.compile(r"(?<!!)(?<!\\)\[[^\]\n]+\]\(\s*<?([^\s)>]+\.md(?:#[^\s)>]*)?)>?\s*(?:\"[^\"\n]*\"|'[^'\n]*')?\s*\)")






_LINKISH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")




_CODE_SPAN = re.compile(r"```.*?```|~~~.*?~~~|``.+?``|`[^`\n]+`", re.DOTALL)




_PROSE_TYPES = ("memory", "doc", "instruction", "skill", "command")



_POINTER = re.compile(
    r"\bget_(doc|memory|script|command|skill)\(\s*[\"']([^\"']+)[\"']"
    r"(?:\s*,\s*(?:project\s*=\s*)?[\"']([^\"']+)[\"'])?")

_POINTER_GENERIC = re.compile(
    r"\bget_entity\(\s*[\"'](doc|memory|script|command|skill)[\"']\s*,\s*[\"']([^\"']+)[\"']"
    r"(?:\s*,\s*(?:project\s*=\s*)?[\"']([^\"']+)[\"'])?")




_TITLE_LINK = re.compile(r"^[A-Za-z][A-Za-z0-9 ,'’.-]{2,70}$")


_PLACEHOLDER = re.compile(r"[…<>]|\.\.\.")


def _slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _strip_code(text):
    'Blank out fenced blocks and inline code spans so wikilink scanning only sees\n    prose. Replaces each span with spaces of equal length to keep offsets stable.'
    return _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), text)


def _known_targets(store):
    'Every name a reference may legitimately resolve to, keyed by pointer kind.\n\n    Scope-agnostic on purpose: skills, commands and scripts fall back to global when\n    absent at project scope, so a per-scope check would report false positives on\n    legitimate fallbacks.'
    mem_slugs, doc_paths, doc_titles, file_paths = set(), set(), set(), set()
    named = {"script": set(), "command": set(), "skill": set()}
    
    
    for scope_dir in ("global", "projects", "workspaces"):
        for directory, _dirs, files in os.walk(os.path.join(store.root, scope_dir)):
            for filename in files:
                if filename.endswith(".md"):
                    path = os.path.join(directory, filename)
                    file_paths.add(os.path.relpath(path, store.root).replace(os.sep, "/")[:-3])
    for e in store.entities.values():
        t = e.get("type")
        if t == "memory":
            mem_slugs.add(e.get("slug"))
        elif t == "doc":
            doc_paths.add(e.get("path"))
            if e.get("title"):
                doc_titles.add(e.get("title"))
        elif t in named:
            named[t].add(e.get("name"))
    k = {"memory": mem_slugs - {None}, "doc": doc_paths - {None}, **{t: v - {None} for t, v in named.items()}}
    k["doc_titles"] = doc_titles
    k["_wiki"] = k["memory"] | k["doc"] | doc_titles | file_paths
    return k


def _scan_wikilink_anchors(body):
    '(target, anchor or None) for each wikilink outside code spans and fences, `|alias`\n    dropped. The anchor is everything after the first `#`: `[[a#B#C|x]]` is ("a", "B#C").'
    links = []
    for m in _WIKILINK.findall(_strip_code(body)):
        t, _, anchor = m.strip().split("|", 1)[0].partition("#")
        if t.strip():
            links.append((t.strip(), anchor.strip() or None))
    return links


def _scan_wikilinks(body):
    'Wikilink targets outside code spans and fences, `#section` and `|alias` dropped.'
    return [t for t, _anchor in _scan_wikilink_anchors(body)]


def _scan_markdown_doc_links(body, source_path):
    '(scope-relative doc path, anchor) from local Markdown links in a doc.'
    if not source_path or not source_path.lower().endswith(".md"):
        return []
    found = []
    for raw in _MD_DOC_LINK.findall(_strip_code(body)):
        url, _, anchor = raw.partition("#")
        if url.startswith("/") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url):
            continue
        target = posixpath.normpath(posixpath.join(posixpath.dirname(source_path),
                                                 unquote(url)))
        if target in (".", "..") or target.startswith("../"):
            continue
        found.append((target, unquote(anchor) or None))
    return found


def _scan_pointers(body):
    ' scan pointers.'
    return ([(k, t, p or None) for k, t, p in _POINTER.findall(body)]
            + [(k, t, p or None) for k, t, p in _POINTER_GENERIC.findall(body)])


def _scan_refs(body):
    'Both pointer syntaxes found in one body: ([wikilink targets], [(kind, target)]).'
    return _scan_wikilinks(body), [(k, t) for k, t, _p in _scan_pointers(body)]


def _resolve_refs(known, body):
    'Unresolved references in `body`, as (label, target, suggestion|None).\n\n    One implementation, two callers: check_integrity reports these across the whole\n    store, and the write path warns about the ones a single edit introduces. A second\n    copy of this logic is exactly how the pointer syntax went unvalidated for months.'
    links, pointers = _scan_refs(body)
    out = []
    pool = sorted(x for x in known["_wiki"] if x)
    for t in links:
        if t in known["_wiki"]:
            continue
        if _LINKISH.match(t):
            near = difflib.get_close_matches(t, pool, n=1, cutoff=0.72)
            out.append((f"[[{t}]]", t, near[0] if near else None))
        elif _TITLE_LINK.match(t):
            
            
            near = difflib.get_close_matches(_slugify(t), pool, n=1, cutoff=0.72)
            if near:
                out.append((f"[[{t}]]", t, near[0]))
    for kind, t in pointers:
        if _PLACEHOLDER.search(t):
            continue  
        ok = known.get(kind) or set()
        if t in ok or (kind == "doc" and t in known["doc_titles"]):
            continue
        near = difflib.get_close_matches(t, sorted(x for x in ok if x), n=1, cutoff=0.72)
        out.append((f'get_{kind}("{t}")', t, near[0] if near else None))
    return out


def _with_ref_warning(store, result, body):
    'Attach a dead-reference warning to a write result, preserving any existing one.'
    w = _dead_ref_warning(store, body)
    if w:
        result["warning"] = f"{result['warning']} | {w}" if result.get("warning") else w
    return result


def _dead_ref_warning(store, body):
    "Warn at WRITE time about references this body introduces that resolve to nothing.\n\n    Deliberately a warning, not a block: a forward reference to something you are about\n    to create in the next call is legitimate. But it lands in the same tool result, so a\n    typo'd slug or a pointer to something another session just deleted is visible\n    immediately instead of rotting until the next audit."
    dead = _resolve_refs(_known_targets(store), str(body or ""))
    if not dead:
        return None
    parts = [f"{label}{' → did you mean ' + s + '?' if s else ''}" for label, _t, s in dead[:5]]
    more = f" (+{len(dead) - 5} more)" if len(dead) > 5 else ""
    return f"{len(dead)} reference(s) here resolve to nothing: " + "; ".join(parts) + more
