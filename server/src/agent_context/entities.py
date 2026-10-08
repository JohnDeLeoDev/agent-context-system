
'Skills, commands, hooks, scripts and agent definitions.\n\n`fstools` re-exports everything here, so callers and tests import `fstools as T`.'
from __future__ import annotations

import json
import os
import re

from . import graph
from .index import _DESC_MAX
from .shaping import _named


def list_skills(store, project=None, workspace=None):
    return [_named(e, body=False) for e in store.list("skill", project, workspace=workspace)]


def get_skill(store, name, project=None, workspace=None):
    e = store.get("skill", name, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return None
    d = _named(e)
    files = []
    sk_dir = os.path.dirname(e["_path"])
    for rel in e.get("files", []):
        fp = os.path.join(sk_dir, rel)
        if os.path.isfile(fp):
            
            with open(fp, encoding="utf-8") as fh:
                files.append({"relative_path": rel, "content": fh.read()})
    d["files"] = files
    
    
    
    d["allowed_tools"] = e.get("allowed_tools")
    d["disable_model_invocation"] = e.get("disable_model_invocation")
    return d


def carry_existing(store, kind, name, project, body, meta, body_key="body",
                   meta_key="description", scope=None, language=None):
    'Resolve an omitted body/metadata against what is already stored.\n\n    -> (body, meta, error_or_None)\n\n    `meta_key` is the entity\'s own name for its one-line metadata: "description"\n    for scripts/skills/commands/hooks, "title" for docs. Passing the wrong one\n    would silently blank the field it was meant to preserve.\n\n    Without this, changing a one-line description costs a full re-send of the\n    entity. Scripts, skills, commands, hooks and docs share it.\n\n    Omitting the body on an entity that does not exist is an error, not an empty\n    entity: that would create a husk with a description and no content.'
    prev = store.get(kind, name, project, scope=scope)
    if body is None:
        if not prev:
            
            
            
            
            
            adopted = store.adopt_body(kind, name,
                                       scope or store._scope_for(project),
                                       language or "sh")
            if adopted is not None:
                return adopted, meta, None
            return None, None, {
                "error": f"{kind} '{name}' not found and no body given"
                         + (" (no file at its canonical path to adopt either)"
                            if kind in ("hook", "script") else "")}
        
        
        exact = store.exact_body(prev)
        body = prev.get(body_key) or "" if exact is None else exact
    if meta is None and prev:
        meta = prev.get(meta_key)
    return body, meta, None


def carry_scalars(store, kind, name, project, scope, given, defaults=None):
    'Resolve omitted scalar fields against what is already stored -> dict.\n\n    carry_existing covers the body and the one-line description; every other field\n    comes through here, so a partial update does not reset it (for example an\n    `upsert_hook` that omits `language` keeps the stored one). upsert_skill\n    (allowed_tools), upsert_agent_definition (model, effort, tools, permission_mode)\n    and every upsert\'s `origin` all call this.\n\n    Semantics are three-way:\n      value given   -> use it\n      None          -> CARRY what is stored (or the create-time default if new)\n      "" or [] (empty) -> CLEAR it, which is the only way back to "no matcher"\n                       once one is set; without this, carrying would make the\n                       field one-way and you could never unset it. A bool field has\n                       no empty value of its own type to clear with, so the caller\n                       clears it by passing its off value (e.g. `False`), documented\n                       on the field\'s upsert.'
    prev = store.get(kind, name, project, scope=scope)
    defaults = defaults or {}
    out = {}
    for key, value in given.items():
        if value == "" or value == []:
            out[key] = None
        elif value is not None:
            out[key] = value
        elif prev is not None and prev.get(key) is not None:
            out[key] = prev.get(key)
        else:
            out[key] = defaults.get(key)
    return out





_YAML_NEEDS_QUOTES = re.compile(r":\s|:$|\s#|^[\s\-?:,\[\]{}#&*!|>'\"%@`]|\s$")
_YAML_RESERVED = frozenset(("", "~", "null", "true", "false", "yes", "no", "on", "off"))


def _yaml_scalar(text):
    '`text` as a YAML scalar that reads back as the same string: bare when that is\n    safe, else a JSON string (a valid YAML double-quoted scalar).'
    if (_YAML_NEEDS_QUOTES.search(text) or text.lower() in _YAML_RESERVED
            or re.fullmatch(r"[-+]?(\d[\d_]*)?\.?\d+([eE][-+]?\d+)?", text)):
        return json.dumps(text, ensure_ascii=False)
    return text


def sync_body_description(body, description):
    "Rewrite a body's own YAML frontmatter `description:` to match the metadata one.\n\n    A skill or command can carry two descriptions: the store metadata field, and a\n    `description:` inside the body's own frontmatter. project-materialize projects the\n    body verbatim when it has frontmatter, so the body copy is what the harness\n    matches skills against, while list_entities and every MCP read show the metadata\n    copy. Updating only the metadata would leave the projection serving the old text.\n\n    Handles the folded multi-line form too: `description: first line\n  continuation`,\n    so a single-line replace would leave orphaned continuation lines behind.\n\n    No frontmatter, or no `description:` in it, means the materializer synthesizes one\n    from metadata; nothing to do. Returns the body unchanged in that case."
    if description is None or not body or not body.startswith("---\n"):
        return body
    end = body.find("\n---", 4)
    if end == -1:
        return body
    head, rest = body[4:end], body[end:]
    out, i, lines, found = [], 0, head.split("\n"), False
    while i < len(lines):
        if lines[i].startswith("description:") and not found:
            found = True
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or lines[i].startswith("\t")):
                i += 1  
            out.append(f"description: {_yaml_scalar(description)}")
            continue
        out.append(lines[i])
        i += 1
    return "---\n" + "\n".join(out) + rest if found else body


