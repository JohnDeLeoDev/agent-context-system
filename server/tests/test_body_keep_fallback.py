'Context graph T9, found in review: what an omitted body falls back to when the file\ncannot be read.\n\n`store.exact_body` returns None for a file whose frontmatter block is gone, whose bytes\nare not UTF-8, or that has been deleted while the index still holds the entity. Treating\nthat None as an empty body replaced the prose with nothing: a route billed as touching\none frontmatter key emptied the file instead. The index copy has a stripped trailing\nnewline, which is a one byte diff. An emptied file is the whole body.'
import pytest

from agent_context import fstools as T
from agent_context import usage


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _path(store, kind, key):
    e = store.get(kind, key)
    assert e is not None, (kind, key)
    return e["_path"]


def _text(path):
    with open(path, newline="") as fh:
        return fh.read()


def _break_frontmatter(path):
    'Lose the closing delimiter, the shape a bad merge or a hand edit can leave.'
    raw = _text(path)
    with open(path, "w", newline="") as fh:
        fh.write(raw.replace("\n---\n", "\n--\n", 1))


def test_a_frontmatter_only_write_keeps_prose_it_cannot_read_back(store):
    T.upsert_memory(store, "subject", "reference", "d", "important prose\n")
    path = _path(store, "memory", "subject")
    _break_frontmatter(path)
    store.reload()

    T.set_memory_description(store, "subject", "a new description")

    assert "important prose" in _text(path)


def test_set_entity_links_keeps_prose_it_cannot_read_back(store):
    T.upsert_memory(store, "subject", "reference", "d", "important prose\n")
    T.upsert_memory(store, "target", "reference", "d", "b\n")
    path = _path(store, "memory", "subject")
    _break_frontmatter(path)
    store.reload()

    T.set_entity_links(store, "memory", "subject", {"sibling": ["target"]})

    assert "important prose" in _text(path)


def test_a_new_entity_with_no_body_is_still_created_empty(store):
    'The fallback must not invent a body where nothing is stored.'
    store.upsert("memory", "fresh", {"memory_type": "reference", "description": "d"},
                 body=None, scope="global")

    assert _text(_path(store, "memory", "fresh")).endswith("---\n\n\n")
