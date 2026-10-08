'Build 19: get_entity / list_entities / delete_entity / edit_body replace 21 per-kind tools.'
from narrow import aslist, notnone

from agent_context import fstools as T


def _seed(store):
    T.upsert_memory(store, "m1", "reference", "a memory", "body m1", project=None)
    T.upsert_doc(store, "guides/a.md", "doc a", title="A")
    T.upsert_doc(store, "other/b.md", "doc b", title="B")
    T.upsert_skill(store, "sk", "a skill", "skill body: step one")
    T.upsert_command(store, "cmd", "command body: run it", description="a command")
    T.upsert_script(store, "scr", "#!/bin/sh\necho old\n", description="a script")
    T.upsert_hook(store, "hk", "PreToolUse", "#!/bin/sh\nexit 0\n", matcher="Bash", description="a hook")
    T.upsert_instruction(store, "Rules", "rule body", project=None)


def test_get_entity_reaches_every_kind(store):
    _seed(store)
    assert T.get_entity(store, "memory", "m1")["body"] == "body m1"
    assert T.get_entity(store, "doc", "guides/a.md")["title"] == "A"
    assert T.get_entity(store, "skill", "sk")["description"] == "a skill"
    assert T.get_entity(store, "command", "cmd")["body"].startswith("command body")
    assert "echo old" in T.get_entity(store, "script", "scr")["script_body"]
    assert T.get_entity(store, "hook", "hk")["matcher"] == "Bash"
    assert "error" in T.get_entity(store, "widget", "x")


def test_list_entities_filters(store):
    _seed(store)
    assert [d["path"] for d in aslist(T.list_entities(store, "doc", path_prefix="guides/"))] == ["guides/a.md"]
    assert [m["slug"] for m in aslist(T.list_entities(store, "memory", memory_type="reference"))] == ["m1"]
    assert T.list_entities(store, "memory", memory_type="feedback") == []
    assert [h["name"] for h in aslist(T.list_entities(store, "hook"))] == ["hk"]
    assert [i["title"] for i in aslist(T.list_entities(store, "instruction"))] == ["Rules"]
    assert all("body" not in i for i in T.list_entities(store, "instruction"))
    assert "error" in T.list_entities(store, "widget")


def test_delete_entity_by_kind(store):
    _seed(store)
    T.delete_entity(store, "memory", "m1")
    assert T.get_memory(store, "m1") is None
    T.delete_entity(store, "doc", "other/b.md")
    assert T.get_doc(store, "other/b.md") is None
    T.delete_entity(store, "instruction", "Rules")           
    assert T.get_instructions(store) == []
    assert "error" in T.delete_entity(store, "widget", "x")


def test_delete_entity_instruction_kind_falls_back_to_uuid_for_other_kinds(store):
    '`delete_entity`\'s docstring says instructions accept their title OR uuid; the\n    uuid branch (`_delete_by_uuid`) dispatches on the entity\'s OWN stored type, not\n    on the "instruction" kind the caller passed — so it must delete an agent_definition\n    reached this way too, not silently no-op it.'
    agent = T.upsert_agent_definition(store, "worker-explore", description="d", body="b")
    uid = agent["id"]
    result = T.delete_entity(store, "instruction", uid)
    assert result.get("deleted") == "worker-explore", result
    assert T.get_agent_definition(store, "worker-explore") is None


def test_edit_body_patches_scripts_hooks_commands_and_skills(store):
    _seed(store)
    r = T.edit_body(store, "script", "scr", "echo old", "echo new")
    assert "echo new" in r["script_body"] and r["description"] == "a script"
    r = T.edit_body(store, "hook", "hk", "exit 0", "exit 2")
    assert "exit 2" in r["script_body"] and r["matcher"] == "Bash" and r["event_type"] == "PreToolUse"
    r = T.edit_body(store, "command", "cmd", "run it", "run it twice")
    assert "run it twice" in r["body"] and r["description"] == "a command"
    r = T.edit_body(store, "skill", "sk", "step one", "step 1")
    assert "step 1" in r["body"]
    store.reload()                                              
    assert "exit 2" in notnone(T.get_hook(store, "hk"))["script_body"]
    assert notnone(T.get_hook(store, "hk"))["description"] == "a hook"
    assert "error" in T.edit_body(store, "hook", "hk", "nope", "x")
    
    assert "error" in T.edit_body(store, "memory", "m1", "body", "ghp_" + "A" * 30)


def test_pointer_scan_reads_the_generic_form(store):
    _seed(store)
    assert T._dead_ref_warning(store, 'see get_entity("script", "scr")') is None
    w = T._dead_ref_warning(store, 'see get_entity("script", "missing-one")')
    assert w and "missing-one" in w