def upsert_skill(store, name, description=None, body=None, project=None, allowed_tools=None,
                 origin=None, workspace=None, links=None, upstream=None,
                 disable_model_invocation=None):
    scope = store.scope_for_write(project, workspace)
    typed, err = graph.link_fields(links, key=name, kind="skill",
                                   scope=scope or store._scope_for(project), store=store)
    if err:
        return err
    body, description, err = carry_existing(store, "skill", name, project, body, description,
                                            scope=scope)
    if err:
        return err
    body = sync_body_description(body, description)
    
    
    
    fields = carry_scalars(store, "skill", name, project, scope,
                           {"allowed_tools": allowed_tools, "origin": origin,
                            "upstream": upstream,
                            "disable_model_invocation": disable_model_invocation},
                           {"origin": "user"})
    fields["description"] = description
    fields.update(typed)
    e = store.upsert("skill", name, fields, body=body, project=project, scope=scope)
    result = _named(e)
    w = graph.dangling_link_warning(store, e)
    if w:
        result["warning"] = w
    return result


def delete_skill(store, name, project=None):
    return store.delete("skill", name, project)












def _agent_named(e, body=True):
    '`_named` surfaces only name/description/body, which would silently drop the three\n    fields that make an agent definition worth having. Shape it explicitly instead.'
    d = _named(e, body=body)
    for k in ("model", "effort", "tools", "permission_mode"):
        d[k] = e.get(k)
    return d


def list_agent_definitions(store, project=None, workspace=None):
    return [_agent_named(e, body=False)
            for e in store.list("agent_definition", project, workspace=workspace)]


def get_agent_definition(store, name, project=None, workspace=None):
    e = store.get("agent_definition", name, project,
                  scope=store.scope_for_read(project, workspace))
    return _agent_named(e) if e else None


def upsert_agent_definition(store, name, description=None, body=None, project=None,
                            model=None, effort=None, tools=None, permission_mode=None,
                            origin=None, workspace=None):
    scope = store.scope_for_write(project, workspace)
    body, description, err = carry_existing(
        store, "agent_definition", name, project, body, description, scope=scope)
    if err:
        return err
    
    fields = carry_scalars(
        store, "agent_definition", name, project, scope,
        {"model": model, "effort": effort, "tools": tools,
         "permission_mode": permission_mode, "origin": origin},
        {"origin": "user"})
    fields["description"] = description
    return _agent_named(store.upsert("agent_definition", name, fields, body=body,
                                     project=project, scope=scope))


def delete_agent_definition(store, name, project=None):
    return store.delete("agent_definition", name, project)


def list_commands(store, project=None, workspace=None):
    return [_named(e, body=False) for e in store.list("command", project, workspace=workspace)]


def get_command(store, name, project=None, workspace=None):
    e = store.get("command", name, project, scope=store.scope_for_read(project, workspace))
    if not e:
        return None
    d = _named(e)
    
    d["allowed_tools"] = e.get("allowed_tools")
    d["disable_model_invocation"] = e.get("disable_model_invocation")
    d["argument_hint"] = e.get("argument_hint")
    return d


def upsert_command(store, name, body=None, project=None, description=None, origin=None,
                   workspace=None, allowed_tools=None, disable_model_invocation=None,
                   argument_hint=None):
    scope = store.scope_for_write(project, workspace)
    body, description, err = carry_existing(store, "command", name, project, body, description,
                                            scope=scope)
    if err:
        return err
    body = sync_body_description(body, description)
    
    
    
    fields = carry_scalars(store, "command", name, project, scope,
                           {"origin": origin, "allowed_tools": allowed_tools,
                            "disable_model_invocation": disable_model_invocation,
                            "argument_hint": argument_hint},
                           {"origin": "user"})
    fields["description"] = description
    store.upsert("command", name, fields, body=body, project=project, scope=scope)
    return get_command(store, name, project, workspace)


def delete_command(store, name, project=None):
    return store.delete("command", name, project)


def list_hooks(store, project=None, workspace=None):
    return [_named(e, "script_body", body=False)
            for e in store.list("hook", project, workspace=workspace)]


