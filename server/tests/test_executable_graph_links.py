'Explicit executable relationships without treating source code as prose.'

from pathlib import Path

from agent_context import fstools as T
from agent_context import graph


def _body_bytes(store, kind, key):
    entity = store.get(kind, key)
    assert entity is not None
    return Path(entity["_path"]).read_bytes()


def test_script_relations_are_explicit_and_preserve_executable_body(store):
    T.upsert_script(store, "worker", "# [[target]]\necho ready\n", description="Worker")
    T.upsert_memory(store, "target", "reference", "Target", "Target body.\n")
    script = store.get("script", "worker")
    target = store.get("memory", "target")
    assert script is not None and target is not None
    assert target["uuid"] not in graph.graph_for(store).out.get(script["uuid"], [])
    before = _body_bytes(store, "script", "worker")

    result = T.set_entity_links(store, "script", "worker", {"sibling": ["target"]})

    assert "error" not in result, result
    assert _body_bytes(store, "script", "worker") == before
    explored = graph.explore(store, "script", "worker")
    assert explored is not None
    assert any("sibling memory target" in card for card in explored["cards"])


def test_hook_is_a_target_and_an_explicit_source(store):
    T.upsert_hook(store, "gate", "PreToolUse", "# [[worker]]\nexit 0\n", description="Gate")
    T.upsert_script(store, "worker", "echo ready\n", description="Worker")
    T.upsert_skill(store, "procedure", "Procedure", "Run it.\n")
    before = _body_bytes(store, "hook", "gate")

    source = T.set_entity_links(store, "hook", "gate", {"sibling": ["worker"]})
    target = T.set_entity_links(store, "skill", "procedure", {"enforced_by": ["hook:gate"]})

    assert "error" not in source and "error" not in target, (source, target)
    assert "warning" not in target, target
    assert _body_bytes(store, "hook", "gate") == before
    hook_cards = graph.explore(store, "hook", "gate")
    skill_cards = graph.explore(store, "skill", "procedure")
    assert hook_cards is not None and skill_cards is not None
    assert any("sibling script worker" in card for card in hook_cards["cards"])
    assert any("enforced_by hook gate" in card for card in skill_cards["cards"])


def test_unresolved_executable_relation_warns_without_an_edge(store):
    T.upsert_hook(store, "gate", "PreToolUse", "exit 0\n", description="Gate")

    result = T.set_entity_links(store, "hook", "gate", {"enforced_by": ["missing-worker"]})

    assert "error" not in result, result
    assert "missing-worker" in result.get("warning", "")
    explored = graph.explore(store, "hook", "gate")
    assert explored is not None
    assert not any("enforced_by" in card for card in explored["cards"])


def test_coverage_separates_prose_gaps_from_executable_gaps(store):
    T.upsert_doc(store, "agent-context-store.md", "Root.\n", title="Root")
    T.upsert_memory(store, "note", "reference", "Note", "Standalone.\n")
    T.upsert_script(store, "worker", "# [[note]]\necho ready\n", description="Worker")
    T.upsert_hook(store, "gate", "PreToolUse", "exit 0\n", description="Gate")

    report = graph.coverage(store)

    assert report["semantic_isolated_executable"] == 2
    assert report["semantic_isolated_prose"] == 2
    assert report["semantic_isolated_active"] == 4
    assert sorted(report["semantic_isolated_prose_items"], key=lambda row: row["key"]) == [
        {"type": "doc", "scope": "global", "key": "agent-context-store.md"},
        {"type": "memory", "scope": "global", "key": "note"}]
