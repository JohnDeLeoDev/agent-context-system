'Body-free typed links on commands and instructions.'

from agent_context import fstools as T


def test_set_entity_links_keeps_command_body_and_adds_relation(store):
    T.upsert_memory(store, "target", "reference", "target", "target body")
    T.upsert_command(store, "deploy", body="deploy body", description="deploy")

    result = T.set_entity_links(store, "command", "deploy", {"depends_on": ["target"]})

    assert "error" not in result, result
    assert T.get_command(store, "deploy")["body"] == "deploy body"
    assert any("→ depends_on memory target" in card for card in result["links"])


def test_set_entity_links_keeps_instruction_body_and_explores_relation(store):
    T.upsert_memory(store, "target", "reference", "target", "target body")
    T.upsert_instruction(store, "Policy", "instruction body")

    result = T.set_entity_links(store, "instruction", "Policy", {"enforced_by": ["target"]})

    assert "error" not in result, result
    instruction = store.get("instruction", "Policy")
    assert instruction["body"] == "instruction body"
    explored = T.explore(store, "instruction", "Policy")
    assert any("→ enforced_by memory target" in card for card in explored["cards"])
