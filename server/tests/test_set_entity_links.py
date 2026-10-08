'Context graph T8: `set_entity_links`, a body-free route for typed relations.\n\nWhy the route exists: `links=` rides on `upsert_memory` / `upsert_doc` / `upsert_skill`,\nand those take the whole body. Nearly every entity written before 2026-09 carries the\nbanned dash in its prose, and `plain-language-check` refuses any body an agent sends that\nholds one. So adding a relation to an old entity meant rewriting its prose first, which is\nwhat commit b38d718a did to buy exactly one `sibling` key: 74 lines added, 71 removed, none\nof it about the link. This route sends no body, so a legacy body is never retyped and never\nrewritten.\n\nThe bodies here carry the banned character as an escape (\\u2014) on purpose: it is the\nbyte a legacy body holds, and proving it survives is the point of the route.'
import pytest

from agent_context import fstools as T
from agent_context import usage

LEGACY = "Prose from 2026-07 — with the banned dash — twice.\n\nAnd a second line.\n"


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    'Empty, isolated read counters: card order depends on them.'
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _mem(store, slug, body=LEGACY, desc=None, **kw):
    out = T.upsert_memory(store, slug, "reference", desc or f"about {slug}", body, **kw)
    assert "error" not in out, out
    return out


def _file_bytes(store, kind, key, project=None):
    e = store.get(kind, key, project)
    assert e is not None, (kind, key)
    with open(e["_path"], "rb") as fh:
        return fh.read()


def _body_bytes(raw):
    'Everything after the frontmatter block, as bytes.'
    assert raw.startswith(b"---\n"), raw[:40]
    return raw.split(b"\n---\n", 1)[1]


def _front(store, kind, key, project=None):
    from pathlib import Path

    from agent_context.store import parse_frontmatter
    e = store.get(kind, key, project)
    assert e is not None, (kind, key)
    return parse_frontmatter(Path(e["_path"]).read_text(encoding="utf-8"))[0]


def _read(result):
    'A read that found nothing answers None; every call here expects a hit.'
    assert result is not None, "read returned nothing"
    return result


def _scopes_named(store, typ, key):
    return sorted(scope for (t, scope, k) in store.by_key if t == typ and k == key)




def test_writes_the_relation_and_leaves_the_body_byte_for_byte(store):
    _mem(store, "source")
    _mem(store, "target")
    before = _body_bytes(_file_bytes(store, "memory", "source"))

    out = T.set_entity_links(store, "memory", "source", {"sibling": ["target"]})

    assert "error" not in out, out
    assert _front(store, "memory", "source")["sibling"] == ["[[global/memory/target.md]]"]
    assert _body_bytes(_file_bytes(store, "memory", "source")) == before
    assert b"\xe2\x80\x94" in before, "fixture lost the legacy dash"


def test_keeps_every_other_frontmatter_key(store):
    _mem(store, "target")
    store.upsert("memory", "source",
                 {"memory_type": "reference", "description": "kept", "load_behavior": "lazy",
                  "origin": "user", "aliases": ["an obsidian property"]},
                 body=LEGACY)
    created = _front(store, "memory", "source")["created_at"]

    T.set_entity_links(store, "memory", "source", {"part_of": ["target"]})

    front = _front(store, "memory", "source")
    assert front["description"] == "kept"
    assert front["load_behavior"] == "lazy"
    assert front["origin"] == "user"
    assert front["memory_type"] == "reference"
    assert front["aliases"] == ["an obsidian property"]
    assert front["created_at"] == created
    assert front["part_of"] == ["[[global/memory/target.md]]"]


