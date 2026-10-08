'Docs and patch-style body edits.\n\nSplit out of fstools.py (build 25); `fstools` re-exports everything so callers and\ntests keep importing `fstools as T`.'
from __future__ import annotations

import os

from . import graph, sections, usage
from .audit import _DIGEST_PATH_RE, audit_digest
from .refs import _with_ref_warning
from .shaping import _carry_fields, _doc, _edit_source, _not_found, _unique_replace




BODY_FILE_MAX_BYTES = 8 * 1024 * 1024

DOC_DESC_MAX = 140


def list_docs(store, project=None, path_prefix="", workspace=None):
    return [_doc(e, body=False) for e in store.list("doc", project, workspace=workspace)
            if str(e.get("path", "")).startswith(path_prefix)]


def get_doc(store, path, project=None, workspace=None, section=None):
    m = _DIGEST_PATH_RE.match(path or "")
    if m and not project and not workspace:
        d = audit_digest(store, m.group(1))
        body = d["body"]
        out = {"scope": "global", "project": None, "origin": "agent", "path": path,
               "title": d["title"], "load_behavior": "lazy", "generated": True,
               "body": body}
        return sections.shape(out, body, section, lambda: sections.parse(body), f"doc '{path}'")
    e = store.get("doc", path, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return None
    usage.record("doc", e.get("scope"), path, read=True)
    return graph.read(store, e, {**_doc(e), "links": graph.cards(store, e)}, section)


def search_docs(store, query, project=None, limit=20):
    rows = store.search(query, types=("doc",), project=project, limit=limit)
    usage.record_many("doc", [(r["scope"], r["name"]) for r in rows], hit=True)
    
    
    return [{"path": r["name"], "title": r.get("title", r["description"]),
             **({"description": r["description"]} if "title" in r else {}),
             "snippet": r["snippet"], "rank": r["rank"],
             **{k: r[k] for k in ("scope", "workspace", "project") if k in r}}
            for r in rows]


def read_body_file(body_path):
    'Read a doc body off local disk. Returns (body, error_dict); one is always None.'
    p = os.path.expanduser(body_path or "")
    if not os.path.isabs(p):
        return None, {"error": f"body_path must be absolute or ~-rooted, got '{body_path}'"}
    if not os.path.isfile(p):
        return None, {"error": f"body_path not found: {p}"}
    size = os.path.getsize(p)
    if size > BODY_FILE_MAX_BYTES:
        return None, {"error": f"body_path is {size:,} bytes, over the "
                               f"{BODY_FILE_MAX_BYTES:,} byte limit: {p}"}
    try:
        with open(p, encoding="utf-8") as f:
            return f.read(), None
    except UnicodeDecodeError:
        return None, {"error": f"body_path is not UTF-8 text: {p}"}
    except OSError as e:
        return None, {"error": f"body_path could not be read: {p} ({e})"}


def upsert_doc(store, path, body=None, project=None, title=None, origin: str | None = "user",
               workspace=None, body_path=None, links=None, description=None, keywords=None):
    '`description` says when to read the doc, as a memory\'s does; `keywords` are extra\n    search words. Both carry when omitted and clear on "" or [].'
    if description and len(description) > DOC_DESC_MAX:
        return {"error": f"description is {len(description)} chars; the limit is "
                         f"{DOC_DESC_MAX}. State when to read the doc. Nothing was written."}
    scope = store.scope_for_write(project, workspace)
    typed, err = graph.link_fields(links, key=path, kind="doc",
                                   scope=scope or store._scope_for(project), store=store)
    if err:
        return err
    if body_path is not None:
        
        if body is not None:
            return {"error": "pass body or body_path, not both"}
        body, err = read_body_file(body_path)
        if err:
            return err
    
    
    from .entities import carry_existing, carry_scalars
    body, title, err = carry_existing(store, "doc", path, project, body, title,
                                      meta_key="title", scope=scope)
    if err:
        return err
    
    
    fields = {"title": title or path, "load_behavior": "lazy",
              **carry_scalars(store, "doc", path, project, scope,
                              {"origin": origin, "description": description,
                               "keywords": keywords}), **typed}
    e = store.upsert("doc", path, fields, body=body, project=project, scope=scope)
    
    
    
    result = _doc(e, body=body_path is None)
    if body_path is not None:
        result["bytes"] = len((body or "").encode())
        result["source_file"] = os.path.expanduser(body_path)
    result = _with_ref_warning(store, result, body)
    w = graph.dangling_link_warning(store, e)
    if w:
        result["warning"] = f"{result['warning']} | {w}" if result.get("warning") else w
    return result


def delete_doc(store, path, project=None, workspace=None):
    return store.delete("doc", path, project, scope=store.scope_for_read(project, workspace))


def edit_doc_body(store, path, old_string, new_string, project=None, workspace=None):
    "Replace a unique occurrence of old_string in a doc's body."
    e = store.get("doc", path, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return _not_found(store, "doc", path)
    new_body, err = _unique_replace(_edit_source(store, e), old_string, new_string,
                                    f"doc '{path}'")
    if err:
        return err
    fields = _carry_fields(e, "path")
    result = _doc(store.upsert("doc", path, fields, body=new_body, scope=e.get("scope")))
    return _with_ref_warning(store, result, new_string)


def append_to_doc(store, path, text, project=None, workspace=None):
    "Append text to a doc's body (on a fresh line if the body doesn't end in one)."
    e = store.get("doc", path, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return _not_found(store, "doc", path)
    
    
    body = _edit_source(store, e)
    sep = "" if (not body or body.endswith("\n")) else "\n"
    new_body = body + sep + text
    fields = _carry_fields(e, "path")
    return _doc(store.upsert("doc", path, fields, body=new_body, scope=e.get("scope")))
