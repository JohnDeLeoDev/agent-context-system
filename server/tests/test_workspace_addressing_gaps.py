'The doc and memory delete and append helpers, and list_entities, took only `project`, so\na `ws:` entity could be read and edited by `workspace=` and not appended to, deleted\nthrough them, or listed on its own. delete_entity refused a bad workspace even for a\nproject, whose address is its name alone.'
import pytest

from agent_context import fstools as T
from agent_context import generic


def _ws(store):
    T.upsert_project(store, "gh:org/p", "P", workspace="W")


def test_append_to_doc_reaches_a_workspace_doc(store):
    _ws(store)
    T.upsert_doc(store, "ws.md", "one\n", workspace="W")
    out = T.append_to_doc(store, "ws.md", "two", workspace="W")
    assert "error" not in out, out
    assert store.get("doc", "ws.md", scope="ws:W")["body"].split() == ["one", "two"]


def test_delete_doc_and_memory_reach_the_workspace_scope(store):
    _ws(store)
    T.upsert_doc(store, "ws.md", "x\n", workspace="W")
    T.upsert_memory(store, "ws-m", "project", "d", "b\n", workspace="W")
    T.delete_doc(store, "ws.md", workspace="W")
    T.delete_memory(store, "ws-m", workspace="W")
    assert store.get("doc", "ws.md", scope="ws:W") is None
    assert store.get("memory", "ws-m", scope="ws:W") is None


def test_list_entities_lists_a_workspace_without_a_project(store):
    _ws(store)
    T.upsert_doc(store, "ws.md", "x\n", workspace="W")
    rows = generic.list_entities(store, "doc", workspace="W")
    assert "ws.md" in [r["path"] for r in rows]
    assert "ws.md" not in [r["path"] for r in generic.list_entities(store, "doc")]


def test_list_entities_refuses_an_unknown_workspace(store):
    _ws(store)
    with pytest.raises(ValueError, match="unknown workspace"):
        generic.list_entities(store, "doc", workspace="NSYTA")


def test_deleting_a_project_ignores_the_workspace_argument(store):
    _ws(store)
    out = generic.delete_entity(store, "project", "P", workspace="typo")
    assert not (isinstance(out, dict) and "unknown workspace" in str(out.get("error", ""))), out