def test_the_first_patch_changes_the_file_and_a_repeat_does_not(store):
    'test the first patch changes the file and a repeat does not.'
    _mem(store, "source")
    _mem(store, "target")
    before = _file_bytes(store, "memory", "source")

    T.set_entity_links(store, "memory", "source", {"sibling": ["target"]})
    after_write = _file_bytes(store, "memory", "source")

    T.set_entity_links(store, "memory", "source", {"sibling": ["target"]})

    assert after_write != before
    assert _file_bytes(store, "memory", "source") == after_write
    assert _front(store, "memory", "source")["sibling"] == ["[[global/memory/target.md]]"]


def test_an_unknown_relation_is_refused_and_nothing_is_written(store):
    _mem(store, "source")
    before = _file_bytes(store, "memory", "source")

    out = T.set_entity_links(store, "memory", "source", {"relates_to": ["target"]})

    assert "error" in out, out
    assert "relates_to" in out["error"]
    assert _file_bytes(store, "memory", "source") == before


def test_a_dangling_target_is_written_with_a_warning(store):
    _mem(store, "source")

    out = T.set_entity_links(store, "memory", "source", {"sibling": ["nothing-by-this-name"]})

    assert "error" not in out, out
    assert "nothing-by-this-name" in (out.get("warning") or "")
    assert _front(store, "memory", "source")["sibling"] == ["[[nothing-by-this-name]]"]


def test_an_empty_list_removes_only_that_relation(store):
    _mem(store, "source")
    _mem(store, "target")
    T.set_entity_links(store, "memory", "source",
                       {"sibling": ["target"], "part_of": ["target"]})

    out = T.set_entity_links(store, "memory", "source", {"sibling": []})

    assert "error" not in out, out
    front = _front(store, "memory", "source")
    assert "sibling" not in front
    assert front["part_of"] == ["[[global/memory/target.md]]"]


def test_relations_the_call_does_not_name_are_kept(store):
    _mem(store, "source")
    _mem(store, "target")
    T.set_entity_links(store, "memory", "source", {"supersedes": ["target"]})

    T.set_entity_links(store, "memory", "source", {"sibling": ["target"]})

    front = _front(store, "memory", "source")
    assert front["supersedes"] == ["[[global/memory/target.md]]"]
    assert front["sibling"] == ["[[global/memory/target.md]]"]


def test_a_missing_entity_is_an_error_and_creates_nothing(store):
    out = T.set_entity_links(store, "memory", "no-such-memory", {"sibling": ["x"]})

    assert "error" in out, out
    assert "no-such-memory" in out["error"]
    assert _scopes_named(store, "memory", "no-such-memory") == []


def test_links_must_be_a_mapping_of_relation_to_targets(store):
    _mem(store, "source")

    out = T.set_entity_links(store, "memory", "source", ["sibling"])

    assert "error" in out, out
    assert "relation" in out["error"]


def test_naming_no_relation_at_all_is_refused(store):
    _mem(store, "source")
    before = _file_bytes(store, "memory", "source")

    out = T.set_entity_links(store, "memory", "source", {})

    assert "error" in out, out
    assert _file_bytes(store, "memory", "source") == before


def test_the_receipt_carries_no_body(store):
    _mem(store, "source")
    _mem(store, "target")

    out = T.set_entity_links(store, "memory", "source", {"sibling": ["target"]})

    assert "body" not in out, out
    assert out["links_set"] == {"sibling": ["[[global/memory/target.md]]"]}
    assert out["scope"] == "global"




def test_a_doc_takes_a_relation_without_resending_its_body(store):
    T.upsert_doc(store, "notes/one.md", LEGACY)
    T.upsert_doc(store, "notes/two.md", "other\n")
    before = _body_bytes(_file_bytes(store, "doc", "notes/one.md"))

    out = T.set_entity_links(store, "doc", "notes/one.md", {"sibling": ["notes/two.md"]})

    assert "error" not in out, out
    assert _front(store, "doc", "notes/one.md")["sibling"] == ["[[global/docs/notes/two.md]]"]
    assert _body_bytes(_file_bytes(store, "doc", "notes/one.md")) == before


