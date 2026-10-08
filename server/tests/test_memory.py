'upsert_memory cross-scope warning + memory_index serialization shape.'
from agent_context import fstools as T
from agent_context import projects as P


def test_cross_scope_warning_on_duplicate_slug(store):
    T.upsert_project(store, "gh:org/repo", "P")
    T.upsert_memory(store, "foo", "reference", "d", "body-global", project=None)
    r = T.upsert_memory(store, "foo", "reference", "d", "body-proj", project="P")
    assert "warning" in r
    assert "foo" in r["warning"]
    assert "global" in r["warning"]


def test_no_warning_for_unique_slug(store):
    r = T.upsert_memory(store, "bar", "reference", "d", "b", project=None)
    assert "warning" not in r


def test_no_warning_when_same_scope_updated(store):
    T.upsert_memory(store, "baz", "reference", "d", "v1", project=None)
    r = T.upsert_memory(store, "baz", "reference", "d", "v2", project=None)
    assert "warning" not in r


def test_memory_index_is_text_rows_not_json_objects(store):
    T.upsert_memory(store, "note", "reference", "a description", "body text", project=None,
                     load_behavior="always")
    ctx = T.get_session_context(store, "/no/project/here")
    idx = ctx["memory_index"]
    
    assert idx["global"]["rows"] == "r note — a description"
    assert "body text" not in idx["global"]["rows"]
    assert "project" not in idx  


def test_memory_index_splits_global_and_project_blocks(store, monkeypatch):
    T.upsert_project(store, "gh:org/repo", "P")
    T.upsert_memory(store, "gnote", "reference", "gd", "gbody", project=None,
                     load_behavior="always")
    T.upsert_memory(store, "pnote", "project", "pd", "pbody", project="P",
                     load_behavior="always")
    monkeypatch.setattr(P, "resolve_project", lambda s, cwd: {"display_name": "P"})
    idx = T.get_session_context(store, "/anywhere")["memory_index"]
    
    assert idx["global"]["rows"] == "r gnote — gd"
    assert idx["project"]["rows"] == "p pnote — pd"


def test_memory_row_encoding_beats_json_objects(store):
    'The whole point of the text encoding: framing cost per row is ~4 chars, not ~46.'
    for i in range(20):
        T.upsert_memory(store, f"slug-{i}", "project", "d" * 80, "b", project=None)
    rows = T.get_session_context(store, "/no/project")["memory_index"]["global"]["rows"]
    overhead = len(rows) - sum(len(f"slug-{i}") + 80 for i in range(20))
    assert overhead / 20 < 10  


def test_overlong_description_is_refused_everywhere_it_can_be_written(store):
    r = T.upsert_memory(store, "fat", "reference", "x" * 141, "b", project=None)
    assert "error" in r and "141" in r["error"]
    assert T.get_memory(store, "fat") is None
    T.upsert_memory(store, "ok", "reference", "x" * 140, "b", project=None)
    r = T.set_memory_description(store, "ok", "y" * 141)
    assert "error" in r
    assert T.get_memory(store, "ok")["description"] == "x" * 140


def test_credential_literal_is_refused_in_a_body(store):
    tok = "ghp_" + "A" * 30
    r = T.upsert_memory(store, "leak", "reference", "d", f"token {tok}", project=None)
    assert "error" in r and "credential" in r["error"]
    assert T.get_memory(store, "leak") is None
    T.upsert_memory(store, "clean", "reference", "d", "see 1Password item X", project=None)
    r = T.edit_memory_body(store, "clean", "item X", f"item {tok}")
    assert "error" in r
    assert "ghp_" not in T.get_memory(store, "clean")["body"]


def test_upsert_memory_keeps_an_existing_lazy_tier(store):
    'test upsert memory keeps an existing lazy tier.'
    T.upsert_memory(store, "narrow", "reference", "d", "v1", project=None)
    T.set_memory_load_behavior(store, "narrow", "lazy")
    T.upsert_memory(store, "narrow", "reference", "d", "v2", project=None)
    assert T.get_memory(store, "narrow")["load_behavior"] == "lazy"
    
    T.upsert_memory(store, "narrow", "reference", "d", "v3", project=None, load_behavior="always")
    assert T.get_memory(store, "narrow")["load_behavior"] == "always"
