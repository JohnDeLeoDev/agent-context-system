"Context graph T3: the store as an Obsidian vault.\n\nObsidian rewrites a note's frontmatter as YAML whenever user edits a property, and a\nnote he creates there has no store frontmatter at all. These tests pin what the loader\nmust do with both, and the integrity findings that make a hand move or rename visible.\n\nFixture YAML follows Obsidian's Properties documentation (obsidian.md/help/properties):\n`key: value`, lists as `key:` then `  - item`, links quoted, `[]` for an empty list."
import json
from pathlib import Path

import pytest

from agent_context import fstools as T
from agent_context.index import _is_lazy
from agent_context.store import emit_frontmatter, parse_frontmatter, stable_uuid


def _write(store, rel, text):
    p = Path(store.root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _frontmatter_lines(text):
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    assert end != -1
    return text[4:end].splitlines()


def _body(entity):
    assert entity is not None
    return entity["body"]




OBSIDIAN_YAML = """---
title: A New Hope
quoted: "double: quoted"
single: 'it''s single'
year: 1977
pie: 3.14
neg: -2
favorite: true
reply: false
aliases: []
date: 2020-08-21
cast:
  - Mark Hamill
  - Harrison Ford
links:
  - "[[Link]]"
  - "[[Link2]]"
nums:
  - 1
  - 2
flush:
- one
- two
after: kept
---
Body line
"""

EXPECTED = {
    "title": "A New Hope",
    "quoted": "double: quoted",
    "single": "it's single",
    "year": 1977,
    "pie": 3.14,
    "neg": -2,
    "favorite": True,
    "reply": False,
    "aliases": [],
    "date": "2020-08-21",
    "cast": ["Mark Hamill", "Harrison Ford"],
    "links": ["[[Link]]", "[[Link2]]"],
    "nums": [1, 2],
    "flush": ["one", "two"],
    "after": "kept",
}


def test_obsidian_yaml_parses_to_the_json_line_values():
    meta, body = parse_frontmatter(OBSIDIAN_YAML)
    assert meta == EXPECTED
    assert body == "Body line\n"


def test_yaml_and_json_line_forms_of_one_entity_parse_identically():
    json_meta, json_body = parse_frontmatter(emit_frontmatter(EXPECTED, "Body line"))
    yaml_meta, _ = parse_frontmatter(OBSIDIAN_YAML)
    assert yaml_meta == json_meta == EXPECTED
    assert json_body == "Body line\n"


@pytest.mark.parametrize("key", sorted(k for k in EXPECTED if k != "date"))
def test_each_value_matches_a_real_yaml_parser(key):
    yaml = pytest.importorskip("yaml")
    block = OBSIDIAN_YAML[4:OBSIDIAN_YAML.find("\n---\n", 4)]
    meta, _ = parse_frontmatter(OBSIDIAN_YAML)
    assert meta[key] == yaml.safe_load(block)[key]


def test_block_list_is_a_list_not_a_string():
    meta, _ = parse_frontmatter("---\ntags:\n  - journal\n  - personal\n---\nb\n")
    assert meta == {"tags": ["journal", "personal"]}


def test_wrapped_scalar_still_joins_onto_its_key():
    'Unchanged: an imported SKILL.md whose description wraps onto indented lines.'
    text = "---\nname: s\ndescription: first line\n  second line\n---\nb\n"
    meta, _ = parse_frontmatter(text)
    assert meta == {"name": "s", "description": "first line second line"}


def test_json_line_frontmatter_is_unchanged():
    fields = {"uuid": "u-1", "type": "memory", "slug": "s", "description": "a: b, \"c\"",
              "metadata": {"k": [1, 2]}, "tags": ["x"], "n": 0, "flag": False}
    meta, body = parse_frontmatter(emit_frontmatter(fields, "line one\n\nline two"))
    assert meta == fields
    assert body == "line one\n\nline two\n"




def _obsidian_memory(slug):
    uid = stable_uuid("memory", "global", slug)
    return f"""---
uuid: {uid}
type: memory
slug: {slug}
memory_type: reference
description: "Written by Obsidian: kept"
load_behavior: lazy
tags:
  - alpha
  - "[[beta]]"
rating: 4.5
favorite: true
aliases: []
---
old body
"""


_KEPT = {"memory_type": "reference", "load_behavior": "lazy",
         "tags": ["alpha", "[[beta]]"], "rating": 4.5, "favorite": True, "aliases": []}


def _assert_json_line_form(path, expect):
    text = Path(path).read_text()
    for line in _frontmatter_lines(text):
        assert not line[:1].isspace(), f"not JSON-line form: {line!r}"
        _, _, v = line.partition(":")
        json.loads(v.strip())
    meta, _ = parse_frontmatter(text)
    for k, v in expect.items():
        assert meta[k] == v, k
    return meta


def test_edit_body_keeps_obsidian_values_and_writes_json_lines(store):
    p = _write(store, "global/memory/obs-note.md", _obsidian_memory("obs-note"))
    store.reload()
    res = T.edit_body(store, "memory", "obs-note", "old body", "new body")
    assert "error" not in (res or {})
    meta = _assert_json_line_form(p, {**_KEPT, "description": "Written by Obsidian: kept"})
    assert meta["uuid"] == stable_uuid("memory", "global", "obs-note")
    assert _body(T.get_memory(store, "obs-note")) == "new body"


def test_set_memory_description_keeps_obsidian_values(store):
    p = _write(store, "global/memory/obs-desc.md", _obsidian_memory("obs-desc"))
    store.reload()
    res = T.set_memory_description(store, "obs-desc", "changed")
    assert "error" not in (res or {})
    _assert_json_line_form(p, {**_KEPT, "description": "changed"})


def test_doc_edit_body_keeps_obsidian_values(store):
    uid = stable_uuid("doc", "global", "guides/vault.md")
    p = _write(store, "global/docs/guides/vault.md",
               f"---\nuuid: {uid}\ntype: doc\npath: guides/vault.md\ntitle: Vault\n"
               "cssclasses:\n  - wide\n---\nfirst\n")
    store.reload()
    res = T.edit_body(store, "doc", "guides/vault.md", "first", "second")
    assert "error" not in (res or {})
    _assert_json_line_form(p, {"title": "Vault", "cssclasses": ["wide"]})




def test_uuidless_memory_gets_path_derived_identity(store):
    p = _write(store, "global/memory/hand-note.md", "Just a body\n")
    before = p.read_bytes()
    store.reload()
    e = store.get("memory", "hand-note")
    assert e is not None
    assert e["uuid"] == stable_uuid("memory", "global", "hand-note")
    assert (e["type"], e["slug"], e["scope"]) == ("memory", "hand-note", "global")
    assert e["memory_type"] == "reference"
    assert e.get("load_behavior") is None
    assert _is_lazy(e)
    assert _body(T.get_memory(store, "hand-note")) == "Just a body"
    assert p.read_bytes() == before, "the loader must never rewrite the file"
    assert not [x for x in store.load_errors if x["path"] == "global/memory/hand-note.md"]


def test_uuidless_memory_keeps_its_obsidian_properties(store):
    _write(store, "global/memory/tagged.md",
           "---\nmemory_type: feedback\ntags:\n  - x\n---\nBody\n")
    store.reload()
    e = store.get("memory", "tagged")
    assert e is not None
    assert e["uuid"] == stable_uuid("memory", "global", "tagged")
    assert e["tags"] == ["x"]
    assert e["memory_type"] == "feedback"


def test_uuidless_doc_gets_path_derived_identity_at_any_depth(store):
    p = _write(store, "global/docs/notes/idea.md", "An idea\n")
    before = p.read_bytes()
    store.reload()
    e = store.get("doc", "notes/idea.md")
    assert e is not None
    assert e["uuid"] == stable_uuid("doc", "global", "notes/idea.md")
    assert (e["type"], e["path"], e["scope"]) == ("doc", "notes/idea.md", "global")
    assert _body(T.get_doc(store, "notes/idea.md")) == "An idea"
    assert p.read_bytes() == before


@pytest.mark.parametrize("rel,typ,scope,key", [
    ("projects/P/docs/x.md", "doc", "project:P", "x.md"),
    ("workspaces/W/memory/w-note.md", "memory", "ws:W", "w-note"),
])
def test_path_derived_identity_in_project_and_workspace_scopes(store, rel, typ, scope, key):
    _write(store, rel, "body\n")
    store.reload()
    assert store.by_key.get((typ, scope, key)) == stable_uuid(typ, scope, key)


def test_check_integrity_lists_path_derived_entities(store):
    _write(store, "global/memory/hand-note.md", "Just a body\n")
    _write(store, "global/docs/notes/idea.md", "An idea\n")
    T.upsert_memory(store, "normal", "reference", "d", "b")
    store.reload()
    f = T.check_integrity(store)
    rows = {(x["path"], x["type"], x["key"], x["scope"]) for x in f["path_derived_identity"]}
    assert rows == {("global/memory/hand-note.md", "memory", "hand-note", "global"),
                    ("global/docs/notes/idea.md", "doc", "notes/idea.md", "global")}
    assert f["summary"]["path_derived_identity"] == 2


def test_an_mcp_write_adopts_a_path_derived_entity(store):
    p = _write(store, "global/memory/hand-note.md", "Just a body\n")
    store.reload()
    res = T.edit_body(store, "memory", "hand-note", "Just", "Only")
    assert "error" not in (res or {})
    meta, _ = parse_frontmatter(p.read_text())
    assert meta["uuid"] == stable_uuid("memory", "global", "hand-note")
    assert T.check_integrity(store)["path_derived_identity"] == []


@pytest.mark.parametrize("rel", [
    "global/skills/s/SKILL.md",
    "global/instructions/i.md",
    "global/commands/c.md",
    "global/agents/a.md",
    "global/skills/s/docs/ref.md",
    "global/memory/sub/nested.md",
])
def test_other_uuidless_entity_files_keep_the_load_error(store, rel):
    _write(store, rel, "---\nname: x\n---\nbody\n")
    store.reload()
    assert any(x["path"] == rel and "unindexed" in x["error"] for x in store.load_errors)
    assert not [e for e in store.entities.values()
                if e.get("_path", "").endswith(rel)]


def test_uuidless_markdown_outside_a_scope_root_is_still_ignored(store):
    _write(store, "shared-skills/x/memory/y.md", "body\n")
    _write(store, "templates/t/docs/z.md", "body\n")
    store.reload()
    assert store.load_errors == []
    assert store.entities == {} or not [e for e in store.entities.values()
                                        if "shared-skills" in e.get("_path", "")
                                        or "templates" in e.get("_path", "")]




def _move(store, src_rel, dst_scope, tail):
    root = Path(store.root)
    dst = Path(store._base_dir(dst_scope)) / tail
    dst.parent.mkdir(parents=True, exist_ok=True)
    (root / src_rel).rename(dst)
    store.reload()
    return str(dst.relative_to(root))


def test_memory_moved_across_scopes_is_reported_and_still_readable(store):
    T.upsert_project(store, "github.com:org/p", "P")
    T.upsert_memory(store, "mover", "reference", "d", "moved body")
    T.upsert_memory(store, "stayer", "reference", "d", "b")
    new_rel = _move(store, "global/memory/mover.md", "project:P", "memory/mover.md")
    f = T.check_integrity(store)
    hits = [x for x in f["uuid_scope_mismatch"] if x["key"] == "mover"]
    assert len(hits) == 1
    assert hits[0]["type"] == "memory"
    assert hits[0]["scope"] == "project:P"
    assert hits[0]["from_scope"] == "global"
    assert hits[0]["path"] == new_rel
    assert not [x for x in f["uuid_scope_mismatch"] if x["key"] == "stayer"]
    assert f["summary"]["uuid_scope_mismatch"] == 1
    assert _body(T.get_memory(store, "mover", project="P")) == "moved body"


def test_doc_moved_across_scopes_is_reported(store):
    T.upsert_project(store, "github.com:org/p", "P")
    T.upsert_doc(store, "guides/a.md", body="doc body")
    _move(store, "global/docs/guides/a.md", "project:P", "docs/guides/a.md")
    hits = [x for x in T.check_integrity(store)["uuid_scope_mismatch"]
            if x["key"] == "guides/a.md"]
    assert [(h["type"], h["scope"], h["from_scope"]) for h in hits] == [
        ("doc", "project:P", "global")]


def test_clean_store_has_no_move_or_rename_findings(store):
    T.upsert_memory(store, "m", "reference", "d", "b")
    T.upsert_doc(store, "guides/a.md", body="x")
    _write(store, "global/memory/hand-note.md", "Just a body\n")
    store.reload()
    f = T.check_integrity(store)
    assert f["uuid_scope_mismatch"] == []
    assert f["filename_key_mismatch"] == []




def test_renamed_memory_is_reported_and_resolves_by_frontmatter(store):
    T.upsert_memory(store, "alpha", "reference", "d", "alpha body")
    root = Path(store.root)
    (root / "global/memory/alpha.md").rename(root / "global/memory/beta.md")
    store.reload()
    f = T.check_integrity(store)
    hits = [x for x in f["filename_key_mismatch"] if x["key"] == "alpha"]
    assert len(hits) == 1
    assert hits[0]["type"] == "memory"
    assert hits[0]["path"] == "global/memory/beta.md"
    assert hits[0]["expected"] == "global/memory/alpha.md"
    assert f["summary"]["filename_key_mismatch"] == 1
    assert _body(T.get_memory(store, "alpha")) == "alpha body"
    assert T.get_memory(store, "beta") is None


def test_renamed_doc_is_reported(store):
    T.upsert_doc(store, "guides/a.md", body="x")
    root = Path(store.root)
    (root / "global/docs/guides/a.md").rename(root / "global/docs/guides/b.md")
    store.reload()
    hits = T.check_integrity(store)["filename_key_mismatch"]
    assert [(h["type"], h["key"], h["path"], h["expected"]) for h in hits] == [
        ("doc", "guides/a.md", "global/docs/guides/b.md", "global/docs/guides/a.md")]
