'Context graph T9: a write that does not mean to touch the body must not touch it.\n\nThe index holds a body with trailing newlines stripped (store.py:670) and\n`emit_frontmatter` adds exactly one back, so any route that hands the index copy back\nto `store.upsert` collapses a trailing blank line. T8 measured the blast radius on the\nlive tree: the last byte of 247 of 1,131 markdown entities, each a one line diff in a\nfile nobody edited, on a tree that syncs to seven machines.\n\n`store.upsert(body=None)` is the fix: the frontmatter is rewritten and every byte after\nthe closing `---` stays as it is. These tests pin that promise and the routes that ride\non it. `test_set_entity_links_bytes.py` (T8) pins the same promise for the one route\nthat bought it with a workaround.'
import pytest

from agent_context import fstools as T
from agent_context import usage
from agent_context.shaping import _carry_fields


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _raw(store, kind, key):
    e = store.get(kind, key)
    assert e is not None, (kind, key)
    with open(e["_path"], "rb") as fh:
        return fh.read()


def _body_bytes(raw):
    "The file's bytes after the closing `---` of the frontmatter block."
    assert raw.startswith(b"---\n"), raw[:40]
    return raw.split(b"\n---\n", 1)[1]


def _frontmatter_bytes(raw):
    return raw.split(b"\n---\n", 1)[0]




def _memory_with(store, body, slug="subject"):
    T.upsert_memory(store, slug, "reference", "d", body)
    return _raw(store, "memory", slug)


def _patch_frontmatter_only(store, kind, key, **override):
    "Re-upsert an entity's frontmatter, passing no body at all."
    e = store.get(kind, key)
    natural = {"memory": "slug", "doc": "path", "skill": "name",
               "command": "name", "script": "name", "hook": "name"}[kind]
    fields = _carry_fields(e, natural, **override)
    return store.upsert(kind, key, fields, body=None, scope=e.get("scope"))


def test_trailing_blank_lines_survive_a_frontmatter_only_write(store):
    'The case behind the 247 files. An Obsidian edit leaves whatever newlines the\n    editor left; the index strips them all and emit_frontmatter adds one back.'
    before = _memory_with(store, "text\n\n\n")

    _patch_frontmatter_only(store, "memory", "subject", description="changed")
    after = _raw(store, "memory", "subject")

    assert _body_bytes(before).endswith(b"\n\n\n\n")
    assert _body_bytes(after) == _body_bytes(before)


def test_a_crlf_body_keeps_its_line_endings_on_a_frontmatter_only_write(store):
    before = _memory_with(store, "line1\r\nline2\r\n")

    _patch_frontmatter_only(store, "memory", "subject", description="changed")
    after = _raw(store, "memory", "subject")

    assert b"\r\n" in _body_bytes(before), "fixture lost its CRLF endings"
    assert _body_bytes(after) == _body_bytes(before)


def test_a_frontmatter_only_write_still_rewrites_the_frontmatter(store):
    'Keeping the body is not the same as writing nothing.'
    _memory_with(store, "text\n\n\n")

    _patch_frontmatter_only(store, "memory", "subject", description="changed")
    raw = _raw(store, "memory", "subject")

    assert b'"changed"' in _frontmatter_bytes(raw)


def test_a_no_op_frontmatter_only_write_produces_no_diff(store):
    'test a no op frontmatter only write produces no diff.'
    before = _memory_with(store, "text\n\n\n")

    _patch_frontmatter_only(store, "memory", "subject")
    after = _raw(store, "memory", "subject")

    assert after == before


def test_body_none_keeps_the_bytes_of_a_doc(store):
    T.upsert_doc(store, "guides/a.md", "doc text\n\n")
    before = _raw(store, "doc", "guides/a.md")

    _patch_frontmatter_only(store, "doc", "guides/a.md", title="changed")
    after = _raw(store, "doc", "guides/a.md")

    assert _body_bytes(after) == _body_bytes(before)