def test_script_language_change_replaces_the_file_instead_of_twinning(store):
    'test script language change replaces the file instead of twinning.'
    import os

    from agent_context import fstools as T
    T.upsert_script(store, "tool", "print(1)", language="python")
    d = os.path.join(store.root, "global", "scripts")
    assert sorted(os.listdir(d)) == ["tool.py", "tool.py.meta.toml"]
    T.upsert_script(store, "tool", "print(2)", language="py")          
    assert sorted(os.listdir(d)) == ["tool.py", "tool.py.meta.toml"]
    T.upsert_script(store, "tool", "echo 3", language="sh")            
    assert sorted(os.listdir(d)) == ["tool.sh", "tool.sh.meta.toml"]
    assert notnone(T.get_script(store, "tool"))["script_body"] == "echo 3"
    import pytest
    with pytest.raises(ValueError):
        T.upsert_script(store, "tool", "x", language="cobol")
    assert sorted(os.listdir(d)) == ["tool.sh", "tool.sh.meta.toml"]   


def test_typescript_is_a_storable_language(store):
    "It carries `import type`, which is not valid JavaScript, so a `.js` twin\n    would be a file the harness refuses to load. Before `ts` was accepted the\n    store had no kind that could hold one at all, and the fleet's only copy of\n    the ralph-loop driver lived on a single machine's disk."
    import os

    from agent_context import fstools as T
    body = 'import type { ExtensionAPI } from "x";\nexport default function (pi: ExtensionAPI) {}\n'
    T.upsert_script(store, "ralph-loop", body, language="ts")
    d = os.path.join(store.root, "global", "scripts")
    assert "ralph-loop.ts" in os.listdir(d), "ts did not map to a .ts file"
    assert notnone(T.get_script(store, "ralph-loop"))["script_body"] == body
    
    T.upsert_script(store, "ralph-loop", body, language="typescript")
    assert sorted(f for f in os.listdir(d) if f.startswith("ralph-loop")) == \
        ["ralph-loop.ts", "ralph-loop.ts.meta.toml"]


def test_rust_is_a_storable_language(store):
    'policy: the hook client is Rust source that every host compiles.'
    import os

    from agent_context import fstools as T
    body = "fn main() {}\n"
    T.upsert_script(store, "hook-client", body, language="rs")
    d = os.path.join(store.root, "global", "scripts")
    assert "hook-client.rs" in os.listdir(d), "rs did not map to a .rs file"
    assert notnone(T.get_script(store, "hook-client"))["script_body"] == body
    T.upsert_script(store, "hook-client", body, language="rust")
    assert sorted(f for f in os.listdir(d) if f.startswith("hook-client")) == \
        ["hook-client.rs", "hook-client.rs.meta.toml"]













def _canonical(store, typ, name, ext="sh", scope="global"):
    import os
    d = os.path.join(store.root, "global", typ + "s")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}.{ext}")


def test_upsert_script_adopts_a_file_already_at_its_canonical_path(store):
    body = "#!/usr/bin/env bash\necho 'forty kilobytes of real script'\n"
    open(_canonical(store, "script", "already-here"), "w").write(body)

    
    out = notnone(T.upsert_script(store, "already-here", None,
                                  description="adopted, not retyped"))
    assert "error" not in out
    assert out["script_body"] == body
    assert out["description"] == "adopted, not retyped"
    
    assert T.get_entity(store, "script", "already-here")["script_body"] == body


def test_adopt_respects_the_language_extension(store):
    body = "print('python, not sh')\n"
    open(_canonical(store, "script", "py-tool", ext="py"), "w").write(body)
    out = notnone(T.upsert_script(store, "py-tool", None, description="a python one",
                                  language="python"))
    assert out["script_body"] == body
    
    miss = notnone(T.upsert_script(store, "sh-tool", None, description="nothing there"))
    assert "error" in miss


def test_metadata_only_on_a_truly_absent_script_still_errors(store):
    'The husk guard must survive: a description with no content anywhere is an\n    error, not an empty entity.'
    out = notnone(T.upsert_script(store, "nowhere", None, description="d"))
    assert "error" in out
    assert "not found and no body given" in out["error"]
    assert "canonical path to adopt" in out["error"]


def test_adopt_is_scripts_and_hooks_only(store):
    'A doc or memory is addressed by a caller-supplied path or slug, so silently\n    adopting whatever sits there would be a different and worse tool.'
    assert store.adopt_body("doc", "architecture.md", "global") is None
    assert store.adopt_body("memory", "some-slug", "global") is None
