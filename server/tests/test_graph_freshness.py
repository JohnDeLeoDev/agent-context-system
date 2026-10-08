'Context graph T2: cards follow every way the index changes (criterion 7).'
import os

import pytest

from agent_context import fstools as T
from agent_context import usage


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _mem(store, slug, body):
    return T.upsert_memory(store, slug, "reference", f"about {slug}", body)


def _cards(store, slug):
    'Direction and key of each card on one memory, e.g. ["→ b", "← a"].'
    r = T.get_memory(store, slug)
    assert r is not None and "links" in r, r
    return [c.split(" ")[0] + " " + c.split(" ")[3] for c in r["links"]]


def _seed(store):
    _mem(store, "b", "b\n")
    _mem(store, "c", "c\n")
    _mem(store, "a", "see [[b]]\n")
    assert _cards(store, "a") == ["→ b"]
    assert _cards(store, "b") == ["← a"]


def _hand_edit(path, old, new):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert old in text, text
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new))


def test_upsert(store):
    _seed(store)
    _mem(store, "a", "see [[c]]\n")
    assert _cards(store, "a") == ["→ c"]
    assert _cards(store, "b") == []
    assert _cards(store, "c") == ["← a"]


def test_a_new_source_adds_a_backlink_without_touching_the_target(store):
    _seed(store)
    _mem(store, "z", "also [[b]]\n")
    assert sorted(_cards(store, "b")) == ["← a", "← z"]


def test_edit_body(store):
    _seed(store)
    T.edit_body(store, "memory", "a", "[[b]]", "[[c]]")
    assert _cards(store, "a") == ["→ c"]
    assert _cards(store, "b") == []


def test_bulk_edit(store):
    _seed(store)
    T.bulk_edit(store, [{"kind": "memory", "key": "a", "replacements": [["[[b]]", "[[c]]"]]}])
    assert _cards(store, "a") == ["→ c"]


def test_delete_entity(store):
    _seed(store)
    T.delete_entity(store, "memory", "b")
    assert _cards(store, "a") == []


def test_sweep_vanished(store):
    _seed(store)
    os.remove(store.get("memory", "b")["_path"])
    store.sweep_vanished()
    assert _cards(store, "a") == []


def test_a_hand_edit_picked_up_by_fresh(store):
    _seed(store)
    path = store.get("memory", "a")["_path"]
    _hand_edit(path, "[[b]]", "[[c]]")
    later = os.path.getmtime(path) + 10
    os.utime(path, (later, later))
    assert _cards(store, "a") == ["→ c"]
    assert _cards(store, "c") == ["← a"]


def test_a_sync_reload(store):
    _seed(store)
    _hand_edit(store.get("memory", "a")["_path"], "[[b]]", "[[c]]")
    store.reload()
    assert _cards(store, "a") == ["→ c"]
    assert _cards(store, "b") == []
