'The incident: `upsert_hook` was called on a python hook to change one field, with\n`language` omitted. The parameter\'s Python default was "sh", so the hook was rewritten\nas a shell script and stopped running. Nothing reported it; the call returned success.\n\nWhat makes this worth a gate rather than a note is that partial updates are an\nADVERTISED feature — upsert_hook\'s own docstring says script_body "may be omitted on an\nEXISTING hook to change only its metadata/description without re-sending the script".\nThe documented way to make a small edit was the destructive one.\n\nSo these tests cover every upsert, not the one that was reported.'
import pytest
from narrow import notnone

from agent_context import fstools as T
from agent_context import memory as M



def test_omitted_language_does_not_rewrite_a_python_hook_as_sh(store):
    T.upsert_hook(store, "probe", "PreToolUse", "import sys", language="python",
                  matcher="Bash", timeout_seconds=45)
    
    T.upsert_hook(store, "probe", description="now with a description")
    out = notnone(T.get_hook(store, "probe"))
    assert out["language"] == "python", "policy: language was reset to the sh default"
    assert out["matcher"] == "Bash", "matcher was reset"
    assert out["timeout_seconds"] == 45, "timeout_seconds was reset to 30"
    assert out["event_type"] == "PreToolUse", "event_type was lost"
    assert out["script_body"] == "import sys", "body was lost (policy contract)"
    assert out["description"] == "now with a description"


def test_a_given_value_still_wins_over_the_stored_one(store):
    T.upsert_hook(store, "probe", "PreToolUse", "x", language="python")
    T.upsert_hook(store, "probe", language="sh")
    assert notnone(T.get_hook(store, "probe"))["language"] == "sh"


def test_empty_string_clears_a_field_that_carrying_would_otherwise_pin(store):
    'Carrying makes a field one-way unless there is a way to say "no value".\n\n    Without this, a hook that once had a matcher could never go back to having none:\n    omitting it carries, and there is no other spelling. `""` is that spelling.'
    T.upsert_hook(store, "probe", "SessionStart", "x", matcher="Bash")
    T.upsert_hook(store, "probe", matcher="")
    assert not notnone(T.get_hook(store, "probe"))["matcher"]


def test_a_new_hook_still_requires_event_type(store):
    'event_type is carried on update but has no sane default on create: it is WHEN\n    the hook runs, and a guess produces a hook that registers and never fires.'
    out = notnone(T.upsert_hook(store, "brand-new", script_body="exit 0"))
    assert "error" in out and "event_type is required" in out["error"]



def test_script_carries_its_language(store):
    T.upsert_script(store, "probe", "print(1)", language="python")
    T.upsert_script(store, "probe", description="changed")
    assert notnone(T.get_script(store, "probe"))["language"] == "python"


def test_skill_carries_allowed_tools(store):
    T.upsert_skill(store, "probe", "a skill", "body", allowed_tools="Read,Grep")
    T.upsert_skill(store, "probe", description="reworded")
    assert notnone(T.get_skill(store, "probe"))["allowed_tools"] == "Read,Grep"


def test_skill_carries_disable_model_invocation_and_false_clears_it(store):
    T.upsert_skill(store, "probe", "a skill", "body", disable_model_invocation=True)
    T.upsert_skill(store, "probe", description="reworded")
    assert notnone(T.get_skill(store, "probe"))["disable_model_invocation"] is True
    T.upsert_skill(store, "probe", disable_model_invocation=False)
    assert notnone(T.get_skill(store, "probe"))["disable_model_invocation"] is False


def test_command_carries_allowed_tools_disable_model_invocation_and_argument_hint(store):
    T.upsert_command(store, "probe", "body", description="d", allowed_tools=["Bash", "Read"],
                     disable_model_invocation=True, argument_hint="[branch]")
    T.upsert_command(store, "probe", description="reworded")
    out = notnone(T.get_command(store, "probe"))
    assert out["allowed_tools"] == ["Bash", "Read"]
    assert out["disable_model_invocation"] is True
    assert out["argument_hint"] == "[branch]"


def test_command_empty_list_clears_allowed_tools(store):
    T.upsert_command(store, "probe", "body", description="d", allowed_tools=["Bash"])
    T.upsert_command(store, "probe", allowed_tools=[])
    assert not notnone(T.get_command(store, "probe"))["allowed_tools"]


def test_agent_definition_carries_origin_which_its_own_loop_missed(store):
    T.upsert_agent_definition(store, "probe", "a worker", "body",
                              model="sonnet", origin="user")
    T.upsert_agent_definition(store, "probe", description="reworded")
    out = notnone(T.get_agent_definition(store, "probe"))
    assert out["model"] == "sonnet", "the hand-rolled carry regressed"
    assert out["origin"] == "user", "policy: origin was outside that loop"


def test_command_carries_origin(store):
    T.upsert_command(store, "probe", "body", description="d", origin="user")
    T.upsert_command(store, "probe", description="reworded")
    assert notnone(T.get_command(store, "probe"))["origin"] == "user"


def test_memory_carries_origin_and_still_carries_its_tier(store):
    'test memory carries origin and still carries its tier.'
    M.upsert_memory(store, "probe", "reference", "a description", "body",
                    origin="user", load_behavior="lazy")
    M.upsert_memory(store, "probe", "reference", "a description", "rewritten body")
    out = notnone(M.get_memory(store, "probe"))
    assert out["origin"] == "user", "policy: origin was reset to the agent default"
    assert out["load_behavior"] == "lazy", "policy regressed"



@pytest.mark.parametrize("field,expected", [("language", "sh"), ("timeout_seconds", 30),
                                            ("origin", "user")])
def test_a_brand_new_entity_gets_the_documented_default(store, field, expected):
    'Carrying must not become "always None on create". These are the values the tool\n    docs promise when a field is never supplied at all.'
    T.upsert_hook(store, "fresh", "PreToolUse", "exit 0")
    assert notnone(T.get_hook(store, "fresh"))[field] == expected
