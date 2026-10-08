
'Instructions and memories.\n\n`fstools` re-exports everything here, so callers and tests import `fstools as T`.'
from __future__ import annotations

import json
import re

from . import graph, usage
from .index import (
    _DESC_MAX,
    _always_instruction_bytes,
    _index_budget_warning,
    _instruction_budget_warning,
)
from .refs import _dead_ref_warning, _with_ref_warning
from .shaping import _carry_fields, _edit_source, _instr, _mem, _not_found, _unique_replace
from .store import NATURAL_KEY


def get_instructions(store, project=None, load_behavior=None, workspace=None):
    return [_instr(e) for e in store.get_instructions(project, load_behavior, workspace=workspace)]


def _with_instruction_budget_warning(store, result, scope, before):
    'Attach an over-budget warning to an instruction write, keeping any existing one.'
    w = _instruction_budget_warning(store, scope, before)
    if w:
        result["warning"] = f"{result['warning']} | {w}" if result.get("warning") else w
    return result


def upsert_instruction(store, title, body, project=None, load_behavior="always", sort_order=0,
                       origin: str | None = None, workspace=None):
    scope = store.scope_for_write(project, workspace)
    
    
    
    budget_scope = scope or store._scope_for(project)
    before = _always_instruction_bytes(store, budget_scope)
    from .entities import carry_scalars
    fields = {"load_behavior": load_behavior, "sort_order": sort_order,
             **carry_scalars(store, "instruction", title, project, scope, {"origin": origin},
                             {"origin": "user"})}
    e = store.upsert("instruction", title, fields, body=body, project=project, scope=scope)
    return _with_instruction_budget_warning(store, _instr(e), e.get("scope") or budget_scope, before)


def _delete_by_uuid(store, uid):
    e = store.entities.get(uid)
    if not e:
        return {"deleted": None}
    nk = NATURAL_KEY.get(e["type"])
    return store.delete(e["type"], e.get(nk), scope=e.get("scope"))


def delete_instruction(store, instruction_id):
    return _delete_by_uuid(store, instruction_id)


def list_memories(store, project=None, memory_type=None, workspace=None):
    out = [_mem(e, body=False) for e in store.list("memory", project, workspace=workspace)]
    if memory_type:
        out = [m for m in out if m["memory_type"] == memory_type]
    return out


def get_memory(store, slug, project=None, workspace=None, section=None):
    e = store.get("memory", slug, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return None
    usage.record("memory", e.get("scope"), slug, read=True)
    return graph.read(store, e, {**_mem(e), "links": graph.cards(store, e)}, section)


def search_memories(store, query, project=None, limit=20):
    rows = store.search(query, types=("memory",), project=project, limit=limit)
    usage.record_many("memory", [(r["scope"], r["name"]) for r in rows], hit=True)
    
    return [{"slug": r["name"], "description": r["description"], "snippet": r["snippet"], "rank": r["rank"],
             **{k: r[k] for k in ("scope", "workspace", "project") if k in r}}
            for r in rows]


def _cross_scope_memory_warning(store, slug, project, scope=None):
    "If `slug` already exists at a scope other than the one being written, return\n    a non-fatal warning string (else None). Every session's memory_index carries\n    both, so a cross-scope duplicate is silently confusing without this flag."
    target = scope or store._scope_for(project)
    others = sorted({scope for (typ, scope, key) in store.by_key
                     if typ == "memory" and key == slug and scope != target})
    if not others:
        return None
    return (f"slug '{slug}' already exists at scope {', '.join(others)}; you may be creating a "
            f"cross-scope duplicate (writing to {target}). Consolidate to one scope if unintended.")




_CREDENTIAL_LITERAL = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})")


def _memory_write_rejected(description, body):
    "Server-side twin of the memory-husk-guard hook's blocking rules, enforced where\n    every harness writes (the hook only runs in Claude Code).\n\n    - A description over _DESC_MAX loads into every session for its scope.\n    - A credential literal in a body is published: the store auto-commits and pushes to\n      four remotes, and history keeps it after any redaction.\n    Returns the refusal message, or None when the write may proceed."
    if description is not None and len(description) > _DESC_MAX:
        return (f"description is {len(description)} chars; the limit is {_DESC_MAX}. It loads "
                f"into every session for this scope — keep it to one relevance hook (what the "
                f"memory is about, at most one status clause) and put the narrative in the body. "
                f"Nothing was written.")
    if body and _CREDENTIAL_LITERAL.search(body):
        return ("body contains a provider-issued credential literal (GitHub / OpenAI-style / "
                "AWS / Slack / Google prefix). The store pushes to GitHub + s1/s2/ls on every "
                "write, so this would be published and kept in history. Reference the 1Password "
                "item instead of the value. Nothing was written.")
    return None


_LOAD_BEHAVIORS = ("always", "lazy")


def _load_behavior_rejected(load_behavior, carried):
    'Refusal for a memory write that would leave `load_behavior` unset or invalid.\n\n    Two defaults once disagreed (the index read a missing field as always, path-derived\n    notes as lazy), so the field is now explicit on every memory. Returns the refusal\n    message, or None when the write may proceed.'
    if load_behavior is not None and load_behavior not in _LOAD_BEHAVIORS:
        return (f"load_behavior must be 'always' or 'lazy', not {load_behavior!r}. "
                f"Nothing was written.")
    if load_behavior is None and carried not in _LOAD_BEHAVIORS:
        return ("load_behavior is required: this memory has none to carry. Pass 'lazy' "
                "(slug in the bootstrap roster, body read on demand) or 'always' (row "
                "loaded in every session for this scope). Nothing was written.")
    return None


