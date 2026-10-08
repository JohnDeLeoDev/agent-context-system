'It was filed against instructions and it was never only instructions: docs,\nmemories, skills, commands, scripts, hooks and agent definitions all route\nthrough the same idiom, so each of them silently forked in exactly the same way.\n\nEvery test here asserts BOTH halves, because the visible half passes either way:\nthe edit reports success and returns the new body under both the broken and the\nfixed code. What distinguishes them is what is left on disk — one entity, still\nat `ws:`, or two.'
from agent_context import fstools as T


def _ws_project(store, name="P", ws="W"):
    'A registered project belonging to workspace `ws`.'
    T.upsert_project(store, f"gh:org/{name.lower()}", name, workspace=ws)
    return name


def _scopes_named(store, typ, key):
    return sorted(scope for (t, scope, k) in store.by_key if t == typ and k == key)


def test_editing_a_workspace_instruction_does_not_fork_a_global_copy(store):
    p = _ws_project(store)
    store.upsert("instruction", "WS Rules", {"load_behavior": "always", "sort_order": 0,
                                             "origin": "user"},
                 body="one\ntwo\n", scope="ws:W")

    out = T.edit_instruction_body(store, "WS Rules", "two", "three", project=p)

    assert "error" not in out, out
    assert out["scope"] == "ws:W", out            
    assert _scopes_named(store, "instruction", "WS Rules") == ["ws:W"]   
    assert "three" in store.get("instruction", "WS Rules", p)["body"]


def test_the_edit_is_visible_to_the_project_that_inherits_it(store):
    'The point of fixing the WRITE is that the READ then agrees with it.\n\n    Under the old behavior a project resolving upward still found the STALE\n    workspace body, because the corrected text had gone into a global entity the\n    workspace entity shadows. The edit looked applied and had no effect where it\n    was meant to land.'
    p = _ws_project(store)
    store.upsert("instruction", "WS Rules", {"load_behavior": "always", "sort_order": 0,
                                             "origin": "user"},
                 body="reminder list: yes\n", scope="ws:W")

    T.edit_instruction_body(store, "WS Rules", "yes", "retired", project=p)

    assert "retired" in store.get("instruction", "WS Rules", p)["body"]


def test_every_kind_writes_back_to_workspace_scope(store):
    'Filed against instructions; the idiom was shared by all of them.'
    p = _ws_project(store)
    store.upsert("memory", "ws-mem", {"memory_type": "reference", "description": "d",
                                      "origin": "user"}, body="alpha\n", scope="ws:W")
    store.upsert("doc", "ws-doc.md", {"origin": "user"}, body="alpha\n", scope="ws:W")
    store.upsert("skill", "ws-skill", {"description": "d", "origin": "user"},
                 body="alpha\n", scope="ws:W")

    T.edit_memory_body(store, "ws-mem", "alpha", "beta", project=p)
    T.edit_doc_body(store, "ws-doc.md", "alpha", "beta", project=p)
    T.edit_body(store, "skill", "ws-skill", "alpha", "beta", project=p)

    for typ, key in (("memory", "ws-mem"), ("doc", "ws-doc.md"), ("skill", "ws-skill")):
        assert _scopes_named(store, typ, key) == ["ws:W"], (typ, key)


def test_a_workspace_entity_can_be_deleted_by_uuid(store):
    "`delete_entity`'s uuid path re-derived its scope the same lossy way, so the\n    scope-equality rail could never pass and a workspace entity was undeletable —\n    the recovery route from the very corruption this observation describes."
    _ws_project(store)
    e = store.upsert("instruction", "WS Rules", {"load_behavior": "always", "sort_order": 0,
                                                 "origin": "user"},
                     body="x\n", scope="ws:W")

    assert T.delete_instruction(store, e["uuid"]) == {"deleted": "WS Rules"}
    assert _scopes_named(store, "instruction", "WS Rules") == []


def test_a_project_delete_still_cannot_remove_what_it_inherits(store):
    'The scope rail this change threads through is a SAFETY rail, not an\n    accident: `get()` resolves upward, so a delete aimed at a project must not\n    remove the workspace or global entity that project merely inherits.'
    p = _ws_project(store)
    store.upsert("instruction", "WS Rules", {"load_behavior": "always", "sort_order": 0,
                                             "origin": "user"}, body="x\n", scope="ws:W")

    assert store.delete("instruction", "WS Rules", project=p) == {"deleted": None}
    assert _scopes_named(store, "instruction", "WS Rules") == ["ws:W"]


def test_an_explicit_scope_never_edits_an_inherited_entity_by_mistake(store):
    "Exact-scope lookup on the write side. With the resolving `get()` here, an\n    upsert scoped to `ws:W` for a key that exists only globally would inherit the\n    global entity's created_at and treat the write as an update of it."
    store.upsert("instruction", "Shared", {"load_behavior": "always", "sort_order": 0,
                                           "origin": "user"}, body="global\n")
    store.upsert("instruction", "Shared", {"load_behavior": "always", "sort_order": 0,
                                           "origin": "user"}, body="ws\n", scope="ws:W")

    assert _scopes_named(store, "instruction", "Shared") == ["global", "ws:W"]
    assert store.entities[store.by_key[("instruction", "global", "Shared")]]["body"] == "global"


