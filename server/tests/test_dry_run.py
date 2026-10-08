'A dry run runs the real writer, guards included, against a throwaway copy of the store and reports\nwhat would change. The live store is never written. The smoke script exercises every tool.'
from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_context import dryrun, server, session, smoke
from agent_context import fstools as T

WRITERS = [
    "upsert_project", "upsert_instruction", "upsert_memory", "upsert_doc", "upsert_skill",
    "upsert_agent_definition", "upsert_command", "upsert_hook", "upsert_script",
    "add_audit_observation", "update_audit_observation", "resolve_audit_observation",
    "delete_entity", "edit_body", "set_entity_links", "set_machine", "register_path",
]


FOLDED = ["upsert_memory:description", "upsert_memory:load_behavior", "upsert_doc:append"]
CASES = WRITERS + FOLDED


STORE_CASES = [c for c in CASES if c != "register_path"]
KINDS = ("memory", "doc", "instruction", "skill", "command", "hook", "script",
         "agent_definition", "project")


@pytest.fixture
def srv(store, monkeypatch: pytest.MonkeyPatch):
    'The MCP tool functions, bound to a throwaway store.'
    monkeypatch.setattr(server, "_store", store)
    return server


def _snapshot(store) -> dict[str, str]:
    'Every file under the store root (not .git) by content hash, plus the in-memory index.'
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(store.root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = Path(dirpath) / name
            out[str(p.relative_to(store.root))] = hashlib.sha1(p.read_bytes()).hexdigest()
    index = json.dumps(sorted(store.entities), default=str)
    out["<index>"] = hashlib.sha1((index + json.dumps(
        {k: str(v) for k, v in store.entities.items()}, sort_keys=True)).encode()).hexdigest()
    return out


def _changes(before: dict[str, str], after: dict[str, str]) -> set[tuple[str, str]]:
    out = set()
    for path in before.keys() | after.keys():
        if path == "<index>":
            continue
        if path not in before:
            out.add((path, "create"))
        elif path not in after:
            out.add((path, "delete"))
        elif before[path] != after[path]:
            out.add((path, "update"))
    return out


def _call(srv, tool: str, **kw: Any) -> dict:
    return json.loads(getattr(srv, tool.split(":")[0])(**kw))


def _mem(store, slug: str = "a", body: str = "body one") -> None:
    T.upsert_memory(store, slug, "reference", f"about {slug}", body)


def _audit(store) -> None:
    T.add_audit_observation(store, "an observation to act on", scope="universal", evidence="e")



def _specs() -> dict[str, tuple[Callable[[Any], Any], dict[str, Any]]]:
    return {
        "upsert_project": (lambda s: None, {"canonical_remote": "git@github.com:acme/p.git",
                                            "display_name": "Proj"}),
        "upsert_instruction": (lambda s: None, {"title": "An instruction", "body": "do it"}),
        "upsert_memory": (lambda s: None, {"slug": "new-mem", "memory_type": "reference",
                                           "description": "d", "body": "b",
                                           "load_behavior": "lazy"}),
        "upsert_memory:description": (_mem, {"slug": "a", "description": "a new description"}),
        "upsert_memory:load_behavior": (_mem, {"slug": "a", "load_behavior": "lazy"}),
        "upsert_doc": (lambda s: None, {"path": "notes/x.md", "body": "hello", "title": "X"}),
        "upsert_doc:append": (lambda s: T.upsert_doc(s, "notes/x.md", body="hello", title="X"),
                              {"path": "notes/x.md", "body": "more", "append": True}),
        "upsert_skill": (lambda s: None, {"name": "a-skill", "description": "d", "body": "b"}),
        "upsert_agent_definition": (lambda s: None, {"name": "an-agent", "description": "d",
                                                     "body": "b"}),
        "upsert_command": (lambda s: None, {"name": "a-command", "body": "b", "description": "d"}),
        "upsert_hook": (lambda s: None, {"name": "a-hook", "event_type": "PreToolUse",
                                         "script_body": "exit 0", "language": "sh"}),
        "upsert_script": (lambda s: None, {"name": "a-script", "script_body": "exit 0",
                                           "language": "sh"}),
        "add_audit_observation": (lambda s: None, {"observation": "something is off here",
                                                   "scope": "universal", "evidence": "seen"}),
        "update_audit_observation": (_audit, {"observation_id": 1, "note": "a note"}),
        "resolve_audit_observation": (_audit, {"observation_id": 1, "resolution_note": "done"}),
        "delete_entity": (_mem, {"kind": "memory", "key": "a"}),
        "edit_body": (_mem, {"kind": "memory", "key": "a", "old_string": "body one",
                             "new_string": "body two"}),
        "set_entity_links": (lambda s: (_mem(s, "a"), _mem(s, "b")),
                             {"kind": "memory", "key": "a", "links": {"sibling": ["b"]}}),
        "set_machine": (lambda s: session._ensure_machine(s), {"display_name": "Renamed"}),
    }




@pytest.mark.parametrize("tool", WRITERS)
def test_every_writer_takes_dry_run_defaulting_to_false(tool: str) -> None:
    param = inspect.signature(getattr(server, tool)).parameters.get("dry_run")
    assert param is not None and param.default is False


@pytest.mark.parametrize("tool", WRITERS)
def test_the_tool_schema_advertises_dry_run(tool: str) -> None:
    mcp_tool = next(t for t in server.mcp._tool_manager.list_tools() if t.name == tool)
    param = mcp_tool.parameters["properties"]["dry_run"]
    assert param["type"] == "boolean" and "write nothing" in param["description"]


def test_the_spec_table_covers_every_writer() -> None:
    assert set(_specs()) == set(STORE_CASES)


def test_bulk_edit_keeps_its_own_dry_run() -> None:
    assert inspect.signature(server.bulk_edit).parameters["dry_run"].default is False




@pytest.mark.parametrize("tool", STORE_CASES)
def test_a_dry_run_leaves_the_live_store_untouched_and_names_the_changes(
        tool: str, store, srv) -> None:
    setup, args = _specs()[tool]
    setup(store)
    before = _snapshot(store)
    out = _call(srv, tool, dry_run=True, **args)
    assert _snapshot(store) == before
    assert out["dry_run"] is True
    assert out["would_change"], out
    assert all(set(c) == {"path", "action"} and c["action"] in ("create", "update", "delete")
               for c in out["would_change"])
    assert "result" in out


@pytest.mark.parametrize("tool", STORE_CASES)
def test_would_change_is_what_the_real_call_then_changes(tool: str, store, srv) -> None:
    setup, args = _specs()[tool]
    setup(store)
    dry = _call(srv, tool, dry_run=True, **args)
    before = _snapshot(store)
    real = _call(srv, tool, **args)
    assert "error" not in real, real
    assert _changes(before, _snapshot(store)) == {
        (c["path"], c["action"]) for c in dry["would_change"]}


def test_the_dry_run_result_is_the_tools_own_receipt(store, srv) -> None:
    dry = _call(srv, "upsert_memory", dry_run=True, slug="m1", memory_type="reference",
                description="d", body="b", load_behavior="lazy")
    real = _call(srv, "upsert_memory", slug="m1", memory_type="reference", description="d",
                 body="b", load_behavior="lazy")
    assert dry["result"]["slug"] == real["slug"] == "m1"


def test_a_guard_failure_is_the_normal_error_and_nothing_would_change(store, srv) -> None:
    before = _snapshot(store)
    out = _call(srv, "add_audit_observation", dry_run=True, observation="x")   
    assert out["dry_run"] is True and out["would_change"] == []
    assert "error" in out["result"]
    assert _snapshot(store) == before


def test_a_dry_run_arms_no_commit_and_fires_no_write_callback(
        store, srv, monkeypatch: pytest.MonkeyPatch) -> None:
    armed: list[str] = []
    seen: list[list[str]] = []
    monkeypatch.setattr(store, "_arm_commit", lambda path, *a, **k: armed.append(path))
    store._write_callbacks.append(seen.append)
    _call(srv, "upsert_doc", dry_run=True, path="n/x.md", body="hello", title="X")
    assert armed == [] and seen == []
    _call(srv, "upsert_doc", path="n/x.md", body="hello", title="X")
    assert armed and seen                      


def test_a_dry_run_upsert_project_writes_no_marker_into_the_checkout(
        store, srv, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    args = {"canonical_remote": "git@github.com:acme/p.git", "display_name": "Proj",
            "local_path": str(repo)}
    out = _call(srv, "upsert_project", dry_run=True, **args)
    assert not (repo / ".agents" / "project-id").exists()
    assert out["dry_run"] is True
    _call(srv, "upsert_project", **args)
    assert (repo / ".agents" / "project-id").exists()


def test_a_dry_run_register_path_writes_no_marker(store, srv, tmp_path: Path) -> None:
    T.upsert_project(store, "git@github.com:acme/p.git", "Proj")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _call(srv, "register_path", dry_run=True, cwd=str(repo), project="Proj")
    assert not (repo / ".agents" / "project-id").exists()


def test_the_override_is_gone_after_a_dry_run_including_one_that_errors(store, srv) -> None:
    _call(srv, "upsert_memory", dry_run=True, slug="m1", memory_type="reference",
          description="d", body="b")
    assert dryrun.active_store() is None and not dryrun.active()
    assert server._get_conn() is store
    out = _call(srv, "upsert_memory", dry_run=True, slug="m2", memory_type="reference",
                description="d", body="b", project="Nope", workspace="Also")
    assert "error" in out["result"]
    assert dryrun.active_store() is None and server._get_conn() is store


def test_the_throwaway_copy_is_removed_afterwards(
        store, srv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    _call(srv, "upsert_memory", dry_run=True, slug="m1", memory_type="reference",
          description="d", body="b")
    assert list(scratch.iterdir()) == []


def test_the_dry_run_sees_the_existing_store_so_guards_can_refuse_a_clobber(
        store, srv) -> None:
    T.upsert_project(store, "git@github.com:acme/p.git", "Proj")
    out = _call(srv, "upsert_project", dry_run=True,
                canonical_remote="git@github.com:acme/OTHER.git", display_name="Proj")
    assert "clobber" in out["result"]["error"]
    assert out["would_change"] == []




@pytest.mark.parametrize("tool", STORE_CASES)
def test_an_explicit_dry_run_false_is_the_ordinary_write(tool: str, store, srv) -> None:
    setup, args = _specs()[tool]
    setup(store)
    before = _snapshot(store)
    out = _call(srv, tool, dry_run=False, **args)
    assert "dry_run" not in out and "would_change" not in out
    assert _changes(before, _snapshot(store))




def _kind_call(tool: str, kind: str) -> dict:
    args: dict[str, dict[str, Any]] = {
        "delete_entity": {"kind": kind, "key": "no-such"},
        "edit_body": {"kind": kind, "key": "no-such", "old_string": "a", "new_string": "b"},
        "get_entity": {"kind": kind, "key": "no-such"},
        "list_entities": {"kind": kind},
    }
    raw = getattr(server, tool)(**args[tool])
    out = json.loads(raw) if raw else None
    return out if isinstance(out, dict) else {}


@pytest.mark.parametrize("tool", ["delete_entity", "edit_body", "get_entity", "list_entities"])
def test_the_kind_enum_is_exactly_the_kinds_it_accepts(tool: str, store, srv) -> None:
    accepted = [k for k in KINDS if "unknown kind" not in str(_kind_call(tool, k).get("error"))]
    assert len(accepted) >= 7
    schema = next(t.parameters for t in server.mcp._tool_manager.list_tools() if t.name == tool)
    assert sorted(schema["properties"]["kind"]["enum"]) == sorted(accepted)


@pytest.mark.parametrize("tool", ["delete_entity", "edit_body", "get_entity", "list_entities"])
def test_an_unknown_kind_lists_the_valid_kinds(tool: str, store, srv) -> None:
    err = str(_kind_call(tool, "bogus")["error"])
    for k in KINDS:
        if "unknown kind" not in str(_kind_call(tool, k).get("error")):
            assert k in err




def _roster() -> set[str]:
    return {t.name for t in server.mcp._tool_manager.list_tools()}


def test_the_smoke_tables_cover_every_tool_exactly_once() -> None:
    reads, writes = set(smoke.READ_TOOLS), set(smoke.WRITE_TOOLS)
    assert reads.isdisjoint(writes)
    assert reads | writes == _roster()


def test_every_writer_is_a_write_tool_in_the_smoke_battery() -> None:
    assert set(WRITERS) <= set(smoke.WRITE_TOOLS)


def _local_call(srv) -> Callable[..., Any]:
    def call(tool: str, **kw: Any) -> Any:
        return smoke.invoke(getattr(srv, tool), **kw)
    return call


def test_the_read_battery_passes_against_a_store(store, srv) -> None:
    results = smoke.run_reads(_local_call(srv))
    assert results and set(results) == set(smoke.READ_TOOLS)
    assert {t: r for t, r in results.items() if r != "ok"} == {}


def test_the_write_battery_passes_against_a_throwaway_store(store, srv) -> None:
    results = smoke.run_writes(_local_call(srv))
    assert results and set(results) == set(smoke.WRITE_TOOLS)
    assert {t: r for t, r in results.items() if r != "ok"} == {}


def test_a_failing_call_is_reported_by_tool_name(store, srv) -> None:
    def call(tool: str, **kw: Any) -> Any:
        if tool == "list_audit_observations":
            raise RuntimeError("boom")
        return json.loads(getattr(srv, tool)(**kw))
    results = smoke.run_reads(call)
    assert "boom" in results["list_audit_observations"]
    assert results["list_machines"] == "ok"


def test_main_local_exits_zero_and_prints_a_line_per_tool(
        capsys: pytest.CaptureFixture[str]) -> None:
    assert smoke.main(["--local"]) == 0
    out = capsys.readouterr().out
    assert all(t in out for t in _roster())


def test_main_exits_one_when_a_tool_fails(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(smoke.READ_TOOLS, "list_machines", {"no_such_argument": 1})
    assert smoke.main(["--local"]) == 1
    assert any(line.startswith("FAIL") and "list_machines" in line
               for line in capsys.readouterr().out.splitlines())


def test_main_against_an_unreachable_daemon_fails_cleanly(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "x")
    assert smoke.main(["--url", "http://127.0.0.1:9/mcp"]) == 1
    assert "127.0.0.1:9" in capsys.readouterr().out


def test_the_write_battery_never_writes_the_store_it_is_not_given(
        store, srv, capsys: pytest.CaptureFixture[str]) -> None:
    before = _snapshot(store)
    assert smoke.main(["--local"]) == 0
    assert "upsert_memory" in capsys.readouterr().out       
    assert _snapshot(store) == before
