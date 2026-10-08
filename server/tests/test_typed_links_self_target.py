'Found in review of context graph T8: `sibling: ["<own slug>"]` was written to frontmatter\nand then reported as resolving to nothing, because resolution never matches an entity to\nitself. The write reported success, drew no card, and the warning said the target resolved\nto nothing while the entity plainly existed.'
import pytest

from agent_context import fstools as T
from agent_context import usage
from agent_context.graph import link_fields


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _front(store, kind, key):
    from pathlib import Path

    from agent_context.store import parse_frontmatter
    e = store.get(kind, key)
    assert e is not None, (kind, key)
    return parse_frontmatter(Path(e["_path"]).read_text(encoding="utf-8"))[0]




@pytest.mark.parametrize("target", [
    "me",
    "[[me]]",
    "  me  ",
    "[[me|an alias]]",
    "[[me#A heading]]",
    "me#A heading",
])
def test_link_fields_refuses_a_target_naming_the_key(target):
    fields, err = link_fields({"sibling": [target]}, key="me")

    assert fields == {}
    assert err is not None
    assert "itself" in err["error"]
    assert "me" in err["error"]


def test_link_fields_still_accepts_another_entity():
    fields, err = link_fields({"sibling": ["someone-else"]}, key="me")

    assert err is None
    assert fields == {"sibling": ["[[someone-else]]"]}


def test_link_fields_without_a_key_is_unchanged():
    "`key` is optional: a caller that does not know the source entity gets today's\n    behavior rather than a surprise refusal."
    fields, err = link_fields({"sibling": ["me"]})

    assert err is None
    assert fields == {"sibling": ["[[me]]"]}


def test_a_self_target_beside_a_good_one_refuses_the_whole_call():
    fields, err = link_fields({"sibling": ["other", "me"]}, key="me")

    assert fields == {}
    assert err is not None




def test_upsert_memory_refuses_a_self_link_and_writes_nothing(store):
    T.upsert_memory(store, "me", "reference", "d", "b\n")

    out = T.upsert_memory(store, "me", "reference", "changed", "new body\n",
                          links={"sibling": ["me"]})

    assert "error" in out, out
    assert "itself" in out["error"]
    front = _front(store, "memory", "me")
    assert "sibling" not in front
    assert front["description"] == "d", "the refused call must not have written the body"


def test_set_entity_links_refuses_a_self_link(store):
    T.upsert_memory(store, "me", "reference", "d", "b\n")

    out = T.set_entity_links(store, "memory", "me", {"sibling": ["me"]})

    assert "error" in out, out
    assert "itself" in out["error"]
    assert "sibling" not in _front(store, "memory", "me")


def test_upsert_doc_refuses_a_self_link(store):
    T.upsert_doc(store, "notes/one.md", "b\n")

    out = T.upsert_doc(store, "notes/one.md", "b\n", links={"part_of": ["notes/one.md"]})

    assert "error" in out, out
    assert "itself" in out["error"]


def test_upsert_skill_refuses_a_self_link(store):
    T.upsert_skill(store, "one", "a skill", "b\n")

    out = T.upsert_skill(store, "one", "a skill", "b\n", links={"sibling": ["one"]})

    assert "error" in out, out
    assert "itself" in out["error"]


def test_a_link_between_two_entities_still_works(store):
    T.upsert_memory(store, "me", "reference", "d", "b\n")
    T.upsert_memory(store, "you", "reference", "d", "b\n")

    out = T.set_entity_links(store, "memory", "me", {"sibling": ["you"]})

    assert "error" not in out, out
    assert _front(store, "memory", "me")["sibling"] == ["[[global/memory/you.md]]"]
