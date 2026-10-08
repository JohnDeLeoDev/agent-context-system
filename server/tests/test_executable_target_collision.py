'Same-named scripts and hooks require an explicit kind in typed links.'

from agent_context import fstools as T
from agent_context import graph


def test_qualified_hook_and_script_targets_choose_the_requested_kind(store):
    T.upsert_script(store, "shared", "echo worker\n", description="Worker")
    T.upsert_hook(store, "shared", "PreToolUse", "exit 0\n", description="Gate")
    T.upsert_skill(store, "procedure", "Procedure", "Run it.\n")

    hook = T.set_entity_links(store, "skill", "procedure", {"enforced_by": ["hook:shared"]})
    assert "warning" not in hook, hook
    explored = graph.explore(store, "skill", "procedure")
    assert explored is not None
    assert any("enforced_by hook shared" in card for card in explored["cards"])

    script = T.set_entity_links(store, "skill", "procedure", {"enforced_by": ["script:shared"]})
    assert "warning" not in script, script
    explored = graph.explore(store, "skill", "procedure")
    assert explored is not None
    assert any("enforced_by script shared" in card for card in explored["cards"])


def test_unqualified_executable_collision_reports_ambiguity_without_an_edge(store):
    T.upsert_script(store, "shared", "echo worker\n", description="Worker")
    T.upsert_hook(store, "shared", "PreToolUse", "exit 0\n", description="Gate")
    T.upsert_skill(store, "procedure", "Procedure", "Run it.\n")

    result = T.set_entity_links(store, "skill", "procedure", {"enforced_by": ["shared"]})

    assert "shared" in result.get("warning", "")
    explored = graph.explore(store, "skill", "procedure")
    assert explored is not None
    assert not any("enforced_by" in card for card in explored["cards"])
    findings = graph.ambiguous_links(store)
    assert len(findings) == 1
    assert findings[0]["matches"] == ["hook shared", "script shared"]
