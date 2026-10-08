'search_all keeps its memory and doc rows as store.search returns them (same types, limit,\norder and bytes) and appends up to 3 skills, commands, hooks and scripts after them, each\nrow marked "group": "other". The other kinds match on name and description only: skill\nand command bodies are long procedures, hook and script bodies are code.\n\nCriteria: get_doc("context-graph/plan.md"), "Phase 4" and "T6 results". Mixing those\nkinds into one ranking pushed memory and doc labels out of the top 5 on the benchmark,\nwhich is why they are a separate group after the main rows.'
import json

import pytest

from agent_context import fstools as T
from agent_context import graph, usage

NEW_KINDS = ("skill", "command", "hook", "script")
ROW_FIELDS = {"entity_type", "scope", "name", "description", "snippet", "rank"}


@pytest.fixture
def counters(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})
    monkeypatch.setattr(usage, "_FLUSH_SECS", 0.0)
    return usage


def _make(store, kind, name, description, body="echo plain\n"):
    if kind == "skill":
        return T.upsert_skill(store, name, description, body)
    if kind == "command":
        return T.upsert_command(store, name, body, description=description)
    if kind == "hook":
        return T.upsert_hook(store, name, event_type="PreToolUse", script_body=body,
                             description=description)
    return T.upsert_script(store, name, script_body=body, description=description)


def _make_unguarded(store, kind, name, description, body="echo plain\n"):
    'Write a script or hook below the description limit, as one stored before it.'
    fields = {"language": "sh", "origin": "user", "description": description}
    if kind == "hook":
        fields.update(event_type="PreToolUse", timeout_seconds=30)
    store.upsert(kind, name, fields, body=body)


def _other(rows):
    return [r for r in rows if r.get("group") == "other"]


def test_memory_and_doc_rows_are_what_store_search_returns(store):
    T.upsert_memory(store, "heron-note", "reference", "heron facts", "a heron wades")
    T.upsert_memory(store, "notes", "reference", "unrelated", "one heron mention")
    T.upsert_doc(store, "birds.md", "the heron page", title="birds")
    for kind in NEW_KINDS:
        _make(store, kind, f"heron-{kind}", "heron words")

    for limit in (1, 2, 20):
        expected = store.search("heron", types=("memory", "doc"), limit=limit)
        rows = T.search_all(store, "heron", limit=limit)
        assert json.dumps(rows[:len(expected)]) == json.dumps(expected)
        assert all("group" not in r for r in rows[:len(expected)])
        assert _other(rows), "no other-kind rows were appended"


def test_other_rows_come_after_the_main_rows_and_stop_at_three(store):
    T.upsert_memory(store, "pelican-note", "reference", "pelican facts", "plain")
    for n in range(5):
        _make(store, "skill", f"pelican-skill-{n}", "unrelated words")

    rows = T.search_all(store, "pelican")

    assert [r["name"] for r in rows[:1]] == ["pelican-note"]
    assert len(rows) == 4
    assert [r["group"] for r in rows[1:]] == ["other"] * 3


def test_other_rows_are_the_best_three_by_name_and_description(store):
    _make(store, "skill", "alpha", "egret heron wading")
    _make(store, "command", "beta", "egret heron")
    _make(store, "hook", "gamma", "egret")
    _make(store, "script", "delta", "heron")
    _make(store, "skill", "epsilon", "unrelated", "egret heron egret heron\n")

    names = [r["name"] for r in T.search_all(store, "egret heron")]

    assert len(names) == 3
    assert set(names[:2]) == {"alpha", "beta"}
    assert names[2] in {"gamma", "delta"}


def test_no_other_rows_when_no_other_kind_scores(store):
    T.upsert_memory(store, "osprey-note", "reference", "osprey facts", "plain")
    _make(store, "skill", "unrelated-skill", "nothing here", "osprey osprey osprey\n")
    _make(store, "hook", "unrelated-hook", "nothing here", "echo osprey\n")

    assert [r["name"] for r in T.search_all(store, "osprey")] == ["osprey-note"]
    assert _other(T.search_all(store, "osprey")) == []
    assert T.search_all(store, "zzzznomatch") == []