def test_project_scoped_edits_are_unchanged(store):
    'The ordinary path must not move: project stays project, global stays global.'
    p = _ws_project(store)
    T.upsert_instruction(store, "Proj Rules", "alpha\n", project=p)
    T.upsert_instruction(store, "Global Rules", "alpha\n")

    assert T.edit_instruction_body(store, "Proj Rules", "alpha", "beta",
                                   project=p)["scope"] == "project:P"
    assert T.edit_instruction_body(store, "Global Rules", "alpha", "beta")["scope"] == "global"









def test_upsert_instruction_can_target_a_workspace(store):
    _ws_project(store)
    out = T.upsert_instruction(store, "WS Rules", "body\n", workspace="W")
    assert out["scope"] == "ws:W", out
    assert _scopes_named(store, "instruction", "WS Rules") == ["ws:W"]


def test_a_workspace_entity_is_inherited_by_its_projects_and_no_others(store):
    'The whole point of the scope: reachable from inside the workspace, absent\n    outside it. A global entity would have been visible to every repo on the\n    machine, which is the outcome the forked copy actually produced.'
    inside = _ws_project(store, "Inside", ws="W")
    T.upsert_project(store, "gh:org/outside", "Outside")          
    T.upsert_instruction(store, "WS Rules", "body\n", workspace="W")

    assert store.get("instruction", "WS Rules", inside) is not None
    assert store.get("instruction", "WS Rules", "Outside") is None


def test_every_upsert_kind_accepts_a_workspace(store):
    _ws_project(store)
    T.upsert_memory(store, "m", "reference", "d", "b\n", workspace="W")
    T.upsert_doc(store, "d.md", "b\n", workspace="W")
    T.upsert_skill(store, "s", "d", "b\n", workspace="W")
    T.upsert_command(store, "c", "b\n", description="d", workspace="W")
    T.upsert_script(store, "sc", "b\n", description="d", language="sh", workspace="W")
    T.upsert_hook(store, "h", "SessionStart", "b\n", description="d", workspace="W")
    T.upsert_agent_definition(store, "a", description="d", body="b\n", workspace="W")

    for typ, key in (("memory", "m"), ("doc", "d.md"), ("skill", "s"), ("command", "c"),
                     ("script", "sc"), ("hook", "h"), ("agent_definition", "a")):
        assert _scopes_named(store, typ, key) == ["ws:W"], (typ, key)


def test_an_unknown_workspace_is_refused_rather_than_created(store):
    'A typo must not mkdir `workspaces/NSYTA/` and strand the entity somewhere\n    no session resolves — the same failure `_reject_phantom_project` exists for,\n    and the same one this observation is about: written, inert, and silent.'
    _ws_project(store)
    try:
        T.upsert_instruction(store, "Oops", "body\n", workspace="NSYTA")
    except ValueError as e:
        assert "unknown workspace" in str(e) and "W" in str(e), e
    else:
        raise AssertionError("a typo'd workspace was accepted")
    assert _scopes_named(store, "instruction", "Oops") == []
    
    import os
    assert not os.path.exists(os.path.join(store.root, "workspaces", "NSYTA"))


def test_project_and_workspace_together_are_refused(store):
    'Contradictory addressing: a workspace entity belongs to the whole\n    workspace, so also pinning it to one project cannot be honored. Guessing\n    which the caller meant is how the original fork happened.'
    p = _ws_project(store)
    try:
        T.upsert_instruction(store, "Both", "body\n", project=p, workspace="W")
    except ValueError as e:
        assert "not both" in str(e), e
    else:
        raise AssertionError("contradictory scoping was accepted")


def test_re_upserting_a_workspace_entity_updates_it_in_place(store):
    "Carry-forward reads the WRITE scope. Reading the resolving path instead\n    would hand the update a global entity's body and fork a second one."
    _ws_project(store)
    first = T.upsert_instruction(store, "WS Rules", "one\n", workspace="W")
    second = T.upsert_instruction(store, "WS Rules", "two\n", workspace="W")

    assert first["id"] == second["id"]
    assert second["created_at"] == first["created_at"]
    assert _scopes_named(store, "instruction", "WS Rules") == ["ws:W"]


def test_an_omitted_body_carries_the_workspace_entity_forward_not_a_global_twin(store):
    '`body=None` means "keep what is stored". With the resolving read it would\n    have kept the GLOBAL entity\'s body and written it into workspace scope.'
    _ws_project(store)
    T.upsert_skill(store, "dup", "global desc", "GLOBAL BODY\n")
    T.upsert_skill(store, "dup", "ws desc", "WS BODY\n", workspace="W")

    T.upsert_skill(store, "dup", "ws desc v2", None, workspace="W")

    ws = store.get("skill", "dup", scope="ws:W")
    assert "WS BODY" in ws["body"], ws["body"]
    assert store.get("skill", "dup", scope="global")["body"].strip() == "GLOBAL BODY"














