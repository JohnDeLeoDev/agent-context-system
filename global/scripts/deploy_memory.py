#!/usr/bin/env python3
'find() returns {"slug", "description", "scope"} or None. It raises\nstore_mcp.StoreUnreachable when the store cannot answer within the budget: the caller\nblocks the push then, because an accidental production deploy cannot be undone (policy).\nA repo with no remote cannot push, so it is answered without a call.'
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_mcp  

NEEDLE = "push-to-deploy"
BUDGET = 20.0  
SEARCH_LIMIT = 50


def _norm(name):
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def _slug_names(slug, names):
    'True when a name appears in the slug on word boundaries, never inside a longer word.'
    padded = "_%s_" % _norm(slug)
    return any("_%s_" % n in padded for n in names)


def find(cwd, budget=BUDGET):
    started = time.monotonic()

    def ask(tool, args, meta=None):
        left = budget - (time.monotonic() - started)
        if left <= 0:
            raise store_mcp.StoreUnreachable("the store took too long")
        return store_mcp.call(tool, args, meta=meta, deadline=left)

    ev = store_mcp.repo_evidence(cwd)
    if not ev["remotes"]:
        return None
    top = ev["cwd"]
    names = {_norm(os.path.basename(top.rstrip("/")))}
    project, workspace, idents = None, None, [ev["marker_id"], *ev["remotes"]]
    try:
        rec = ask("resolve_project", {"cwd": top}, meta=store_mcp.evidence_meta(ev))
    except store_mcp.ToolError:
        rec = None  
    if isinstance(rec, dict):
        project, workspace = rec.get("display_name"), rec.get("workspace")
        names.add(_norm(project))
        idents.append(rec.get("canonical_remote"))
    names.discard("")
    idents = [i.lower() for i in idents if i]

    try:
        hits = ask("search_all", {"query": NEEDLE, "limit": SEARCH_LIMIT, "kind": "memory"})
    except store_mcp.ToolError as exc:
        raise store_mcp.StoreUnreachable("search_all refused (%s)" % exc)
    if not isinstance(hits, list):
        raise store_mcp.StoreUnreachable("search_all answered in an unknown shape")

    exact = {"project_%s_deploy" % n for n in names}
    hits = sorted((h for h in hits if isinstance(h, dict) and h.get("slug")),
                  key=lambda h: _norm(h["slug"]) not in exact)  
    for hit in hits:
        slug, hit_project = hit["slug"], hit.get("project")
        if hit_project:
            mine = hit_project == project if project else _slug_names(slug, names)
            shared = False
        else:
            mine = _slug_names(slug, names)
            shared = not workspace or hit.get("workspace") in (None, workspace)
        if not mine and not (shared and "deploy" in (slug + " " + (hit.get("description") or "")).lower()):
            continue  
        if mine and (_norm(slug) in exact or NEEDLE in (hit.get("description") or "").lower()):
            return _found(hit)
        body = _body(ask, hit)
        if NEEDLE in body and (mine or any(i in body for i in idents)):
            return _found(hit)
    return None


def _scope_args(hit):
    if hit.get("project"):
        return {"project": hit["project"]}
    if hit.get("workspace"):
        return {"workspace": hit["workspace"]}
    return {}


def _body(ask, hit):
    try:
        mem = ask("get_memory", {"slug": hit["slug"], **_scope_args(hit)})
    except store_mcp.ToolError:
        return ""
    return ((mem.get("body") or "") if isinstance(mem, dict) else "").lower()


def _found(hit):
    return {"slug": hit["slug"], "description": hit.get("description") or "",
            "scope": hit.get("project") or hit.get("workspace") or "global"}


if __name__ == "__main__":
    try:
        print(find(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()))
    except store_mcp.StoreUnreachable as exc:
        print("store unreachable: %s" % exc)
        sys.exit(1)
