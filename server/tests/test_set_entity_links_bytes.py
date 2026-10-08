"Context graph T8: what `set_entity_links` promises about the body's BYTES.\n\nCriterion 1 says the route keeps the body byte for byte. `tests/test_set_entity_links.py`\npins the normal case; these are the bodies that a rewrite quietly reshapes."
import pytest

from agent_context import fstools as T
from agent_context import usage


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
    assert raw.startswith(b"---\n"), raw[:40]
    return raw.split(b"\n---\n", 1)[1]


def _pair(store, body):
    T.upsert_memory(store, "source", "reference", "d", body)
    T.upsert_memory(store, "target", "reference", "d", "b\n")
    return _body_bytes(_raw(store, "memory", "source"))


def _patch(store):
    out = T.set_entity_links(store, "memory", "source", {"sibling": ["target"]})
    assert "error" not in out, out
    return _body_bytes(_raw(store, "memory", "source"))


def test_a_crlf_body_keeps_its_line_endings(store):
    before = _pair(store, "line1\r\nline2\r\n")

    after = _patch(store)

    assert b"\r\n" in before, "fixture lost its CRLF endings"
    assert after == before


def test_a_mixed_ending_body_is_not_normalized(store):
    before = _pair(store, "lf\ncrlf\r\ncr\rend\n")

    after = _patch(store)

    assert after == before


def test_trailing_blank_lines_survive(store):
    'A hand edit in Obsidian leaves however many newlines the editor left. The index\n    strips them all and emit_frontmatter adds exactly one back, so a body read from the\n    index instead of the file loses them.'
    before = _pair(store, "text\n\n\n\n")

    after = _patch(store)

    assert before.endswith(b"\n\n\n\n")
    assert after == before


def test_a_body_holding_a_horizontal_rule_is_not_resplit(store):
    before = _pair(store, "above\n\n---\n\nbelow\n")

    after = _patch(store)

    assert after == before


def test_an_empty_body_stays_empty(store):
    before = _pair(store, "")

    after = _patch(store)

    assert after == before
