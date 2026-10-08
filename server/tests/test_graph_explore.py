'Context graph T2: explore (criterion 8).\n\nexplore(kind, key, depth=1, rel=None, budget_bytes=4000, project=None, workspace=None)\nreturns cards only, breadth-first. A card below depth 1 is prefixed with the key of the\nnode it was reached from. The budget counts the UTF-8 bytes of the card lines.'
import json

import pytest

from agent_context import fstools as T
from agent_context import server, usage


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _explore(store, *args, **kwargs):
    fn = getattr(T, "explore", None)
    assert fn is not None, "fstools has no explore"
    return fn(store, *args, **kwargs)


def _fmt(n):
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _mem(store, slug, body, **kw):
    return T.upsert_memory(store, slug, "reference", f"about {slug}", body, **kw)


def _card(store, arrow, key, parent=None):
    size = _fmt(len(store.get("memory", key)["body"].encode()))
    line = f"{arrow} mentions memory {key} ({size}): about {key}"
    return f"{parent} {line}" if parent else line


def _seed(store):
    'a -> b, c;  b -> d;  c -> e;  g -> d.'
    for s in ("d", "e"):
        _mem(store, s, f"{s}\n")
    _mem(store, "b", "[[d]]\n")
    _mem(store, "c", "[[e]]\n")
    _mem(store, "g", "[[d]]\n")
    _mem(store, "a", "[[b]] [[c]]\n")


def test_depth_one_returns_the_neighborhood_as_cards(store):
    _seed(store)

    out = _explore(store, "memory", "a")

    assert out["cards"] == [_card(store, "→", "b"), _card(store, "→", "c")]
    assert out["truncated"] == 0


def test_depth_two_is_breadth_first_and_names_the_parent(store):
    _seed(store)

    out = _explore(store, "memory", "a", depth=2)

    assert out["cards"] == [
        _card(store, "→", "b"), _card(store, "→", "c"),
        _card(store, "→", "d", parent="b"), _card(store, "→", "e", parent="c")]
    assert out["truncated"] == 0


def test_explore_reaches_backlinks_too(store):
    _seed(store)

    out = _explore(store, "memory", "d")

    assert out["cards"] == [_card(store, "←", "b"), _card(store, "←", "g")]


def test_explore_stops_before_the_budget_and_counts_what_it_cut(store):
    _seed(store)
    full = _explore(store, "memory", "a", depth=2)
    first = full["cards"][0]

    fits_one = _explore(store, "memory", "a", depth=2, budget_bytes=len(first.encode()))
    fits_none = _explore(store, "memory", "a", depth=2, budget_bytes=len(first.encode()) - 1)

    assert fits_one["cards"] == [first]
    assert fits_one["truncated"] == len(full["cards"]) - 1
    assert fits_none["cards"] == []
    assert fits_none["truncated"] == len(full["cards"])


def test_a_body_over_64_kb_is_carded_but_not_expanded(store):
    _mem(store, "beyond", "b\n")
    _mem(store, "big", "[[beyond]]\n" + "x" * (65 * 1024))
    _mem(store, "root", "[[big]]\n")

    assert _explore(store, "memory", "root", depth=2)["cards"] == [_card(store, "→", "big")]
    
    assert _explore(store, "memory", "big")["cards"] == [
        _card(store, "→", "beyond"), _card(store, "←", "root")]


@pytest.mark.parametrize("depth", [0, 3])
def test_depth_outside_one_to_two_is_an_error(store, depth):
    _seed(store)

    out = _explore(store, "memory", "a", depth=depth)

    assert isinstance(out, dict) and "depth" in out.get("error", ""), out


def test_rel_filters_by_relation(store):
    _seed(store)

    assert _explore(store, "memory", "a", rel="mentions")["cards"] == \
        _explore(store, "memory", "a")["cards"]
    assert _explore(store, "memory", "a", rel="supersedes")["cards"] == []
    assert "error" in _explore(store, "memory", "a", rel="bogus")


def test_explore_starts_from_docs_and_skills(store):
    _mem(store, "m", "b\n")
    T.upsert_doc(store, "guides/a.md", "[[m]]\n", title="Guide A")
    T.upsert_skill(store, "sk", "a skill", "[[m]]\n")

    assert _explore(store, "doc", "guides/a.md")["cards"] == [_card(store, "→", "m")]
    assert _explore(store, "skill", "sk")["cards"] == [_card(store, "→", "m")]


def test_an_unknown_kind_is_an_error(store):
    assert "error" in _explore(store, "widget", "x")


def test_an_unknown_entity_is_none_in_python(store):
    assert _explore(store, "memory", "nope") is None


def _tool(monkeypatch, store, **kwargs):
    fn = getattr(server, "explore", None)
    assert fn is not None, "server has no explore tool"
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    return json.loads(fn(**kwargs))


def test_the_tool_answers_a_miss_with_the_existing_not_found_shape(store, monkeypatch):
    T.upsert_project(store, "gh:org/p", "P", workspace="W")
    _mem(store, "ws-mem", "b\n", workspace="W")

    elsewhere = _tool(monkeypatch, store, kind="memory", key="ws-mem")
    nowhere = _tool(monkeypatch, store, kind="memory", key="no-such")

    assert isinstance(elsewhere, dict) and 'workspace="W"' in elsewhere.get("error", ""), elsewhere
    assert nowhere is None


def test_the_tool_returns_cards(store, monkeypatch):
    _seed(store)

    out = _tool(monkeypatch, store, kind="memory", key="a", depth=2, budget_bytes=4000)

    assert out["cards"] == _explore(store, "memory", "a", depth=2)["cards"]