def get_hook(store, name, project=None, scope=None, workspace=None):
    e = store.get("hook", name, project, scope=scope or store.scope_for_read(project, workspace))
    if not e:
        return None
    d = _named(e, "script_body")
    d.update(event_type=e.get("event_type"), matcher=e.get("matcher"), language=e.get("language"),
             timeout_seconds=e.get("timeout_seconds"), is_async=e.get("is_async"))
    return d


_SCRIPTS_DIR = os.path.join(os.path.expanduser("~"), ".agent-context", "global", "scripts")
_SYNC_TABLE = os.path.join(_SCRIPTS_DIR, "home-settings-sync.py")
_HOOK_PROBE = os.path.join(_SCRIPTS_DIR, "hook-registration-probe.py")


def registration_warning(key, language, scope):
    "Say now that this hook will not run, instead of a session or two later.\n\n    Authoring a global hook is two writes: the file, and an entry in\n    home-settings-sync.py's MANAGED table that puts it in a lifecycle event. Only\n    the first happens here, so between the two writes the hook is on disk and\n    registered nowhere.\n\n    hook-registration-probe.py catches that state, but it reports at the next\n    SessionStart, to whoever opens the next session. The author is the one person who\n    can fix it in one move, so the warning is returned in the same tool result.\n\n    Advisory only: this refuses nothing. The two writes are made in some order, and a\n    tool that rejected the first one would make the second impossible to reach. It is\n    a substring test and not an import of MANAGED, because importing a script that\n    reads $HOME at module scope, from inside the server, costs more than it is worth."
    if scope != "global":
        return None                       
    from .store import EXT

    filename = f"{key}.{EXT.get(language or 'sh', 'sh')}"
    for path in (_SYNC_TABLE, _HOOK_PROBE):
        try:
            with open(path) as fh:
                if filename in fh.read():
                    return None           
        except OSError:
            return None                   
    return (
        f"{filename} is stored in ~/.agent-context/global/hooks, but nothing "
        "runs it yet: no lifecycle event in home-settings-sync.py's MANAGED table names "
        "it, and it is not in hook-registration-probe.py's EXEMPT map. Wire it into "
        "MANAGED now, or add it to EXEMPT with a reason."
    )


def _description_rejected(description):
    'Refusal for a passed description over _DESC_MAX, or None. An omitted\n    description carries the stored one and is never checked here.'
    if description is not None and len(description) > _DESC_MAX:
        return {"error": f"description is {len(description)} chars; the limit is {_DESC_MAX}. "
                         f"Keep it to one line saying what the entity is for and put the "
                         f"detail in the body. Nothing was written."}
    return None


def upsert_hook(store, name, event_type=None, script_body=None, project=None, matcher=None,
                language=None, timeout_seconds=None, origin=None, description=None,
                workspace=None):
    rejected = _description_rejected(description)
    if rejected:
        return rejected
    scope = store.scope_for_write(project, workspace)
    
    script_body, description, err = carry_existing(
        store, "hook", name, project, script_body, description, "script_body", scope=scope,
        language=language)
    if err:
        return err
    
    fields = carry_scalars(
        store, "hook", name, project, scope,
        {"event_type": event_type, "matcher": matcher, "language": language,
         "timeout_seconds": timeout_seconds, "origin": origin},
        {"language": "sh", "timeout_seconds": 30, "origin": "user"})
    
    
    
    if not fields.get("event_type"):
        return {"error": f"hook '{name}' is new, so event_type is required "
                         "(PreToolUse|PostToolUse|UserPromptSubmit|SessionStart|Stop|Notification|SubagentStop)"}
    if description is not None:
        fields["description"] = description
    store.upsert("hook", name, fields, body=script_body, project=project, scope=scope)
    out = get_hook(store, name, project, scope=scope)
    warning = registration_warning(name, language, scope)
    if out is not None and warning:
        out["registration_warning"] = warning
    return out


def delete_hook(store, name, project=None):
    return store.delete("hook", name, project)


def list_scripts(store, project=None, workspace=None):
    return [_named(e, "script_body", body=False)
            for e in store.list("script", project, workspace=workspace)]


def get_script(store, name, project=None, scope=None, workspace=None):
    e = store.get("script", name, project,
                  scope=scope or store.scope_for_read(project, workspace))
    if not e:
        return None
    d = _named(e, "script_body")
    d.update(language=e.get("language"))
    return d


def upsert_script(store, name, script_body=None, project=None, description=None, language=None,
                  origin=None, workspace=None):
    rejected = _description_rejected(description)
    if rejected:
        return rejected
    scope = store.scope_for_write(project, workspace)
    script_body, description, err = carry_existing(
        store, "script", name, project, script_body, description, "script_body", scope=scope,
        language=language)
    if err:
        return err
    fields = carry_scalars(store, "script", name, project, scope,
                           {"language": language, "origin": origin},
                           {"language": "sh", "origin": "user"})
    fields["description"] = description
    store.upsert("script", name, fields, body=script_body, project=project, scope=scope)
    return get_script(store, name, project, scope=scope)


def delete_script(store, name, project=None):
    return store.delete("script", name, project)
