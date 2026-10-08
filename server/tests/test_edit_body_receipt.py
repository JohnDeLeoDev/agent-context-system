'edit_body returns a receipt, not the whole entity.\n\nMeasured on one machine over three days: edit_body was the third most expensive\ntool by amortized context (2.9%), 216 calls carrying 738,605 tokens — roughly\n3,500 tokens to confirm a one-line patch, re-paid as a cache-read on every later\nrequest of the session. bulk_edit already returns counts only and exists for this\nreason; a single edit had no equivalent.\n\nThe body is DROPPED, never truncated: a half-body invites reasoning over a\nfragment that reads as if it were whole.'
import json

from agent_context import server


def _call(monkeypatch, returned, kind="hook", key="some-hook"):
    'Drive the MCP tool with the storage layer stubbed out.'
    monkeypatch.setattr(server, "_get_conn", lambda: object())
    monkeypatch.setattr(server.T, "edit_body",
                        lambda *a, **k: returned)
    return json.loads(server.edit_body(kind, key, "old", "new"))


def test_body_is_omitted_and_its_size_reported(monkeypatch):
    body = "x" * 4321
    out = _call(monkeypatch, {"name": "some-doc", "scope": "global", "body": body},
                kind="doc", key="some-doc")
    assert "body" not in out, "the full body was echoed back"
    assert "4321 chars" in out["body_omitted"]
    assert out["name"] == "some-doc", "the receipt must still identify what changed"
    assert out["scope"] == "global"


def test_a_script_body_is_omitted_too(monkeypatch):
    'Scripts and hooks carry their body in `script_body`, not `body`.\n\n    The first version of this receipt hard-coded "body" and so did nothing at all\n    for the two kinds with the longest bodies: editing a 950-line materializer\n    echoed the whole file back on every call.'
    body = "#!/usr/bin/env python3\n" + "x" * 5000
    for kind in ("script", "hook"):
        out = _call(monkeypatch, {"name": "harness-materialize", "script_body": body},
                    kind=kind, key="harness-materialize")
        assert "script_body" not in out, f"{kind} echoed its whole body back"
        assert f"{len(body)} chars" in out["body_omitted"]
        assert out["name"] == "harness-materialize"


def test_the_wrong_field_for_the_kind_is_left_alone(monkeypatch):
    'A doc result carrying `script_body` is not this receipt\'s business.\n\n    Guards the fix against being written as "pop whatever looks like a body":\n    the field is chosen by KIND, from the same table generic.edit_body dispatches\n    on, so the two cannot drift apart.'
    out = _call(monkeypatch, {"name": "some-doc", "script_body": "x" * 99},
                kind="doc", key="some-doc")
    assert out["script_body"] == "x" * 99
    assert "body_omitted" not in out


def test_no_body_key_is_left_alone(monkeypatch):
    out = _call(monkeypatch, {"name": "some-hook", "replacements": 1})
    assert out == {"name": "some-hook", "replacements": 1}


def test_an_error_result_passes_through_untouched(monkeypatch):
    'A refusal carries no body and must not grow a misleading receipt line.'
    out = _call(monkeypatch, {"error": "old_string not found in hook 'some-hook'"})
    assert out == {"error": "old_string not found in hook 'some-hook'"}
    assert "body_omitted" not in out


def test_a_non_dict_result_is_not_mangled(monkeypatch):
    out = _call(monkeypatch, ["not", "a", "dict"])
    assert out == ["not", "a", "dict"]











def _call_tool(monkeypatch, tool_name, t_attr, returned, kwargs):
    'Drive a server.<tool_name> MCP tool with agent_context.T.<t_attr> stubbed out.'
    monkeypatch.setattr(server, "_get_conn", lambda: object())
    monkeypatch.setattr(server.T, t_attr, lambda *a, **k: returned)
    tool = getattr(server, tool_name)
    return json.loads(tool(**kwargs))


def test_upsert_memory_omits_body(monkeypatch):
    body = "y" * 777
    out = _call_tool(monkeypatch, "upsert_memory", "upsert_memory",
                     {"slug": "some-memory", "body": body},
                     {"slug": "some-memory", "memory_type": "project",
                      "description": "d", "body": body})
    assert "body" not in out, "upsert_memory echoed the full body back"
    assert f"{len(body)} chars" in out["body_omitted"]
    assert out["slug"] == "some-memory"


def test_upsert_doc_omits_body(monkeypatch):
    body = "z" * 321
    out = _call_tool(monkeypatch, "upsert_doc", "upsert_doc",
                     {"path": "some-doc.md", "body": body},
                     {"path": "some-doc.md", "body": body})
    assert "body" not in out, "upsert_doc echoed the full body back"
    assert f"{len(body)} chars" in out["body_omitted"]


