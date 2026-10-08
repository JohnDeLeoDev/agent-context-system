'bulk_edit: many replacements across many entities, counts back instead of bodies.'
from agent_context import fstools as T


def _mk(store):
    store.upsert("doc", "a.md", {"title": "A"},
                 body="The colour is grey. A second colour here.", scope="global")
    store.upsert("doc", "b.md", {"title": "B"},
                 body="No British spelling in this one.", scope="global")
    store.upsert("memory", "m1", {"memory_type": "reference", "description": "d"},
                 body="behaviour and behaviour again", scope="global")
    store.upsert("hook", "h1", {"event_type": "PreToolUse", "description": "d"},
                 body="# honouring the timeout\nexit 0\n", scope="global")


def test_counts_come_back_and_bodies_do_not(store):
    _mk(store)
    r = T.bulk_edit(store, [
        {"kind": "doc", "key": "a.md", "replacements": [["colour", "color"], ["grey", "gray"]]},
        {"kind": "memory", "key": "m1", "replacements": [["behaviour", "behavior"]]},
    ])
    assert r["entities"] == 2
    assert r["entities_changed"] == 2
    assert r["replacements"] == 5          
    blob = repr(r)
    for leaked in ("The colour is", "The color is", "behaviour and", "behavior and"):
        assert leaked not in blob, f"bulk_edit leaked body text: {leaked!r}"


def test_replacements_actually_land(store):
    _mk(store)
    T.bulk_edit(store, [{"kind": "doc", "key": "a.md",
                         "replacements": [["colour", "color"], ["grey", "gray"]]}])
    assert store.get("doc", "a.md")["body"] == "The color is gray. A second color here."


def test_one_write_per_entity_not_per_replacement(store):
    'Three replacements in one doc must be ONE version bump, not three.'
    _mk(store)
    seen = []
    real = store.upsert
    store.upsert = lambda *a, **k: (seen.append(a[1]), real(*a, **k))[1]
    T.bulk_edit(store, [{"kind": "doc", "key": "a.md",
                         "replacements": [["colour", "color"], ["grey", "gray"], ["second", "2nd"]]}])
    store.upsert = real
    assert seen == ["a.md"]


def test_hook_body_uses_script_body_field(store):
    _mk(store)
    r = T.bulk_edit(store, [{"kind": "hook", "key": "h1",
                             "replacements": [["honouring", "honoring"]]}])
    assert r["replacements"] == 1
    assert "honoring" in store.get("hook", "h1")["script_body"]


def test_no_match_is_zero_not_an_error(store):
    'A sweep runs the same list over many entities; most will not match, and that\n    is a normal outcome, not a failure.'
    _mk(store)
    r = T.bulk_edit(store, [{"kind": "doc", "key": "b.md", "replacements": [["colour", "color"]]}])
    assert r["replacements"] == 0
    assert r["entities_changed"] == 0
    assert "error" not in r["results"][0]


def test_dry_run_writes_nothing(store):
    _mk(store)
    before = store.get("doc", "a.md")["body"]
    r = T.bulk_edit(store, [{"kind": "doc", "key": "a.md",
                             "replacements": [["colour", "color"]]}], dry_run=True)
    assert r["replacements"] == 2
    assert r["dry_run"] is True
    assert store.get("doc", "a.md")["body"] == before


def test_require_unique_refuses_an_ambiguous_match(store):
    'edit_body semantics on demand: 2 matches is an error and the entity is untouched.'
    _mk(store)
    before = store.get("doc", "a.md")["body"]
    r = T.bulk_edit(store, [{"kind": "doc", "key": "a.md",
                             "replacements": [["colour", "color"]]}], require_unique=True)
    assert r["results"][0]["applied"] == 0
    assert "not unique" in r["results"][0]["error"]
    assert store.get("doc", "a.md")["body"] == before


def test_a_failed_entity_does_not_stop_the_others(store):
    _mk(store)
    r = T.bulk_edit(store, [
        {"kind": "doc", "key": "does-not-exist.md", "replacements": [["a", "b"]]},
        {"kind": "doc", "key": "a.md", "replacements": [["grey", "gray"]]},
    ])
    assert "not found" in r["results"][0]["error"]
    assert r["results"][1]["applied"] == 1
    assert "gray" in store.get("doc", "a.md")["body"]


def test_unknown_kind_is_reported_per_entity(store):
    r = T.bulk_edit(store, [{"kind": "nonsense", "key": "x", "replacements": [["a", "b"]]}])
    assert "unknown kind" in r["results"][0]["error"]


def test_empty_edit_list_is_a_no_op(store):
    r = T.bulk_edit(store, [])
    assert r == {"entities": 0, "entities_changed": 0, "replacements": 0,
                 "dry_run": False, "require_unique": False, "results": []}