def test_other_rows_alone_when_no_memory_or_doc_matches(store):
    for kind in NEW_KINDS:
        _make(store, kind, f"wren-{kind}", "unrelated words")

    rows = T.search_all(store, "wren")

    assert len(rows) == 3
    assert all(r["group"] == "other" and r["entity_type"] in NEW_KINDS for r in rows)


def test_an_other_row_carries_only_kind_name_description_and_group(store):
    T.upsert_memory(store, "kestrel-note", "reference", "kestrel facts", "plain")
    _make(store, "script", "kestrel-script", "unrelated words")

    rows = T.search_all(store, "kestrel")

    assert len(rows) == 2
    assert set(rows[0]) == ROW_FIELDS
    assert rows[1] == {"entity_type": "script", "name": "kestrel-script",
                       "description": "unrelated words", "group": "other"}


def test_a_non_global_other_row_carries_its_fetch_hint(store):
    T.upsert_project(store, "gh:org/p", "P", workspace="W")
    T.upsert_script(store, "heron-project", script_body="echo\n", description="heron words",
                    project="P")
    T.upsert_hook(store, "heron-workspace", event_type="PreToolUse", script_body="echo\n",
                  description="heron words", workspace="W")

    rows = {r["name"]: r for r in T.search_all(store, "heron")}

    assert rows["heron-project"] == {"entity_type": "script", "name": "heron-project",
                                     "description": "heron words", "project": "P",
                                     "group": "other"}
    assert rows["heron-workspace"] == {"entity_type": "hook", "name": "heron-workspace",
                                       "description": "heron words", "workspace": "W",
                                       "group": "other"}


def test_an_other_row_description_is_cut_to_100_characters_like_a_card(store):
    long = "falcon " + "word " * 40
    family = "x" * 97 + "\U0001F469‍\U0001F469‍\U0001F467" + " tail" * 20
    short = "falcon " + "y" * 60
    _make_unguarded(store, "script", "falcon-long", long)
    _make_unguarded(store, "hook", "falcon-family", family.replace("x" * 7, "falcon ", 1))
    _make(store, "command", "falcon-short", short)

    got = {r["name"]: r["description"] for r in T.search_all(store, "falcon")}

    def card_cut(text):
        text = " ".join(text.split())
        return text if len(text) <= 100 else graph._cut(text, 99) + "…"

    assert got["falcon-long"] == card_cut(long)
    assert len(got["falcon-long"]) == 100 and got["falcon-long"].endswith("…")
    assert got["falcon-family"] == card_cut(family.replace("x" * 7, "falcon ", 1))
    assert not got["falcon-family"][:-1].endswith("‍")
    assert got["falcon-short"] == short


def test_limit_caps_the_main_rows_not_the_other_group(store):
    T.upsert_memory(store, "plover-a", "reference", "plover one", "plain")
    T.upsert_memory(store, "plover-b", "reference", "plover two", "plain")
    _make(store, "skill", "plover-skill", "unrelated words")
    _make(store, "command", "plover-command", "unrelated words")

    rows = T.search_all(store, "plover", limit=1)

    assert len([r for r in rows if "group" not in r]) == 1
    assert {r["name"] for r in _other(rows)} == {"plover-skill", "plover-command"}


def test_search_all_counts_a_hit_for_each_other_row(store, counters):
    for kind in NEW_KINDS:
        _make(store, kind, f"swift-{kind}", "unrelated words")

    returned = {r["name"] for r in _other(T.search_all(store, "swift"))}

    assert len(returned) == 3
    for kind in NEW_KINDS:
        name = f"swift-{kind}"
        assert counters.stats(kind, "global", name)["hits"] == (1 if name in returned else 0)


def test_search_memories_and_search_docs_stay_on_their_own_kind(store):
    T.upsert_memory(store, "stork-note", "reference", "stork facts", "plain")
    T.upsert_doc(store, "stork.md", "plain", title="stork page")
    for kind in NEW_KINDS:
        _make(store, kind, f"stork-{kind}", "stork words")

    mem = T.search_memories(store, "stork")
    docs = T.search_docs(store, "stork")

    assert [r["slug"] for r in mem] == ["stork-note"]
    assert [r["path"] for r in docs] == ["stork.md"]
    assert all("group" not in r for r in mem + docs)