def test_body_none_keeps_the_bytes_of_a_skill(store):
    T.upsert_skill(store, "a-skill", "d", "skill text\n\n")
    before = _raw(store, "skill", "a-skill")

    _patch_frontmatter_only(store, "skill", "a-skill", description="changed")
    after = _raw(store, "skill", "a-skill")

    assert _body_bytes(after) == _body_bytes(before)


def test_body_none_on_a_new_entity_still_writes_an_empty_body(store):
    'Nothing to preserve, so the create case is unchanged.'
    store.upsert("memory", "fresh", {"memory_type": "reference", "description": "d"},
                 body=None, scope="global")

    assert _body_bytes(_raw(store, "memory", "fresh")) == b"\n\n"




def test_set_memory_description_keeps_body_bytes(store):
    before = _memory_with(store, "text\n\n\n")

    out = T.set_memory_description(store, "subject", "a new description")

    assert "error" not in out, out
    assert _body_bytes(_raw(store, "memory", "subject")) == _body_bytes(before)


def test_set_memory_load_behavior_keeps_body_bytes(store):
    before = _memory_with(store, "text\n\n\n")

    out = T.set_memory_load_behavior(store, "subject", "always")

    assert "error" not in out, out
    assert _body_bytes(_raw(store, "memory", "subject")) == _body_bytes(before)


def test_set_entity_links_keeps_body_bytes(store):
    'T8 bought this with a workaround that read the file. It must survive the route\n    switching to body=None.'
    before = _memory_with(store, "text\n\n\n")
    T.upsert_memory(store, "target", "reference", "d", "b\n")

    out = T.set_entity_links(store, "memory", "subject", {"sibling": ["target"]})

    assert "error" not in out, out
    assert _body_bytes(_raw(store, "memory", "subject")) == _body_bytes(before)


def test_a_metadata_only_upsert_script_does_not_rewrite_the_script(store):
    "A script's body is the whole file, and the loader reads it with Python's default\n    newline handling, so a CRLF script comes back from disk as LF and a description\n    change rewrites every line ending in it.\n\n    The reload is the point: within one process the index still holds the body the write\n    put there, so the substitution is invisible until the daemon next reads the file."
    T.upsert_script(store, "a-script", "#!/bin/sh\r\necho hi\r\n", description="d")
    before = _raw(store, "script", "a-script")
    store.reload()

    T.upsert_script(store, "a-script", description="a new description")
    after = _raw(store, "script", "a-script")

    assert b"\r\n" in before, "fixture lost its CRLF endings"
    assert after == before


def test_a_metadata_only_upsert_hook_does_not_rewrite_the_hook(store):
    T.upsert_hook(store, "a-hook", event_type="PreToolUse",
                  script_body="#!/bin/sh\r\nexit 0\r\n", description="d")
    before = _raw(store, "hook", "a-hook")
    store.reload()

    T.upsert_hook(store, "a-hook", description="a new description")
    after = _raw(store, "hook", "a-hook")

    assert b"\r\n" in before, "fixture lost its CRLF endings"
    assert after == before


def test_a_metadata_only_upsert_doc_keeps_body_bytes(store):
    T.upsert_doc(store, "guides/a.md", "doc text\n\n")
    before = _raw(store, "doc", "guides/a.md")

    T.upsert_doc(store, "guides/a.md", title="a new title")
    after = _raw(store, "doc", "guides/a.md")

    assert _body_bytes(after) == _body_bytes(before)




def test_edit_body_changes_only_its_span(store):
    before = _memory_with(store, "above\nTARGET\nbelow\n\n\n")

    out = T.edit_body(store, "memory", "subject", "TARGET", "REPLACED")

    assert isinstance(out, dict) and "error" not in out, out
    assert _body_bytes(_raw(store, "memory", "subject")) == \
        _body_bytes(before).replace(b"TARGET", b"REPLACED")


