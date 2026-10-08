'A collision is distinct from a target that does not exist.'

from agent_context import fstools as T


def test_write_receipt_identifies_script_hook_collision_as_ambiguous(store):
    T.upsert_script(store, "shared", "echo worker\n", description="Worker")
    T.upsert_hook(store, "shared", "PreToolUse", "exit 0\n", description="Gate")
    T.upsert_skill(store, "procedure", "Procedure", "Run it.\n")

    result = T.set_entity_links(store, "skill", "procedure", {"enforced_by": ["shared"]})

    assert "ambiguous" in result.get("warning", "").lower()
    assert "shared" in result["warning"]
    assert "hook:shared" in result["warning"]
