
'Generic entity access (get_entity / list_entities / delete_entity / edit_body).\n\nSplit out of fstools.py (build 25); `fstools` re-exports everything so callers and\ntests keep importing `fstools as T`.'
from __future__ import annotations

import json
import os
import re

from . import graph, paging, sections
from .docs import edit_doc_body, get_doc, list_docs
from .entities import (
    get_agent_definition,
    get_command,
    get_hook,
    get_script,
    get_skill,
    list_agent_definitions,
    list_commands,
    list_hooks,
    list_scripts,
    list_skills,
)
from .memory import (
    _delete_by_uuid,
    _memory_write_rejected,
    edit_instruction_body,
    edit_memory_body,
    get_instructions,
    get_memory,
    list_memories,
)
from .projects import delete_project, list_projects
from .refs import _with_ref_warning
from .shaping import _carry_fields, _edit_source, _not_found, _unique_replace
from .store import NATURAL_KEY
from .write_guard import WriteDenied





_BODY_FIELD = {"memory": "body", "doc": "body", "instruction": "body", "skill": "body",
               "command": "body", "script": "script_body", "hook": "script_body",
               "agent_definition": "body"}



LINKABLE = ("memory", "doc", "instruction", "skill", "command", "script", "hook")


def _unknown_kind(kind, allowed):
    return {"error": f"unknown kind '{kind}' (expected one of: {', '.join(allowed)})"}


def get_entity(store, kind, key, project=None, workspace=None, section=None):
    "Read one entity by kind + natural key (memory slug, doc path, skill/command/hook/\n    script name). Falls back to global when absent at project scope. `section` cuts a\n    memory, doc, skill or command body to one heading's section (sections.shape)."
    if kind == "memory":
        return get_memory(store, key, project, workspace=workspace, section=section)
    if kind == "doc":
        return get_doc(store, key, project, workspace=workspace, section=section)
    fn = {"skill": get_skill, "command": get_command, "hook": get_hook, "script": get_script,
          "agent_definition": get_agent_definition}.get(kind)
    if not fn:
        return _unknown_kind(kind, ("memory", "doc", "skill", "command", "hook", "script",
                                    "agent_definition"))
    out = fn(store, key, project, workspace=workspace)
    if out and section is not None and kind not in sections.KINDS:
        return {"error": f"section applies to {', '.join(sections.KINDS)}; "
                         f"{kind} '{key}' has no sections"}
    
    if out and kind in ("skill", "command"):
        e = store.entities.get(out.get("id"))
        if e:
            out["links"] = graph.cards(store, e)
            out = graph.read(store, e, out, section)
    return out


