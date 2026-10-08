'The MCP server module itself: entrypoint present, exact tool roster.\n\npy_compile and the fstools suite passed a build 19 whose server.py had lost `main()`\nand the `__main__` guard to a bad splice — a daemon re-exec\'ing into it would have\nexited 0 and every supervisor treats exit 0 as "done". This pins what those cannot see.'
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from agent_context import server as S

EXPECTED = {
    
    
    
    
    
    "add_audit_observation", "check_integrity", "get_doc", "get_health",
    "get_instructions", "get_memory", "get_session_context",
    "get_usage_report", "get_version_history", "list_audit_observations", "list_machines",
    
    "set_machine", "register_path",
    "resolve_audit_observation", "resolve_project", "search_all",
    
    
    "get_materialized", "relay_report",
    
    "run_store_task",
    "update_audit_observation", "upsert_agent_definition", "upsert_command",
    "upsert_doc", "upsert_hook",
    "upsert_instruction", "upsert_memory", "upsert_project", "upsert_script", "upsert_skill",
    "get_entity", "list_entities", "delete_entity", "edit_body", "bulk_edit",
    
    "explore",
    
    
    
    "set_entity_links",
    
    
    "list_agents", "send_message", "read_notifications",
}


def _registered():
    tm = getattr(S.mcp, "_tool_manager", None)
    if tm is not None and hasattr(tm, "_tools"):
        return set(tm._tools)
    src = open(S.__file__, encoding="utf-8").read()
    import re
    return set(re.findall(r"@mcp\.tool\(structured_output=False\)\n(?:@\w+\n)*def (\w+)\(", src))


def test_entrypoint_survives():
    assert callable(getattr(S, "main", None))
    assert 'if __name__ == "__main__"' in open(S.__file__, encoding="utf-8").read()


def test_tool_roster_is_exactly_the_documented_set():
    got = _registered()
    assert got == EXPECTED, f"missing={sorted(EXPECTED - got)} extra={sorted(got - EXPECTED)}"
