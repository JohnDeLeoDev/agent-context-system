'Context graph T5, review finding: a supersede cycle must not hide its entities.'
import pytest

from agent_context import fstools as T
from agent_context import usage


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _mem(store, slug, body="b\n", **kw):
    return T.upsert_memory(store, slug, "reference", f"about {slug}", body, **kw)


def _got(result):
    assert result is not None, "read returned nothing"
    return result


def _keys(cards):
    return sorted({c.split(" ")[3] for c in cards})


def test_a_two_entity_cycle_leaves_both_visible_and_neither_superseded(store):
    _mem(store, "a")
    _mem(store, "b", links={"supersedes": ["a"]})
    _mem(store, "a", links={"supersedes": ["b"]})
    _mem(store, "reader", body="see [[a]] and [[b]]\n")

    assert _keys(_got(T.get_memory(store, "reader"))["links"]) == ["a", "b"]
    assert _keys(_got(T.explore(store, "memory", "reader"))["cards"]) == ["a", "b"]
    assert "superseded_by" not in _got(T.get_memory(store, "a"))
    assert "superseded_by" not in _got(T.get_memory(store, "b"))
    assert T.check_integrity(store)["superseded_still_linked"] == []


def test_a_longer_cycle_is_treated_the_same(store):
    for s in ("a", "b", "c"):
        _mem(store, s)
    _mem(store, "a", links={"supersedes": ["b"]})
    _mem(store, "b", links={"supersedes": ["c"]})
    _mem(store, "c", links={"supersedes": ["a"]})
    _mem(store, "reader", body="[[a]] [[b]] [[c]]\n")
    assert _keys(_got(T.get_memory(store, "reader"))["links"]) == ["a", "b", "c"]
    for s in ("a", "b", "c"):
        assert "superseded_by" not in _got(T.get_memory(store, s)), s


def test_an_entity_outside_the_cycle_is_still_superseded(store):
    _mem(store, "a")
    _mem(store, "old")
    _mem(store, "b", links={"supersedes": ["a"]})
    _mem(store, "a", links={"supersedes": ["b", "old"]})
    _mem(store, "reader", body="[[a]] [[b]] [[old]]\n")
    assert _keys(_got(T.get_memory(store, "reader"))["links"]) == ["a", "b"]
    assert _got(T.get_memory(store, "old"))["superseded_by"] == ["memory a"]


def test_supersede_cycles_reports_each_cycle_once(store):
    _mem(store, "a")
    _mem(store, "b", links={"supersedes": ["a"]})
    _mem(store, "a", links={"supersedes": ["b"]})
    for s in ("x", "y", "z"):
        _mem(store, s)
    _mem(store, "x", links={"supersedes": ["y"]})
    _mem(store, "y", links={"supersedes": ["z"]})
    _mem(store, "z", links={"supersedes": ["x"]})
    _mem(store, "new", links={"supersedes": ["plain-old"]})
    _mem(store, "plain-old")
    _mem(store, "self", links={"supersedes": ["self"]})

    f = T.check_integrity(store)
    assert f["supersede_cycles"] == [
        {"entities": ["memory a", "memory b"]},
        {"entities": ["memory x", "memory y", "memory z"]}]
    assert f["summary"]["supersede_cycles"] == 2


def test_no_cycle_no_rows(store):
    _mem(store, "old")
    _mem(store, "new", links={"supersedes": ["old"]})
    f = T.check_integrity(store)
    assert f["supersede_cycles"] == []
    assert f["summary"]["supersede_cycles"] == 0