def list_entities_page(store, kind, project=None, path_prefix="", memory_type=None,
                       name_prefix="", limit=50, offset=0, workspace=None):
    "The MCP form of `list_entities`: one page as `{items, total, shown, next}` (`limit=0`\n    lists everything, still under the byte cap). Anything that is not a list (an unknown\n    kind's error) comes back as it is."
    for name, value in (("limit", limit), ("offset", offset)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return {"error": f"{name} must be a non-negative integer, got {value!r}"}
    rows = list_entities(store, kind, project, path_prefix, memory_type, name_prefix, workspace)
    return paging.page(rows, limit, offset) if isinstance(rows, list) else rows


def list_entities(store, kind, project=None, path_prefix="", memory_type=None, name_prefix="",
                  workspace=None):
    'List one kind, bodies omitted. path_prefix narrows docs; memory_type and name_prefix\n    narrow memories. `workspace` (no project) lists global plus that workspace; for\n    projects it keeps the ones in that workspace.'
    if workspace:
        store.scope_for_read(project, workspace)   
    if kind == "memory":
        rows = list_memories(store, project, memory_type, workspace=workspace)
        return [m for m in rows if str(m.get("slug", "")).startswith(name_prefix)] if name_prefix else rows
    if kind == "doc":
        return list_docs(store, project, path_prefix, workspace=workspace)
    if kind == "instruction":
        return [{k: v for k, v in i.items() if k != "body"} for i in get_instructions(store, project, workspace=workspace)]
    if kind == "project":
        return list_projects(store, workspace)
    fn = {"skill": list_skills, "command": list_commands, "hook": list_hooks,
          "script": list_scripts, "agent_definition": list_agent_definitions}.get(kind)
    if not fn:
        return _unknown_kind(kind, ("memory", "doc", "instruction", "skill", "command", "hook",
                                    "script", "agent_definition", "project"))
    return fn(store, project, workspace=workspace)


def delete_entity(store, kind, key, project=None, workspace=None):
    'Soft-delete one entity. Instructions accept their title or uuid; projects their\n    display_name.'
    if kind == "project":                 
        return delete_project(store, key)
    scope = store.scope_for_read(project, workspace)
    if kind == "instruction":
        if store.get("instruction", key, project, scope=scope):
            return store.delete("instruction", key, project, scope=scope)
        return _delete_by_uuid(store, key)
    if kind in ("memory", "doc", "skill", "command", "hook", "script", "agent_definition"):
        return store.delete(kind, key, project, scope=scope)
    return _unknown_kind(kind, ("memory", "doc", "instruction", "skill", "command", "hook",
                                "script", "agent_definition", "project"))


def edit_body(store, kind, key, old_string, new_string, project=None, workspace=None):
    "Replace a unique occurrence of old_string in an entity's body without re-sending\n    it. Memory/doc/instruction keep their guarded paths; skill/command/script/hook get\n    the same Edit-tool semantics — the only way to patch a 300-line guard hook safely."
    if kind == "memory":
        return edit_memory_body(store, key, old_string, new_string, project, workspace=workspace)
    if kind == "doc":
        return edit_doc_body(store, key, old_string, new_string, project, workspace=workspace)
    if kind == "instruction":
        return edit_instruction_body(store, key, old_string, new_string, project,
                                     workspace=workspace)
    if kind not in ("skill", "command", "script", "hook", "agent_definition"):
        return _unknown_kind(kind, tuple(NATURAL_KEY))
    e = store.get(kind, key, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return _not_found(store, kind, key)
    field = _BODY_FIELD[kind]
    new_body, err = _unique_replace(_edit_source(store, e, field), old_string, new_string,
                                    f"{kind} '{key}'")
    if err:
        return err
    fields = {k: v for k, v in _carry_fields(e, "name").items() if k != "script_body"}
    store.upsert(kind, key, fields, body=new_body, scope=e.get("scope"))
    receipt = get_entity(store, kind, key, project, workspace=workspace)
    if isinstance(receipt, dict):
        receipt.pop("toc", None)  
    return _with_ref_warning(store, receipt, new_string)


def set_entity_links(store, kind, key, links, project=None, workspace=None):
    "Write an entity's typed relation keys without sending its body (context graph T8).\n\n    `links=` rides on `upsert_memory` / `upsert_doc` / `upsert_skill`, and those take the\n    whole body. An agent's write is checked before it arrives, and an older body may not\n    pass the plain-language rules as it stands, so adding one relation through an upsert\n    can force a rewrite of the prose.\n\n    This route sends no body. It reads the stored one, carries every frontmatter key the\n    entity already holds, and writes the relation keys through `graph.link_fields`, the\n    same validation the upserts use. A listed relation replaces that key, `[]` removes it,\n    a relation the call does not name is kept, an unknown relation is refused with nothing\n    written, and a target resolving to nothing is written with a warning.\n\n    Scripts and hooks carry relations in their metadata sidecars; their code bodies\n    remain unparsed. Commands retain the existing write contract."
    if kind not in LINKABLE:
        return _unknown_kind(kind, LINKABLE)
    e = store.get(kind, key, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return _not_found(store, kind, key)
    typed, err = graph.link_fields(links, key=key, kind=kind,
                                   scope=e.get("scope") or "global", store=store)
    if err:
        return err
    if not typed:
        return {"error": 'links must name at least one relation, e.g. {"sibling": '
                         '["other-slug"]}; pass {"sibling": []} to remove one'}
    
    
    fields = {**_carry_fields(e, NATURAL_KEY[kind]), **typed}
    
    stored = store.upsert(kind, key, fields, body=None, scope=e.get("scope"))
    out = {"id": stored.get("uuid"), "type": kind, NATURAL_KEY[kind]: key,
           "scope": stored.get("scope"), "links_set": typed,
           "links": graph.cards(store, stored)}
    warning = graph.dangling_link_warning(store, stored)
    if warning:
        out["warning"] = warning
    return out


def _load_body(store, kind, key, project, workspace=None):
    'Entity + its body text, or an error dict. One shape for every kind.'
    if kind not in _BODY_FIELD:
        return None, None, _unknown_kind(kind, tuple(_BODY_FIELD))
    e = store.get(kind, key, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return None, None, _not_found(store, kind, key)
    return e, _edit_source(store, e, _BODY_FIELD[kind]), None


def _store_body(store, kind, key, e, new_body):
    natural = NATURAL_KEY[kind]
    fields = _carry_fields(e, natural)
    if kind in ("script", "hook"):
        fields = {k: v for k, v in fields.items() if k != "script_body"}
    store.upsert(kind, key, fields, body=new_body, scope=e.get("scope"))






ENTITY_FIELDS = ("description", "keywords", "hosts", "sources", "verified_at")
_LIST_FIELDS = ("keywords", "hosts", "sources")
_FIELD_KINDS = ("memory", "doc")
_FIELD_DESC_MAX = 140
_FIELD_LIST_MAX = 24
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fleet_machine_ids(store):
    'The machine ids `hosts` may name: one per machines/<uuid>.toml.'
    ids = set()
    root = os.path.join(store.root, "machines")
    try:
        names = os.listdir(root)
    except OSError:
        return ids
    for name in names:
        if not name.endswith(".toml"):
            continue
        try:
            with open(os.path.join(root, name), encoding="utf-8") as f:
                m = re.search(r'^\s*machine_id\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
        except OSError:
            continue
        if m:
            ids.add(m.group(1))
    return ids


def _field_changes(store, e, kind, given):
    '(changes, error) for one entity\'s `fields`. None leaves a field alone; "" or []\n    clears it. A value equal to the stored one is not a change.'
    if kind not in _FIELD_KINDS:
        return None, f"fields apply to {' and '.join(_FIELD_KINDS)} only, got {kind}"
    if not isinstance(given, dict):
        return None, "fields must be an object"
    unknown = sorted(set(given) - set(ENTITY_FIELDS))
    if unknown:
        return None, f"unknown field(s): {', '.join(unknown)}; known: {', '.join(ENTITY_FIELDS)}"
    changes = {}
    for name, value in given.items():
        if value is None:
            continue
        if name in _LIST_FIELDS:
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip()
                                                      for v in value):
                return None, f"{name} must be a list of non-empty strings"
            if len(value) > _FIELD_LIST_MAX:
                return None, f"{name} holds {len(value)} items; the limit is {_FIELD_LIST_MAX}"
            value = [v.strip() for v in value]
            if name == "hosts":
                known = fleet_machine_ids(store)
                bad = sorted(set(value) - known) if known else []
                if bad:
                    return None, (f"hosts names unknown machine(s): {', '.join(bad)}; "
                                  f"known: {', '.join(sorted(known))}")
        else:
            if not isinstance(value, str):
                return None, f"{name} must be a string"
            value = value.strip()
            if name == "description":
                if len(value) > _FIELD_DESC_MAX:
                    return None, (f"description is {len(value)} chars; the limit is "
                                  f"{_FIELD_DESC_MAX}")
                if not value and kind == "memory":
                    return None, "a memory's description cannot be cleared"
            if name == "verified_at" and value and not _DATE.match(value):
                return None, "verified_at must be a date, YYYY-MM-DD"
        new = value or None
        if new != (e.get(name) or None):
            changes[name] = new
    return changes, None


def bulk_edit(store, edits, project=None, dry_run=False, require_unique=False, workspace=None,
              file_path=None):
    "Apply many replacements across many entities and return counts, never bodies.\n\n    edit_body echoes the whole entity on every call, so the context cost is\n    entity_size x edit_count. This returns per-entity counts only, so the cost is\n    proportional to the number of entities touched.\n\n    edits: [{kind, key, replacements: [[old, new], ...], project?, workspace?}]\n    Each entity is read once, all its replacements applied in order, written once --\n    one version bump per entity, not one per word. Per-entry `project`/`workspace`\n    override the call-level ones and are mutually exclusive, exactly as everywhere else.\n\n    require_unique=True gives edit_body semantics (a replacement matching 0 or >1 times\n    is an error and the entity is left untouched). Default False replaces every\n    occurrence and reports the count, which is what a spelling sweep wants; verify the\n    result with a re-scan rather than trusting the number.\n\n    dry_run reports what would change and writes nothing.\n\n    An entry may carry `fields` (ENTITY_FIELDS, memory and doc only) and `links` (typed\n    links, as set_entity_links takes them) with or without replacements: they are\n    written in the same version bump. `file_path` reads the edits from a JSON file on\n    the daemon's disk, for a backfill too large to send as an argument."
    if file_path is not None:
        if edits:
            return {"error": "pass edits or file_path, not both"}
        from .docs import read_body_file
        text, err = read_body_file(file_path)
        if err:
            return err
        try:
            edits = json.loads(text)
        except ValueError as ex:
            return {"error": f"file_path is not JSON: {ex}"}
        if not isinstance(edits, list):
            return {"error": "file_path must hold a JSON list of edits"}
    results, total, changed, fields_changed = [], 0, 0, 0
    for spec in edits or []:
        if not isinstance(spec, dict):
            results.append({"kind": None, "key": None, "applied": 0,
                            "error": "each edit must be an object"})
            continue
        kind, key = spec.get("kind"), spec.get("key")
        scope_project = spec.get("project", project)
        scope_workspace = spec.get("workspace", workspace)
        reps = spec.get("replacements") or []
        try:
            e, body, err = _load_body(store, kind, key, scope_project, scope_workspace)
        except ValueError as ex:      
            results.append({"kind": kind, "key": key, "applied": 0, "error": str(ex)})
            continue
        if err:
            results.append({"kind": kind, "key": key, "applied": 0, "error": err["error"]})
            continue
        new_body, per, failed = body or "", [], None
        for rep in reps:
            old, new = (rep[0], rep[1]) if isinstance(rep, (list, tuple)) else (rep.get("old"), rep.get("new"))
            if not old:
                per.append(0)
                continue
            if require_unique:
                candidate, rerr = _unique_replace(new_body, old, new, f"{kind} '{key}'")
                if candidate is None:
                    failed = (rerr or {}).get("error")
                    break
                per.append(1)
                new_body = candidate
            else:
                n = new_body.count(old)
                per.append(n)
                if n:
                    new_body = new_body.replace(old, new)
        if failed:
            results.append({"kind": kind, "key": key, "applied": 0, "error": failed})
            continue
        n = sum(per)
        extra = {}
        if spec.get("fields") is not None:
            extra, ferr = _field_changes(store, e, kind, spec.get("fields"))
            if ferr:
                results.append({"kind": kind, "key": key, "applied": 0, "error": ferr})
                continue
        typed = {}
        if spec.get("links"):
            typed, lerr = graph.link_fields(spec.get("links"), key=key, kind=kind,
                                            scope=e.get("scope") or "global", store=store)
            if lerr:
                results.append({"kind": kind, "key": key, "applied": 0,
                                "error": lerr.get("error")})
                continue
            typed = {k: v for k, v in (typed or {}).items() if (e.get(k) or None) != (v or None)}
        
        if n and kind == "memory":
            rejected = _memory_write_rejected(None, new_body)
            if rejected:
                results.append({"kind": kind, "key": key, "applied": 0, "error": rejected})
                continue
        if (n or extra or typed) and not dry_run:
            try:
                if extra or typed:
                    
                    
                    fields = {**_carry_fields(e, NATURAL_KEY[kind]), **extra, **typed}
                    store.upsert(kind, key, fields, body=new_body if n else None,
                                 scope=e.get("scope"))
                else:
                    _store_body(store, kind, key, e, new_body)
            except WriteDenied as denied:      
                results.append({"kind": kind, "key": key, "applied": 0, "error": str(denied)})
                continue
        row = {"kind": kind, "key": key, "applied": n, "per_replacement": per}
        if extra or typed:
            row["fields_set"] = sorted([*extra, *typed])
            fields_changed += 1
        results.append(row)
        total += n
        changed += 1 if (n or extra or typed) else 0
    if file_path is not None:
        
        
        results = [r for r in results if r.get("error")]
    out = {"entities": len(edits or []), "entities_changed": changed, "replacements": total,
           "dry_run": bool(dry_run), "require_unique": bool(require_unique),
           "results": results}
    if fields_changed or file_path is not None:
        out["entities_with_fields_set"] = fields_changed
    return out