def test_edit_body_keeps_crlf_outside_its_span(store):
    before = _memory_with(store, "a\r\nTARGET\r\nb\r\n")

    out = T.edit_body(store, "memory", "subject", "TARGET", "REPLACED")

    assert isinstance(out, dict) and "error" not in out, out
    assert _body_bytes(_raw(store, "memory", "subject")) == \
        _body_bytes(before).replace(b"TARGET", b"REPLACED")


def test_edit_body_on_a_doc_changes_only_its_span(store):
    T.upsert_doc(store, "guides/a.md", "above\nTARGET\nbelow\n\n\n")
    before = _raw(store, "doc", "guides/a.md")

    out = T.edit_body(store, "doc", "guides/a.md", "TARGET", "REPLACED")

    assert isinstance(out, dict) and "error" not in out, out
    assert _body_bytes(_raw(store, "doc", "guides/a.md")) == \
        _body_bytes(before).replace(b"TARGET", b"REPLACED")


def test_edit_body_on_an_instruction_changes_only_its_span(store):
    T.upsert_instruction(store, "An Instruction", "above\nTARGET\nbelow\n\n\n")
    before = _raw(store, "instruction", "An Instruction")

    out = T.edit_body(store, "instruction", "An Instruction", "TARGET", "REPLACED")

    assert isinstance(out, dict) and "error" not in out, out
    assert _body_bytes(_raw(store, "instruction", "An Instruction")) == \
        _body_bytes(before).replace(b"TARGET", b"REPLACED")


def test_edit_body_on_a_script_changes_only_its_span(store):
    T.upsert_script(store, "a-script", "#!/bin/sh\r\nTARGET\r\n", description="d")
    before = _raw(store, "script", "a-script")
    store.reload()

    out = T.edit_body(store, "script", "a-script", "TARGET", "REPLACED")

    assert isinstance(out, dict) and "error" not in out, out
    assert _raw(store, "script", "a-script") == before.replace(b"TARGET", b"REPLACED")


def test_bulk_edit_changes_only_its_span(store):
    before = _memory_with(store, "above\nTARGET\nbelow\n\n\n")

    out = T.bulk_edit(store, [{"kind": "memory", "key": "subject",
                               "replacements": [["TARGET", "REPLACED"]]}])

    assert out["replacements"] == 1, out
    assert _body_bytes(_raw(store, "memory", "subject")) == \
        _body_bytes(before).replace(b"TARGET", b"REPLACED")


def test_bulk_edit_that_matches_nothing_leaves_the_file_alone(store):
    before = _memory_with(store, "above\nTARGET\nbelow\n\n\n")

    out = T.bulk_edit(store, [{"kind": "memory", "key": "subject",
                               "replacements": [["ABSENT", "x"]]}])

    assert out["replacements"] == 0, out
    assert _raw(store, "memory", "subject") == before




def test_a_full_upsert_that_passes_a_body_writes_that_body(store):
    T.upsert_memory(store, "subject", "reference", "d", "first\n")

    T.upsert_memory(store, "subject", "reference", "d", "second")

    assert _body_bytes(_raw(store, "memory", "subject")) == b"\nsecond\n"


def test_the_index_still_holds_the_stripped_body(store):
    _memory_with(store, "text\n\n\n")

    _patch_frontmatter_only(store, "memory", "subject", description="changed")

    assert store.get("memory", "subject")["body"] == "text"


def test_a_full_upsert_can_still_empty_a_body(store):
    'Passing an empty body is a caller saying so, and stays distinct from omitting it.'
    _memory_with(store, "text\n\n\n")

    T.upsert_memory(store, "subject", "reference", "d", "")

    assert _body_bytes(_raw(store, "memory", "subject")) == b"\n\n"


def test_omitting_the_body_on_an_unknown_entity_is_still_an_error(store):
    out = T.upsert_doc(store, "guides/missing.md", title="t")

    assert "error" in out and "not found" in out["error"], out