def test_upsert_skill_omits_body(monkeypatch):
    body = "a" * 654
    out = _call_tool(monkeypatch, "upsert_skill", "upsert_skill",
                     {"name": "some-skill", "body": body},
                     {"name": "some-skill", "body": body})
    assert "body" not in out, "upsert_skill echoed the full body back"
    assert f"{len(body)} chars" in out["body_omitted"]


def test_upsert_command_omits_body(monkeypatch):
    body = "b" * 111
    out = _call_tool(monkeypatch, "upsert_command", "upsert_command",
                     {"name": "some-command", "body": body},
                     {"name": "some-command", "body": body})
    assert "body" not in out, "upsert_command echoed the full body back"
    assert f"{len(body)} chars" in out["body_omitted"]


def test_upsert_agent_definition_omits_body(monkeypatch):
    body = "c" * 888
    out = _call_tool(monkeypatch, "upsert_agent_definition", "upsert_agent_definition",
                     {"name": "some-agent", "body": body},
                     {"name": "some-agent", "body": body})
    assert "body" not in out, "upsert_agent_definition echoed the full body back"
    assert f"{len(body)} chars" in out["body_omitted"]


def test_upsert_hook_omits_script_body(monkeypatch):
    'Hooks carry their body in `script_body`, the exact field the original\n    edit_body fix hard-coded past — this is the case that matters.'
    body = "#!/usr/bin/env bash\n" + "d" * 2000
    out = _call_tool(monkeypatch, "upsert_hook", "upsert_hook",
                     {"name": "some-hook", "script_body": body},
                     {"name": "some-hook", "event_type": "PreToolUse", "script_body": body})
    assert "script_body" not in out, "upsert_hook echoed its whole script_body back"
    assert f"{len(body)} chars" in out["body_omitted"]


def test_upsert_script_omits_script_body(monkeypatch):
    'Scripts carry their body in `script_body` too — same bug class as hooks.'
    body = "#!/usr/bin/env python3\n" + "e" * 2000
    out = _call_tool(monkeypatch, "upsert_script", "upsert_script",
                     {"name": "some-script", "script_body": body},
                     {"name": "some-script", "script_body": body})
    assert "script_body" not in out, "upsert_script echoed its whole script_body back"
    assert f"{len(body)} chars" in out["body_omitted"]


def test_upsert_memory_description_patch_omits_body(monkeypatch):
    'A description-only upsert_memory changes only metadata but the old code echoed\n    the whole (unchanged) body back with it.'
    body = "f" * 432
    out = _call_tool(monkeypatch, "upsert_memory", "set_memory_description",
                     {"slug": "some-memory", "body": body, "description": "new desc"},
                     {"slug": "some-memory", "description": "new desc"})
    assert "body" not in out
    assert f"{len(body)} chars" in out["body_omitted"]


def test_upsert_memory_load_behavior_patch_omits_body(monkeypatch):
    body = "g" * 210
    out = _call_tool(monkeypatch, "upsert_memory", "set_memory_load_behavior",
                     {"slug": "some-memory", "body": body, "load_behavior": "lazy"},
                     {"slug": "some-memory", "load_behavior": "lazy"})
    assert "body" not in out
    assert f"{len(body)} chars" in out["body_omitted"]


def test_upsert_doc_body_path_branch_gains_no_spurious_receipt(monkeypatch):
    "When body_path is used, the underlying store already omits `body` from the\n    result (it read the content off disk, never through the conversation).\n    _no_body's absent-field check must be a no-op here, not manufacture a\n    body_omitted line for content that was never echoed."
    out = _call_tool(monkeypatch, "upsert_doc", "upsert_doc",
                     {"path": "big.md", "scope": "global", "keywords": ["k"],
                      "description": "Read when testing."},
                     {"path": "big.md", "body_path": "~/big.md"})
    
    assert out == {"path": "big.md", "scope": "global", "keywords": ["k"],
                   "description": "Read when testing."}
    assert "body_omitted" not in out








def test_get_memory_still_returns_full_body(monkeypatch):
    body = "h" * 999
    monkeypatch.setattr(server, "_get_conn", lambda: object())
    monkeypatch.setattr(server.T, "get_memory",
                        lambda *a, **k: {"slug": "some-memory", "body": body})
    out = json.loads(server.get_memory(slug="some-memory"))
    assert out["body"] == body
    assert "body_omitted" not in out


def test_get_doc_still_returns_full_body(monkeypatch):
    body = "i" * 999
    monkeypatch.setattr(server, "_get_conn", lambda: object())
    monkeypatch.setattr(server.T, "get_doc",
                        lambda *a, **k: {"path": "some-doc.md", "body": body})
    out = json.loads(server.get_doc(path="some-doc.md"))
    assert out["body"] == body
    assert "body_omitted" not in out
