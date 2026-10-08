'The advertised MCP tool surface (context overhaul, Phase 1 item 3).\n\nShared rules live on parameter types, not in every description; fixed value sets are\nenums; the schema carries no pydantic titles or null branches; no tool ships a\nstructuredContent copy; and the routes folded into upsert_memory / upsert_doc refuse\narguments they cannot apply.'
import json

import pytest

from agent_context import fstools as T
from agent_context import server


def _tools():
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}


def _walk(node):
    if isinstance(node, dict):
        yield node
        for k, v in node.items():
            if k == "properties":
                for s in v.values():
                    yield from _walk(s)
            else:
                yield from _walk(v)
    elif isinstance(node, list):
        for n in node:
            yield from _walk(n)


@pytest.fixture
def srv(store, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "_store", store)
    return server


def _call(tool: str, **kw) -> dict:
    return json.loads(getattr(server, tool)(**kw))


def test_no_tool_ships_structured_output() -> None:
    assert [n for n, t in _tools().items() if t.output_schema is not None] == []


def test_the_advertised_schema_has_no_titles_or_null_branches() -> None:
    for name, t in _tools().items():
        for node in _walk(t.parameters):
            assert "title" not in node, name
            assert {"type": "null"} not in node.get("anyOf", []), name


def test_a_parameter_named_title_survives_compaction() -> None:
    assert "title" in _tools()["upsert_doc"].parameters["properties"]
    assert "title" in _tools()["upsert_instruction"].parameters["properties"]


def test_the_workspace_and_links_rules_are_not_repeated_in_descriptions() -> None:
    for name, t in _tools().items():
        desc = t.description or ""
        assert "INSTEAD of `project=`" not in desc, name
        assert "enforced_by, contradicts" not in desc, name


def test_the_workspace_and_links_rules_ride_on_the_parameter() -> None:
    props = _tools()["upsert_skill"].parameters["properties"]
    assert "exclusive" in props["workspace"]["description"]
    assert "depends_on" in props["links"]["description"]


@pytest.mark.parametrize("tool,param,value", [
    ("upsert_memory", "memory_type", "feedback"),
    ("upsert_memory", "load_behavior", "lazy"),
    ("add_audit_observation", "severity", "blocker"),
    ("add_audit_observation", "scope", "universal"),
    ("upsert_hook", "event_type", "PreToolUse"),
    ("upsert_agent_definition", "model", "fable"),
    ("upsert_agent_definition", "effort", "xhigh"),
    ("get_usage_report", "group_by", "sidechain"),
    ("get_usage_report", "report", "tokens"),
    ("list_entities", "kind", "agent_definition"),
])
def test_fixed_value_sets_are_enums(tool: str, param: str, value: str) -> None:
    assert value in _tools()[tool].parameters["properties"][param]["enum"]


def test_upsert_memory_patches_description_and_tier_without_a_body(store, srv) -> None:
    T.upsert_memory(store, "m", "reference", "old", "the body", load_behavior="always")
    out = _call("upsert_memory", slug="m", description="new", load_behavior="lazy")
    assert "error" not in out, out
    got = T.get_memory(store, "m")
    assert (got["description"], got["load_behavior"], got["body"]) == ("new", "lazy", "the body")


@pytest.mark.parametrize("kw", [{}, {"memory_type": "user", "description": "d"},
                                {"links": {"sibling": ["x"]}, "load_behavior": "lazy"}])
def test_upsert_memory_without_a_body_refuses_what_it_cannot_patch(store, srv, kw) -> None:
    T.upsert_memory(store, "m", "reference", "old", "the body", load_behavior="always")
    assert "error" in _call("upsert_memory", slug="m", **kw)
    assert T.get_memory(store, "m")["description"] == "old"


def test_upsert_memory_with_a_body_still_needs_type_and_description(store, srv) -> None:
    assert "error" in _call("upsert_memory", slug="m", body="b", load_behavior="lazy")


def test_upsert_doc_append_adds_to_the_end(store, srv) -> None:
    T.upsert_doc(store, "n.md", body="one", title="N")
    assert "error" not in _call("upsert_doc", path="n.md", body="two", append=True)
    assert T.get_doc(store, "n.md")["body"].rstrip() == "one\ntwo"


@pytest.mark.parametrize("kw", [{"body": "x", "title": "T"}, {}])
def test_upsert_doc_append_refuses_other_fields_and_a_missing_body(store, srv, kw) -> None:
    T.upsert_doc(store, "n.md", body="one", title="N")
    assert "error" in _call("upsert_doc", path="n.md", append=True, **kw)
    assert T.get_doc(store, "n.md")["body"].rstrip() == "one"


def test_set_machine_with_no_field_is_refused(store, srv) -> None:
    assert "error" in _call("set_machine")


def test_set_machine_applies_every_field_passed(store, srv) -> None:
    me = T.get_session_context(store, "/nowhere")["machine"]["machine_uuid"]
    out = _call("set_machine", display_name="Bench", sleeps=True)
    assert "error" not in out, out
    row = next(e for e in store.entities.values()
               if e["type"] == "machine" and e.get("machine_uuid") == me)
    assert row.get("display_name") == "Bench" and row.get("sleeps") is True
