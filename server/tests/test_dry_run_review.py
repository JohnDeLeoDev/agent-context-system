'Defects an adversarial review found in the dry-run work (test_dry_run.py holds the criteria).\n\nA dry run must reproduce the git-dependent guards and id allocation of a real write, must not\nreach the live store or its telemetry, and the smoke battery must fail when a tool does nothing.'
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_stale_write_guard import _fleet, _git, _identify

from agent_context import dryrun, server, smoke, usage
from agent_context.store import ContextStore


@pytest.fixture
def srv(monkeypatch: pytest.MonkeyPatch):
    def bind(store: ContextStore):
        monkeypatch.setattr(server, "_store", store)
        return server
    return bind


def _call(srv, tool: str, **kw: Any) -> dict:
    return json.loads(getattr(srv, tool)(**kw))


def _listing(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if ".git" not in p.relative_to(root).parts)




def test_a_dry_run_is_refused_by_the_stale_write_guard_like_the_real_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, srv) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_STALE_GUARD", "1")
    a, b = _fleet(tmp_path)
    sb = ContextStore(str(b))
    sb.upsert("memory", "shared-note", {"description": "b's version"}, body="from b")
    _git(b, "add", "-A")
    _git(b, "commit", "-qm", "b writes")
    _git(b, "push", "-q", "origin", "main")
    live = ContextStore(str(a))
    tools = srv(live)
    args = {"slug": "shared-note", "memory_type": "reference", "description": "a's", "body": "x",
            "load_behavior": "lazy"}
    real = _call(tools, "upsert_memory", **args)
    dry = _call(tools, "upsert_memory", dry_run=True, **args)
    assert "stale write refused" in real["error"]
    assert "stale write refused" in dry["result"]["error"]
    assert dry["would_change"] == []


def test_a_dry_run_allocates_the_audit_id_a_real_write_would(tmp_path: Path, srv) -> None:
    root = tmp_path / "ctx"
    (root / "global" / "audit-observations").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _identify(root)
    seen = root / "global" / "audit-observations" / "0007.json"
    seen.write_text(json.dumps({"id": 7, "observation": "an earlier one", "status": "open"}))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "an observation another machine filed")
    seen.unlink()              
    tools = srv(ContextStore(str(root)))
    args = {"observation": "something is off here", "scope": "universal", "evidence": "seen"}
    dry = _call(tools, "add_audit_observation", dry_run=True, **args)
    real = _call(tools, "add_audit_observation", **args)
    assert real["id"] == 8
    assert dry["result"]["id"] == real["id"]
    assert [c["path"] for c in dry["would_change"]] == ["global/audit-observations/0008.json"]


def test_the_dry_run_copy_of_a_git_store_leaves_the_live_git_dir_alone(
        tmp_path: Path, srv) -> None:
    root = tmp_path / "ctx"
    (root / "global").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _identify(root)
    (root / "global" / ".keep").write_text("")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    before = subprocess.run(["git", "-C", str(root), "count-objects", "-v"], text=True,
                            capture_output=True, check=True).stdout
    tools = srv(ContextStore(str(root)))
    _call(tools, "upsert_memory", dry_run=True, slug="m1", memory_type="reference",
          description="d", body="b")
    after = subprocess.run(["git", "-C", str(root), "count-objects", "-v"], text=True,
                           capture_output=True, check=True).stdout
    assert after == before
    assert not (root / "global" / "memory" / "m1.md").exists()




def test_a_dry_run_receipt_does_not_take_the_live_stores_pending_write_warning(
        store, srv) -> None:
    tools = srv(store)
    store._write_warning = "PENDING WARNING FOR ANOTHER WRITE"
    _call(tools, "upsert_memory", dry_run=True, slug="m1", memory_type="reference",
          description="d", body="b")
    assert store._write_warning == "PENDING WARNING FOR ANOTHER WRITE"


def test_usage_is_not_recorded_while_a_dry_run_is_active() -> None:
    before = json.dumps(usage.snapshot(), sort_keys=True, default=str)
    token = dryrun._ACTIVE.set(object())
    try:
        usage.record("memory", "global", "dry-run-probe", read=True, hit=True)
        usage.record_many("memory", [("global", "dry-run-probe")], read=True)
    finally:
        dryrun._ACTIVE.reset(token)
    assert json.dumps(usage.snapshot(), sort_keys=True, default=str) == before
    usage.record("memory", "global", "dry-run-probe", read=True)          
    assert json.dumps(usage.snapshot(), sort_keys=True, default=str) != before


def test_a_copy_failure_is_an_error_result_and_leaves_no_scratch(
        store, srv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = srv(store)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))

    def boom(*a: Any, **k: Any) -> None:
        raise shutil.Error("cannot copy an unreadable file")
    monkeypatch.setattr(shutil, "copytree", boom)
    out = _call(tools, "upsert_memory", dry_run=True, slug="m1", memory_type="reference",
                description="d", body="b")
    assert out["dry_run"] is True and out["would_change"] == []
    assert "unreadable file" in out["result"]["error"]
    assert list(scratch.iterdir()) == []




def test_a_write_step_that_answers_not_found_fails() -> None:
    def call(tool: str, **kw: Any) -> Any:
        return {"error": "memory 'nope' not found"}
    with pytest.raises(AssertionError):
        smoke._step(call, "upsert_memory", slug="nope", description="d")


@pytest.mark.parametrize("tool", ["delete_entity", "edit_body", "bulk_edit", "upsert_memory"])
def test_a_write_tool_that_reports_success_but_changes_nothing_fails_the_battery(
        tool: str, store, srv) -> None:
    tools = srv(store)

    def call(name: str, **kw: Any) -> Any:
        if name == tool:
            return {"deleted": None, "ok": True}          
        return smoke.invoke(getattr(tools, name), **kw)   
    assert smoke.run_writes(call)[tool] != "ok"


def test_only_a_missing_entity_counts_as_a_read_pass_not_a_missing_program() -> None:
    assert smoke._failure({"error": "no memory found for key 'x'"}) is None
    assert smoke._failure({"error": "No project found for path: /"}) is None
    assert smoke._failure({"error": "git not found on PATH"}) is not None
    assert smoke._failure({"error": "unknown kind 'x'"}) is not None


def test_url_and_local_together_report_the_network_reads_too(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_network(url: str, token: str) -> Any:
        def call(tool: str, **kw: Any) -> Any:
            if tool == "get_health":          
                raise RuntimeError("network read failed")
            return []
        return call
    monkeypatch.setattr(smoke, "_network_call", fake_network)
    assert smoke.main(["--url", "http://daemon.invalid/mcp", "--local"]) == 1
    out = capsys.readouterr().out
    assert any(line.startswith("FAIL") and "get_health" in line
               and "network read failed" in line for line in out.splitlines())


def test_the_smoke_batteries_write_no_usage_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "usage.json"
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(target))
    usage.flush()                  
    target.unlink(missing_ok=True)
    assert smoke.main(["--local"]) == 0
    usage.flush()
    assert not target.exists()
    assert "PASS" in capsys.readouterr().out