def test_a_skill_takes_a_relation_without_resending_its_body(store):
    T.upsert_skill(store, "one", "first skill", LEGACY)
    T.upsert_skill(store, "two", "second skill", "other\n")
    before = _body_bytes(_file_bytes(store, "skill", "one"))

    out = T.set_entity_links(store, "skill", "one", {"part_of": ["two"]})

    assert "error" not in out, out
    assert _front(store, "skill", "one")["part_of"] == ["[[global/skills/two/SKILL.md]]"]
    assert _body_bytes(_file_bytes(store, "skill", "one")) == before


def test_unknown_kind_is_refused(store):
    T.upsert_command(store, "cmd", body="do a thing\n", description="a command")

    out = T.set_entity_links(store, "machine", "cmd", {"sibling": ["x"]})

    assert "error" in out, out
    assert "memory" in out["error"] and "doc" in out["error"] and "skill" in out["error"]




def test_the_typed_card_shows_after_the_patch(store):
    _mem(store, "source")
    _mem(store, "target")

    T.set_entity_links(store, "memory", "source", {"sibling": ["target"]})

    cards = _read(T.get_memory(store, "source"))["links"]
    assert any(c.startswith("→ sibling memory target") for c in cards), cards
    back = _read(T.get_memory(store, "target"))["links"]
    assert any(c.startswith("← sibling memory source") for c in back), back


def test_explore_filters_on_the_patched_relation(store):
    _mem(store, "source")
    _mem(store, "target")
    T.set_entity_links(store, "memory", "source", {"sibling": ["target"]})

    out = _read(T.explore(store, "memory", "source", rel="sibling"))

    assert [c for c in out["cards"] if "sibling memory target" in c], out




def test_a_project_entity_is_patched_at_project_scope_without_forking_global(store):
    T.upsert_project(store, "gh:org/p", "P")
    _mem(store, "source", project="P")
    _mem(store, "target", project="P")

    out = T.set_entity_links(store, "memory", "source", {"sibling": ["target"]}, project="P")

    assert "error" not in out, out
    assert out["scope"] == "project:P", out
    assert _scopes_named(store, "memory", "source") == ["project:P"]


def test_a_global_entity_reached_from_a_project_stays_global(store):
    T.upsert_project(store, "gh:org/p", "P")
    _mem(store, "source")
    _mem(store, "target")

    out = T.set_entity_links(store, "memory", "source", {"sibling": ["target"]}, project="P")

    assert "error" not in out, out
    assert out["scope"] == "global", out
    assert _scopes_named(store, "memory", "source") == ["global"]


def test_a_workspace_entity_is_patched_at_workspace_scope(store):
    T.upsert_project(store, "gh:org/p", "P", workspace="W")
    store.upsert("memory", "source",
                 {"memory_type": "reference", "description": "ws", "load_behavior": "lazy"},
                 body=LEGACY, scope="ws:W")
    _mem(store, "target")

    out = T.set_entity_links(store, "memory", "source", {"sibling": ["target"]}, workspace="W")

    assert "error" not in out, out
    assert out["scope"] == "ws:W", out
    assert _scopes_named(store, "memory", "source") == ["ws:W"]




def test_upsert_with_links_still_writes_them(store):
    _mem(store, "target")

    out = T.upsert_memory(store, "source", "reference", "d", "b\n",
                          links={"sibling": ["target"]})

    assert "error" not in out, out
    assert _front(store, "memory", "source")["sibling"] == ["[[global/memory/target.md]]"]


def test_a_later_body_upsert_keeps_a_patched_relation(store):
    _mem(store, "source")
    _mem(store, "target")
    T.set_entity_links(store, "memory", "source", {"sibling": ["target"]})

    T.upsert_memory(store, "source", "reference", "new description", "a new body\n")

    front = _front(store, "memory", "source")
    assert front["sibling"] == ["[[global/memory/target.md]]"]
    assert front["description"] == "new description"
