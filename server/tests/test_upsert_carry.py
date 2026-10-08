'Before this, `store.upsert` rebuilt `meta` from `fields` alone, so a property added in\nObsidian, a typed link, or an omitted optional field was dropped by the next full upsert.\nThe carry lives in `store.upsert`, one site for every kind (handoff decision).\n\nHand edits below are followed by `store.reload()`, so the tests do not depend on which\nfile of a hook or script `_fresh` watches.'
from pathlib import Path

import pytest

from agent_context import fstools as T
from agent_context.store import parse_frontmatter, parse_toml


def _front(store, kind, key, project=None):
    e = store.get(kind, key, project)
    assert e is not None, (kind, key)
    return parse_frontmatter(Path(e["_path"]).read_text(encoding="utf-8"))[0]


def _hand_add_yaml(store, kind, key, line):
    'Add a property the way Obsidian writes one, then reload.'
    p = Path(store.get(kind, key)["_path"])
    p.write_text(p.read_text(encoding="utf-8").replace("\n---\n", f"\n{line}\n---\n", 1),
                 encoding="utf-8")
    store.reload()


def _mem(store, slug, body="b\n", desc="d", **kw):
    return T.upsert_memory(store, slug, "reference", desc, body, **kw)


def test_a_full_upsert_memory_keeps_a_property_added_in_obsidian(store):
    _mem(store, "m")
    _hand_add_yaml(store, "memory", "m", "status: draft")
    _mem(store, "m", body="rewritten\n", desc="new description")
    fm = _front(store, "memory", "m")
    assert fm["status"] == "draft"
    assert fm["description"] == "new description"
    out = T.get_memory(store, "m")
    assert out is not None and out["body"] == "rewritten"


def test_a_full_upsert_memory_keeps_typed_links_and_metadata(store):
    _mem(store, "t")
    _mem(store, "m", links={"part_of": ["t"]}, metadata={"k": 1})
    _mem(store, "m", body="rewritten\n")
    fm = _front(store, "memory", "m")
    assert fm["part_of"] == ["[[global/memory/t.md]]"]
    assert fm["metadata"] == '{"k": 1}'


_WRITES = {
    "doc": ("d.md", lambda s, b: T.upsert_doc(s, "d.md", b, title="D")),
    "skill": ("sk", lambda s, b: T.upsert_skill(s, "sk", "a skill", b)),
    "command": ("cmd", lambda s, b: T.upsert_command(s, "cmd", b, description="a command")),
    "instruction": ("Rules", lambda s, b: T.upsert_instruction(s, "Rules", b)),
    "agent_definition": ("worker", lambda s, b: T.upsert_agent_definition(s, "worker",
                                                                          "a worker", b)),
}


@pytest.mark.parametrize("kind", sorted(_WRITES))
def test_every_markdown_kind_keeps_a_hand_added_property(store, kind):
    key, write = _WRITES[kind]
    write(store, "first body\n")
    _hand_add_yaml(store, kind, key, "status: draft")
    write(store, "second body\n")
    assert _front(store, kind, key)["status"] == "draft"


@pytest.mark.parametrize("kind", ["hook", "script"])
def test_hooks_and_scripts_keep_a_hand_added_sidecar_key(store, kind):
    if kind == "hook":
        T.upsert_hook(store, "x", "PreToolUse", "exit 0\n", description="d")
    else:
        T.upsert_script(store, "x", "exit 0\n", description="d")
    sidecar = Path(store.get(kind, "x")["_path"] + ".meta.toml")
    sidecar.write_text(sidecar.read_text(encoding="utf-8") + 'status = "draft"\n',
                       encoding="utf-8")
    store.reload()
    if kind == "hook":
        T.upsert_hook(store, "x", script_body="exit 1\n", description="d2")
    else:
        T.upsert_script(store, "x", "exit 1\n", description="d2")
    meta = parse_toml(sidecar.read_text(encoding="utf-8"))
    assert meta["status"] == "draft"
    assert meta["description"] == "d2"


def test_an_empty_value_removes_a_key(store):
    store.upsert("memory", "m", {"memory_type": "reference", "description": "d",
                                 "status": "draft", "tags": ["a"], "owner": "x"}, body="b")
    store.upsert("memory", "m", {"memory_type": "reference", "description": "d",
                                 "status": None, "tags": [], "owner": ""}, body="b")
    fm = _front(store, "memory", "m")
    for k in ("status", "tags", "owner"):
        assert k not in fm, fm
        assert store.get("memory", "m").get(k) is None


def test_a_passed_value_replaces_the_stored_one(store):
    store.upsert("memory", "m", {"memory_type": "reference", "description": "d",
                                 "status": "draft"}, body="b")
    store.upsert("memory", "m", {"memory_type": "reference", "description": "d",
                                 "status": "done"}, body="b")
    assert _front(store, "memory", "m")["status"] == "done"


def test_the_carry_does_not_cross_scopes(store):
    T.upsert_project(store, "github.com:org/p", "P")
    _mem(store, "m")
    _hand_add_yaml(store, "memory", "m", "status: draft")
    _mem(store, "m", project="P")
    assert "status" not in _front(store, "memory", "m", project="P")
    assert _front(store, "memory", "m")["status"] == "draft"


def test_upsert_doc_keeps_origin_when_the_call_omits_it(store):
    'The MCP tool passes origin=None when the caller gives none. That means "not given",\n    so the stored origin is carried, as upsert_skill and upsert_command already do.'
    T.upsert_doc(store, "d.md", "b\n", title="D", origin="user")
    T.upsert_doc(store, "d.md", "b2\n", origin=None)
    assert _front(store, "doc", "d.md")["origin"] == "user"