def upsert_memory(store, slug, memory_type, description, body, project=None, metadata=None,
                  origin=None, load_behavior=None, workspace=None, links=None,
                  require_load_behavior=False, keywords=None):
    '`require_load_behavior=True` (the MCP tool path) refuses a write that would leave\n    the memory with no load_behavior field; internal callers may omit it.'
    scope = store.scope_for_write(project, workspace)
    rejected = _memory_write_rejected(description, body)
    if rejected:
        return {"error": rejected}
    typed, err = graph.link_fields(links, key=slug, kind="memory",
                                   scope=scope or store._scope_for(project), store=store)
    if err:
        return err
    fields = {"memory_type": memory_type, "description": description, **typed}
    if metadata is not None:
        fields["metadata"] = json.dumps(metadata) if not isinstance(metadata, str) else metadata
    
    
    
    
    same_scope = store.get("memory", slug, project, scope=scope)
    if not (same_scope and same_scope.get("scope") == (scope or store._scope_for(project))):
        same_scope = None
    if load_behavior is not None or require_load_behavior:
        rejected = _load_behavior_rejected(load_behavior, (same_scope or {}).get("load_behavior"))
        if rejected:
            return {"error": rejected}
    if load_behavior is not None:  
        fields["load_behavior"] = load_behavior
    elif same_scope and same_scope.get("load_behavior"):
        fields["load_behavior"] = same_scope["load_behavior"]
    if origin is not None:
        fields["origin"] = origin
    else:
        fields["origin"] = (same_scope or {}).get("origin") or "agent"
    
    if keywords is None:
        keywords = (same_scope or {}).get("keywords")
    if keywords is not None:
        fields["keywords"] = keywords or None
    warning = _cross_scope_memory_warning(store, slug, project, scope)
    e = store.upsert("memory", slug, fields, body=body, project=project, scope=scope)
    result = _mem(e)
    
    warnings = [w for w in (warning, _dead_ref_warning(store, body),
                            graph.dangling_link_warning(store, e),
                            _index_budget_warning(store, project)) if w]
    if warnings:
        result["warning"] = " | ".join(warnings)
    return result


def set_memory_load_behavior(store, slug, load_behavior, project=None, workspace=None):
    'Tier a memory in/out of the always-loaded session bootstrap without touching its\n    body/description. load_behavior="lazy" removes it from every-session context (still\n    reachable via search_all/get_memory); "always" restores it. Pure index\n    hygiene for archival/narrow memories that don\'t earn a permanent context slot.'
    rejected = _load_behavior_rejected(load_behavior, None)
    if rejected:
        return {"error": rejected}
    e = store.get("memory", slug, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return _not_found(store, "memory", slug)
    fields = _carry_fields(e, "slug", load_behavior=load_behavior)
    
    
    return _mem(store.upsert("memory", slug, fields, body=None, scope=e.get("scope")))


def delete_memory(store, slug, project=None, workspace=None):
    return store.delete("memory", slug, project, scope=store.scope_for_read(project, workspace))


def set_memory_description(store, slug, description, project=None, workspace=None):
    "Update only a memory's description; body/type/metadata untouched."
    rejected = _memory_write_rejected(description, None)
    if rejected:
        return {"error": rejected}
    e = store.get("memory", slug, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return _not_found(store, "memory", slug)
    fields = _carry_fields(e, "slug", description=description)
    return _mem(store.upsert("memory", slug, fields, body=None, scope=e.get("scope")))


def set_memory_keywords(store, slug, keywords, project=None, workspace=None):
    "Update only a memory's search keywords; [] clears them."
    e = store.get("memory", slug, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return _not_found(store, "memory", slug)
    fields = _carry_fields(e, "slug", keywords=keywords or None)
    return _mem(store.upsert("memory", slug, fields, body=None, scope=e.get("scope")))


def edit_memory_body(store, slug, old_string, new_string, project=None, workspace=None):
    "Replace a unique occurrence of old_string in a memory's body."
    rejected = _memory_write_rejected(None, new_string)
    if rejected:
        return {"error": rejected}
    e = store.get("memory", slug, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return _not_found(store, "memory", slug)
    new_body, err = _unique_replace(_edit_source(store, e), old_string, new_string,
                                    f"memory '{slug}'")
    if err:
        return err
    fields = _carry_fields(e, "slug")
    result = _mem(store.upsert("memory", slug, fields, body=new_body, scope=e.get("scope")))
    
    return _with_ref_warning(store, result, new_string)


def edit_instruction_body(store, title, old_string, new_string, project=None, workspace=None):
    "Replace a unique occurrence of old_string in an instruction's body. The safe\n    patch path for the always-loaded instructions — avoids re-emitting the whole body\n    of the highest-blast-radius entity (a transcription slip there breaks every\n    session). Errors if not found or old_string isn't unique."
    e = store.get("instruction", title, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return _not_found(store, "instruction", title)
    new_body, err = _unique_replace(_edit_source(store, e), old_string, new_string,
                                    f"instruction '{title}'")
    if err:
        return err
    fields = _carry_fields(e, "title")
    scope = e.get("scope")
    before = _always_instruction_bytes(store, scope)
    result = _instr(store.upsert("instruction", title, fields, body=new_body, scope=scope))
    return _with_instruction_budget_warning(store, result, scope, before)
