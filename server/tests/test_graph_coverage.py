'Structural graph coverage stays visible before the usage window matures.'

from agent_context import fstools as T
from agent_context import graph


def test_coverage_separates_structural_isolation_from_usage_orphans(store):
    T.upsert_memory(store, "lonely", "reference", "Lonely", "No links.\n")
    T.upsert_memory(store, "linked", "reference", "Linked", "See [[lonely]].\n")
    T.upsert_doc(store, "archive/old.md", "Historical note.\n", title="Old")

    report = graph.coverage(store)

    assert report["nodes"] == 3
    assert report["components"] == 2
    assert report["isolated_active"] == 0
    assert report["isolated_archived"] == 1
    assert report["zero_inbound_active"] == 1


def test_scope_root_connects_active_content_without_mutating_its_body(store):
    T.upsert_doc(store, "agent-context-store.md", "Store root.\n", title="Store")
    T.upsert_memory(store, "entry", "reference", "Entry", "Body unchanged.\n")
    T.upsert_doc(store, "archive/old.md", "Historical note.\n", title="Old")

    entry = store.get("memory", "entry")
    root = store.get("doc", "agent-context-store.md")
    g = graph.graph_for(store)

    assert root["uuid"] in g.out[entry["uuid"]]
    assert "in_scope" in g.rels[(entry["uuid"], root["uuid"])]
    assert entry["body"] == "Body unchanged."
    assert graph.coverage(store)["isolated_archived"] == 1


def test_hook_and_script_are_traversable_nodes(store):
    T.upsert_doc(store, "agent-context-store.md", "Store root.\n", title="Store")
    T.upsert_script(store, "worker", "echo ready\n", description="Worker")
    T.upsert_hook(store, "on-start", "SessionStart", "echo ready\n", description="Start")

    hook = graph.explore(store, "hook", "on-start")
    script = graph.explore(store, "script", "worker")
    assert hook is not None and hook["cards"]
    assert script is not None and script["cards"]
    report = graph.coverage(store)
    assert report["by_kind"]["hook"]["nodes"] == 1
    assert report["by_kind"]["script"]["nodes"] == 1