def test_a_workspace_entity_is_readable_by_workspace_without_a_project(store):
    _ws_project(store)
    T.upsert_memory(store, "ws-mem", "project", "d", "WS BODY\n", workspace="W")

    assert T.get_memory(store, "ws-mem") is None          
    got = T.get_memory(store, "ws-mem", workspace="W")
    assert got and got["scope"] == "ws:W", got
    assert "WS BODY" in got["body"]


def test_reading_by_workspace_never_answers_with_an_inherited_global_entity(store):
    "The whole point of exact addressing: `project=` resolves upward and would\n    hand back the global twin, which is how #266's fork carried the wrong body."
    _ws_project(store)
    T.upsert_skill(store, "dup", "global desc", "GLOBAL BODY\n")
    T.upsert_skill(store, "dup", "ws desc", "WS BODY\n", workspace="W")

    assert "WS BODY" in T.get_entity(store, "skill", "dup", workspace="W")["body"]
    assert "GLOBAL BODY" in T.get_entity(store, "skill", "dup")["body"]


def test_editing_by_workspace_edits_the_workspace_entity_only(store):
    _ws_project(store)
    T.upsert_memory(store, "dup", "project", "d", "global one\n")
    T.upsert_memory(store, "dup", "project", "d", "ws one\n", workspace="W")

    out = T.edit_body(store, "memory", "dup", "one", "two", workspace="W")

    assert "error" not in out, out
    assert out["scope"] == "ws:W", out
    assert store.get("memory", "dup", scope="ws:W")["body"].strip() == "ws two"
    assert store.get("memory", "dup", scope="global")["body"].strip() == "global one"
    assert _scopes_named(store, "memory", "dup") == ["global", "ws:W"]


def test_deleting_by_workspace_removes_only_the_workspace_entity(store):
    'test deleting by workspace removes only the workspace entity.'
    _ws_project(store)
    T.upsert_memory(store, "dup", "project", "d", "global\n")
    T.upsert_memory(store, "dup", "project", "d", "ws\n", workspace="W")

    assert T.delete_entity(store, "memory", "dup", workspace="W")["deleted"] == "dup"
    assert _scopes_named(store, "memory", "dup") == ["global"]


def test_reading_with_project_and_workspace_together_is_refused(store):
    p = _ws_project(store)
    T.upsert_memory(store, "ws-mem", "project", "d", "b\n", workspace="W")
    try:
        T.get_memory(store, "ws-mem", project=p, workspace="W")
    except ValueError as e:
        assert "not both" in str(e), e
    else:
        raise AssertionError("contradictory addressing was accepted on a READ")


def test_reading_an_unknown_workspace_is_refused_rather_than_answered_empty(store):
    'A typo must not read as "no such entity" — that is the exact confusion this\n    whole change exists to end.'
    _ws_project(store)
    try:
        T.get_memory(store, "anything", workspace="NSYTA")
    except ValueError as e:
        assert "unknown workspace" in str(e), e
    else:
        raise AssertionError("a typo'd workspace read as empty instead of refusing")


def test_not_found_names_the_scope_that_actually_holds_the_key(store):
    'THE REGRESSION THAT MATTERS. A bare "not found" is indistinguishable from\n    "does not exist", and an agent that cannot tell them apart invents an\n    explanation and acts on it. The error must say where the thing is.'
    _ws_project(store)
    T.upsert_memory(store, "ws-only", "project", "d", "b\n", workspace="W")

    err = T.edit_body(store, "memory", "ws-only", "b", "c")["error"]

    assert "not found" in err                      
    assert "ws:W" in err, err
    assert 'workspace="W"' in err, err
    
    assert _scopes_named(store, "memory", "ws-only") == ["ws:W"]
    assert store.get("memory", "ws-only", scope="ws:W")["body"].strip() == "b"
    assert "error" not in T.edit_body(store, "memory", "ws-only", "b", "c", workspace="W")


def test_not_found_for_something_genuinely_absent_stays_plain(store):
    'The hint must not fire when there is nothing to point at, or every real\n    absence grows noise and the signal stops meaning anything.'
    _ws_project(store)
    err = T.edit_body(store, "memory", "no-such-thing", "a", "b")["error"]
    assert err == "memory 'no-such-thing' not found", err


def test_a_project_scoped_twin_is_named_too(store):
    'Workspace is the case that bit us, but the ambiguity is not workspace-specific:\n    a project-scoped entity is just as invisible to a global lookup.'
    T.upsert_project(store, "gh:org/q", "Q")
    T.upsert_memory(store, "proj-only", "project", "d", "b\n", project="Q")

    err = T.edit_body(store, "memory", "proj-only", "b", "c")["error"]

    assert "project:Q" in err, err
    assert 'project="Q"' in err, err
