'Row/record shaping shared by every entity kind.\n\nSplit out of fstools.py (build 25); `fstools` re-exports everything so callers and\ntests keep importing `fstools as T`.'
from __future__ import annotations




def _proj(scope):
    kind, _, name = (scope or "global").partition(":")
    return name if kind == "project" else None


def _common(e):
    return {"id": e["uuid"], "scope": e.get("scope"), "project": _proj(e.get("scope")),
            "origin": e.get("origin"), "created_at": e.get("created_at"), "updated_at": e.get("updated_at")}


def _mem(e, body=True):
    d = _common(e)
    d.update(slug=e.get("slug"), memory_type=e.get("memory_type"), description=e.get("description"),
                             load_behavior=e.get("load_behavior") or "lazy")
    for key in ("keywords", "hosts", "sources", "verified_at"):
        if e.get(key):
            d[key] = e.get(key)
    if body:
        d["body"] = e.get("body")
    return d


def _doc(e, body=True):
    d = _common(e)
    d.update(path=e.get("path"), title=e.get("title"), load_behavior=e.get("load_behavior"))
    for key in ("description", "keywords", "hosts", "sources", "verified_at"):
        if e.get(key):
            d[key] = e.get(key)
    if body:
        d["body"] = e.get("body")
    return d


def _instr(e):
    d = _common(e)
    d.update(title=e.get("title"), body=e.get("body"),
                             load_behavior=e.get("load_behavior"), sort_order=e.get("sort_order"))
    return d


def _instr_bootstrap(e):
    'Instruction as the bootstrap needs it: the body an agent must obey, and the title\n    to address it by. The uuid/scope/origin/timestamps that _common carries are dead\n    weight here — nothing in a session addresses an instruction by uuid, and they cost\n    ~180 chars apiece every session. Full records stay available via get_instructions().'
    d = {"title": e.get("title"), "body": e.get("body")}
    p = _proj(e.get("scope"))
    if p:
        d["project"] = p
    return d


def _named(e, body_field="body", body=True):
    d = _common(e)
    d.update(name=e.get("name"), description=e.get("description"))
    if body:
        d[body_field] = e.get("script_body") if body_field == "script_body" else e.get("body")
    return d






_UPSERT_INTERNAL = {"uuid", "type", "scope", "_path", "body", "created_at", "updated_at", "files"}


def _carry_fields(e, natural_key, **override):
    d = {k: v for k, v in e.items() if k not in _UPSERT_INTERNAL and k != natural_key}
    d.update(override)
    return d


def _edit_source(store, e, body_key="body"):
    "The text an edit runs against: the file's bytes, so everything outside the span it\n    replaces survives the write.\n\n    The index copy has had its trailing newlines stripped, so editing one word through it\n    also rewrote the file's last byte. Falls back to the index copy when there is no file\n    to read. See `store.exact_body` (context graph T9)."
    exact = store.exact_body(e)
    return (e.get(body_key) or "") if exact is None else exact


def _unique_replace(body, old_string, new_string, what):
    'Edit-tool semantics: replace a unique occurrence, else return an error dict.'
    count = body.count(old_string)
    if count == 0:
        return None, {"error": f"old_string not found in {what}"}
    if count > 1:
        return None, {"error": f"old_string is not unique in {what} (appears {count} times); "
                      f"include more surrounding context to make it unique"}
    return body.replace(old_string, new_string, 1), None


def _addressing_for(scope):
    'How a caller reaches `scope`, as the argument they would pass.'
    if scope == "global":
        return "omit both project and workspace"
    if scope.startswith("ws:"):
        return f'pass workspace="{scope[3:]}"'
    if scope.startswith("project:"):
        return f'pass project="{scope[len("project:"):]}"'
    return f"scope {scope}"


def _not_found(store, typ, key):
    'The "<kind> \'<key>\' not found" error, naming the scopes that do hold the key.\n\n    A bare "not found" cannot be told apart from "does not exist", so an agent that\n    mis-addressed a lookup concludes the entity is gone and acts on that, for example\n    by writing a duplicate row at the wrong scope. Naming the scopes that hold the key\n    shows the caller the entity is reachable and how to address it.\n\n    The prefix is unchanged, so a caller (or hook) matching on\n    "<kind> \'<key>\' not found" keeps working and only gains a clause after it.'
    msg = f"{typ} '{key}' not found"
    try:
        elsewhere = store.scopes_holding(typ, key)
    except Exception:              
        return {"error": msg}
    if not elsewhere:
        return {"error": msg}
    where = "; ".join(f"{s} ({_addressing_for(s)})" for s in elsewhere)
    return {"error": f"{msg} at the scope you addressed — but it EXISTS at: {where}"}
