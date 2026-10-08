'A workspace or project name is a directory name, and nothing may leave the store with it.'
import os

import pytest

from agent_context import fstools as T


@pytest.mark.parametrize("ws", ["../esc", "../../x", "a/b", ".hidden", "global"])
def test_a_project_naming_an_unsafe_workspace_is_refused(store, ws):
    out = T.upsert_project(store, "gh:org/p", "P", workspace=ws)
    assert "error" in out and "workspace" in out["error"], out
    assert not store.project_entity("P")


@pytest.mark.parametrize("name", ["../esc", "a/b", ".hidden"])
def test_an_unsafe_project_name_is_refused(store, name):
    out = T.upsert_project(store, "gh:org/p", name)
    assert "error" in out, out
    assert not os.path.exists(os.path.join(store.root, "esc"))


def test_the_reviewed_repro_writes_nothing_outside_workspaces(store):
    T.upsert_project(store, "gh:org/p", "P", workspace="../esc")
    with pytest.raises(ValueError, match="refused"):
        store.scope_for_write(None, "../esc")
    assert not os.path.exists(os.path.join(store.root, "esc"))


def test_an_unsafe_name_in_an_existing_record_is_not_a_workspace(store):
    'A record written before this rule still names the bad workspace; it is ignored.'
    T.upsert_project(store, "gh:org/p", "P", workspace="W")
    store.project_entity("P")["workspace"] = "../esc"
    assert "../esc" not in store.workspaces()


def test_a_safe_workspace_still_works(store):
    T.upsert_project(store, "gh:org/p", "P", workspace="example-workspace")
    assert store.scope_for_write(None, "example-workspace") == "ws:example-workspace"
