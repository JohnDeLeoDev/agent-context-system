'`session.machine_admin`: the per-machine setters behind the `set_machine` and\n`register_path` MCP tools.\n\nThe MCP surface was one `machine_admin` tool with an `action`; it is now `set_machine`\n(display_name, sleeps, relay_only) and `register_path`. Both route through\nsession.machine_admin, so these tests of its actions still cover every code path.'
import inspect

from agent_context import fstools as T
from agent_context import project_resolve as PR
from agent_context import server as S

ACTIONS = ("set_display_name", "set_sleeps", "register_path", "translate_paths")
REMOVED = ("sync_materialization", "canonicalize_typed_links", "set_machine_display_name",
           "set_machine_sleeps", "register_machine_path", "translate_project_paths")


def _me(store):
    'The isolated store starts with no machine row; bootstrapping creates this one.'
    return T.get_session_context(store, "/nowhere")["machine"]["machine_uuid"]


def _registered():
    return set(S.mcp._tool_manager._tools)


def test_set_machine_and_register_path_are_registered():
    assert {"set_machine", "register_path"} <= _registered()
    assert "machine_admin" not in _registered()
    assert {"display_name", "sleeps", "relay_only", "machine"} <= set(
        inspect.signature(S.set_machine).parameters)


def test_the_six_folded_or_removed_tools_are_gone():
    left = [name for name in REMOVED if name in _registered()]
    assert not left, f"still registered: {left}"


def test_set_display_name_matches_the_old_tool(store):
    me = _me(store)
    r = T.machine_admin(store, "set_display_name", display_name="Bench")
    assert r == {"machine_uuid": me, "display_name": "Bench"}
    assert [m["display_name"] for m in T.list_machines(store)] == ["Bench"]


def test_set_display_name_can_target_another_machine_by_name(store):
    _me(store)
    T.machine_admin(store, "set_display_name", display_name="First")
    r = T.machine_admin(store, "set_display_name", display_name="Second", machine="First")
    assert r["display_name"] == "Second"
    assert [m["display_name"] for m in T.list_machines(store)] == ["Second"]


def test_set_sleeps_true_and_false_are_both_values_not_missing(store):
    me = _me(store)
    on = T.machine_admin(store, "set_sleeps", sleeps=True)
    assert on["machine_uuid"] == me and on["sleeps"] is True
    off = T.machine_admin(store, "set_sleeps", sleeps=False)
    assert "error" not in off and off["sleeps"] is False


def test_register_path_matches_the_old_tool(store, monkeypatch, tmp_path):
    T.upsert_project(store, "example.invalid:/volume1/GitServer/Personal/example.invalid", "site")
    repo = tmp_path / "checkout"
    repo.mkdir()
    monkeypatch.setattr(PR, "get_git_remotes", lambda cwd: ["user@ls:/elsewhere/example.invalid"])
    monkeypatch.setattr(PR, "get_git_remote", lambda cwd: "user@ls:/elsewhere/example.invalid")
    monkeypatch.setattr(PR, "get_git_branch", lambda cwd: "main")
    monkeypatch.setattr(PR, "get_repo_root", lambda cwd: str(repo))
    assert "error" in T.machine_admin(store, "register_path", cwd=str(repo))
    assert "error" in T.machine_admin(store, "register_path", cwd=str(repo), project="nope")
    r = T.machine_admin(store, "register_path", cwd=str(repo), project="site")
    assert "error" not in r and r["display_name"] == "site" and r["marker_written"]
    assert (repo / ".agents" / "project-id").is_file()


def test_translate_paths_matches_the_old_tool(store):
    assert T.machine_admin(store, "translate_paths") == T.translate_project_paths(store)


def test_unknown_action_names_the_valid_ones_and_writes_nothing(store):
    _me(store)
    T.machine_admin(store, "set_display_name", display_name="Keep")
    r = T.machine_admin(store, "rename_everything", display_name="Lost")
    assert "error" in r
    for action in ACTIONS:
        assert action in r["error"], f"{action} missing from: {r['error']}"
    assert [m["display_name"] for m in T.list_machines(store)] == ["Keep"]


def test_missing_required_argument_is_an_error_naming_it(store):
    _me(store)
    T.machine_admin(store, "set_display_name", display_name="Keep")
    for action, missing in (("set_display_name", "display_name"),
                            ("set_sleeps", "sleeps"),
                            ("register_path", "cwd")):
        r = T.machine_admin(store, action)
        assert "error" in r and missing in r["error"], (action, r)
    assert [m["display_name"] for m in T.list_machines(store)] == ["Keep"]
