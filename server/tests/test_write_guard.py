"Server-side guards on the store's write path.\n\nCriteria 8 to 11 and 13 to 17 of the steps 0 and 1 plan (write-guard), plus the ruling that\nglobal and workspace scope instructions are protected. Until now every guard on a store write\nwas a hook in the calling harness; a token holder could call upsert_hook over HTTP and skip\nall of them. The check lives in `Store.upsert`, `Store.delete` and `paths.write_atomic`, keyed\non the caller bound by the ASGI wrapper, so no tool can be added later that skips it."
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_context import (
    docs,
    dryrun,
    entities,
    generic,
    memory,
    paths,
    token_table,
    write_audit,
    write_guard,
)
from agent_context.write_guard import Caller, WriteDenied

_REAL_PROTECT_FILE = write_audit.protect_file


@pytest.fixture(autouse=True)
def guard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "enforce")
    monkeypatch.setenv("AGENT_CONTEXT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    for name in ("AGENT_CONTEXT_FREE_WRITES_PER_HOUR", "AGENT_CONTEXT_DENIALS_BEFORE_SUSPEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(write_audit, "protect_file", lambda path: None)
    write_guard.reset_state()
    token_table.issue(tmp_path / "tokens.json", id="reader", scopes=["read"])
    token_table.issue(tmp_path / "tokens.json", id="writer", scopes=["read", "entity-write"])
    token_table.issue(tmp_path / "tokens.json", id="admin",
                 scopes=["read", "entity-write", "protected-write"])


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(write_guard, "alert", lambda text: seen.append(text))
    return seen


@contextmanager
def as_token(token_id: str, ip: str = "100.64.0.9") -> Iterator[Caller]:
    who = Caller(token_id=token_id, machine_id="laptop", ip=ip, session_key="s-1")
    bound = write_guard.bind(who)
    try:
        yield who
    finally:
        write_guard.reset(bound)


def _hook(store, name: str = "h1", body: str = "print(1)\n") -> None:
    entities.upsert_hook(store, name, event_type="PreToolUse", script_body=body, language="py")


def _hook_body(store, name: str = "h1") -> str:
    entry = entities.get_hook(store, name)
    assert entry is not None
    return str(entry["script_body"])


def _script_body(store, name: str) -> str:
    entry = entities.get_script(store, name)
    assert entry is not None
    return str(entry["script_body"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()





def test_a_write_with_no_bound_token_is_a_system_write_and_passes(store) -> None:
    assert write_guard.current() is None
    _hook(store)
    assert entities.get_hook(store, "h1") is not None
    generic.delete_entity(store, "hook", "h1")
    assert entities.get_hook(store, "h1") is None





def test_a_read_only_token_cannot_write_anything(store) -> None:
    with as_token("reader"):
        with pytest.raises(WriteDenied):
            memory.upsert_memory(store, "m1", "feedback", "d", "body")
        with pytest.raises(WriteDenied):
            docs.upsert_doc(store, "notes/a.md", body="x")
    assert memory.get_memory(store, "m1") is None


def test_the_denial_names_the_missing_scope_and_the_token(store) -> None:
    with as_token("reader"), pytest.raises(WriteDenied) as info:
        memory.upsert_memory(store, "m1", "feedback", "d", "body")
    text = str(info.value)
    assert "denied" in text and "entity-write" in text and "reader" in text


def test_write_denied_is_a_value_error_so_existing_tool_handlers_report_it() -> None:
    assert issubclass(WriteDenied, ValueError)





def test_entity_write_can_write_the_free_class(store) -> None:
    (Path(store.root) / "projects" / "p1").mkdir(parents=True)
    with as_token("writer"):
        memory.upsert_memory(store, "m1", "feedback", "d", "body")
        docs.upsert_doc(store, "notes/a.md", body="x")
        entities.upsert_skill(store, "sk1", "d", body="b")
        entities.upsert_command(store, "c1", body="b")
        memory.upsert_instruction(store, "Project rule", "body", project="p1")
    assert memory.get_memory(store, "m1") is not None
    assert docs.get_doc(store, "notes/a.md") is not None


def test_entity_write_cannot_upsert_a_hook_a_script_or_an_agent_definition(store) -> None:
    with as_token("writer"):
        with pytest.raises(WriteDenied):
            _hook(store)
        with pytest.raises(WriteDenied):
            entities.upsert_script(store, "s", script_body="x = 1\n", language="py")
        with pytest.raises(WriteDenied):
            entities.upsert_agent_definition(store, "a1", "d", body="b")
    assert entities.get_hook(store, "h1") is None
    assert entities.get_script(store, "s") is None


def test_global_and_workspace_instructions_are_protected_and_project_ones_are_free(
        store) -> None:
    (Path(store.root) / "projects" / "p1").mkdir(parents=True)
    with as_token("writer"):
        with pytest.raises(WriteDenied):
            memory.upsert_instruction(store, "Global rule", "body")
        with pytest.raises(WriteDenied):
            store.upsert("instruction", "WS rule", {"load_behavior": "always"}, body="b",
                         scope="ws:example-workspace")
        memory.upsert_instruction(store, "Project rule", "body", project="p1")
    with as_token("admin"):
        memory.upsert_instruction(store, "Global rule", "body")


@pytest.mark.parametrize("kind,scope,key,expected", [
    ("hook", "global", "h", "protected"),
    ("hook", "project:p", "h", "protected"),
    ("script", "global", "anything.py", "protected"),
    ("agent_definition", "global", "worker", "protected"),
    ("instruction", "global", "T", "protected"),
    ("instruction", "ws:example-workspace", "T", "protected"),
    ("instruction", "project:p", "T", "free"),
    ("memory", "global", "m", "free"),
    ("doc", "global", "a.md", "free"),
    ("skill", "global", "s", "free"),
    ("command", "global", "c", "free"),
])
def test_the_classifier(kind: str, scope: str, key: str, expected: str) -> None:
    assert write_guard.classify(kind, scope, key) == expected


def test_scripts_are_default_deny_with_an_empty_free_allowlist() -> None:
    assert write_guard.FREE_SCRIPTS == frozenset()


def test_edit_body_and_delete_on_protected_entities_are_denied_for_entity_write(store) -> None:
    _hook(store, body="print('aaa')\n")
    with as_token("writer"):
        with pytest.raises(WriteDenied):
            generic.edit_body(store, "hook", "h1", "aaa", "bbb")
        with pytest.raises(WriteDenied):
            generic.delete_entity(store, "hook", "h1")
    assert "aaa" in _hook_body(store, "h1")


def test_bulk_edit_reports_a_denied_entry_and_applies_the_rest(store) -> None:
    memory.upsert_memory(store, "m1", "feedback", "d", "aaa")
    _hook(store, body="print('aaa')\n")
    edits = [{"kind": "memory", "key": "m1", "replacements": [["aaa", "bbb"]]},
             {"kind": "hook", "key": "h1", "replacements": [["aaa", "bbb"]]}]
    with as_token("writer"):
        out = generic.bulk_edit(store, edits)
    by_kind = {r["kind"]: r for r in out["results"]}
    assert by_kind["memory"]["applied"] == 1
    assert by_kind["hook"]["applied"] == 0
    assert "denied" in by_kind["hook"]["error"]
    assert "aaa" in _hook_body(store, "h1")





def test_protected_write_can_change_hooks_and_scripts_and_delete_them(store) -> None:
    with as_token("admin"):
        _hook(store)
        entities.upsert_script(store, "s", script_body="x = 1\n", language="py")
        generic.edit_body(store, "hook", "h1", "print(1)", "print(2)")
        generic.delete_entity(store, "script", "s")
    assert "print(2)" in _hook_body(store, "h1")
    assert entities.get_script(store, "s") is None


def test_invalid_python_is_refused_and_nothing_is_written(store) -> None:
    with as_token("admin"), pytest.raises(WriteDenied) as info:
        _hook(store, body="def broken(:\n    pass\n")
    assert "syntax" in str(info.value).lower()
    assert entities.get_hook(store, "h1") is None


def test_invalid_python_in_a_script_is_refused(store) -> None:
    with as_token("admin"), pytest.raises(WriteDenied):
        entities.upsert_script(store, "s", script_body="def (:\n", language="py")
    assert entities.get_script(store, "s") is None


def test_invalid_shell_in_a_script_is_refused(store) -> None:
    with as_token("admin"), pytest.raises(WriteDenied):
        entities.upsert_script(store, "s", script_body="case x in\n", language="sh")
    assert entities.get_script(store, "s") is None


def test_invalid_shell_is_refused(store) -> None:
    with as_token("admin"), pytest.raises(WriteDenied):
        entities.upsert_hook(store, "h2", event_type="PreToolUse",
                             script_body="if [ -f x ; then\n", language="sh")
    assert entities.get_hook(store, "h2") is None


def test_valid_python_and_shell_pass(store) -> None:
    with as_token("admin"):
        _hook(store)
        entities.upsert_hook(store, "h2", event_type="PreToolUse",
                             script_body="#!/bin/sh\necho ok\n", language="sh")
    assert entities.get_hook(store, "h2") is not None


def test_python_is_checked_against_the_oldest_interpreter_in_the_fleet(store) -> None:
    'Synology nodes run 3.8, where `match` is a syntax error.'
    body = "match 1:\n    case 1:\n        pass\n"
    with as_token("admin"), pytest.raises(WriteDenied):
        _hook(store, body=body)


def test_the_syntax_check_never_executes_the_body(store, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    body = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
    with as_token("admin"):
        _hook(store, body=body)
    assert not marker.exists()


def test_an_oversized_body_is_refused(store) -> None:
    with as_token("admin"), pytest.raises(WriteDenied) as info:
        _hook(store, body="x = 1\n" + "# pad\n" * 100_000)
    assert "size" in str(info.value).lower() or "large" in str(info.value).lower()


def _lock(store, rel: str, body: str) -> Path:
    locks = Path(store.root) / "global" / "test-locks"
    locks.mkdir(parents=True, exist_ok=True)
    record = locks / (rel.replace("/", "__") + ".json")
    record.write_text(json.dumps({"locked_at": "2026-09-21T00:00:00Z", "path": rel,
                                  "sha256": _sha(body)}))
    return record


def test_a_locked_test_script_cannot_be_changed_by_any_token_until_unlocked(store) -> None:
    original = "def test_a():\n    assert True\n"
    entities.upsert_script(store, "test-x", script_body=original, language="py")
    record = _lock(store, "global/scripts/test-x.py", original)
    changed = "def test_a():\n    assert False\n"
    with as_token("admin"):
        with pytest.raises(WriteDenied) as info:
            entities.upsert_script(store, "test-x", script_body=changed, language="py")
        assert "lock" in str(info.value).lower()
        entities.upsert_script(store, "test-x", script_body=original, language="py")
        record.unlink()
        entities.upsert_script(store, "test-x", script_body=changed, language="py")
    assert "assert False" in _script_body(store, "test-x")


def test_a_locked_test_script_cannot_be_deleted(store) -> None:
    original = "def test_a():\n    assert True\n"
    entities.upsert_script(store, "test-x", script_body=original, language="py")
    _lock(store, "global/scripts/test-x.py", original)
    with as_token("admin"), pytest.raises(WriteDenied):
        generic.delete_entity(store, "script", "test-x")




PROTECTED_PATHS = [
    "global/hooks-manifest.json", "global/hook-fingerprints.json", "global/mcp-servers.json",
    "global/settings-seed.json", "hook-dispatch.json", "global/test-locks/a.json", "AGENTS.md",
    "server/src/agent_context/x.py", "global/hooks/h.py", "global/scripts/s.py",
    "global/agents/a.md", "global/instructions/i.md", "workspaces/W/instructions/i.md",
]


@pytest.mark.parametrize("rel", PROTECTED_PATHS)
def test_a_protected_path_is_refused_for_entity_write_and_open_to_protected_write(
        store, rel: str) -> None:
    target = Path(store.root) / rel
    with as_token("writer"), pytest.raises(WriteDenied):
        paths.write_atomic(target, "x")
    assert not target.exists()
    with as_token("admin"):
        paths.write_atomic(target, "x")
    assert target.read_text() == "x"


@pytest.mark.parametrize("rel", PROTECTED_PATHS)
def test_a_protected_path_is_open_to_a_system_write(store, rel: str) -> None:
    paths.write_atomic(Path(store.root) / rel, "x")
    assert (Path(store.root) / rel).exists()


def test_a_free_path_and_a_path_outside_every_store_are_not_touched_by_the_backstop(
        store, tmp_path: Path) -> None:
    with as_token("writer"):
        paths.write_atomic(Path(store.root) / "global" / "memory" / "m.md", "x")
        paths.write_atomic(tmp_path / "elsewhere" / "state.json", "x")
    with as_token("reader"):
        paths.write_atomic(tmp_path / "elsewhere" / "state.json", "x")


def test_operational_records_stay_writable_by_a_read_only_token(store) -> None:
    'Machine rows, audit observations and session state are written by every machine at\n    session start. Read scope means no ENTITY writes, so these must not break.'
    with as_token("reader"):
        paths.write_atomic(Path(store.root) / "machines" / "u.toml", "x")
        paths.write_atomic(Path(store.root) / "global" / "audit-observations" / "1.md", "x")


def test_every_module_that_writes_or_removes_files_is_on_the_reviewed_list() -> None:
    'A new writer must be looked at: does a remote caller reach it, and does it go through\n    `paths.write_atomic` (backstop) or `Store.upsert`/`delete` (typed check)?'
    reviewed = {"audit.py", "claims.py", "daemon.py", "dryrun.py", "gate_lock.py", "janitor.py",
                "paths.py",
                "projects.py", "relay_materialize.py", "relay_source.py", "relay_update.py",
                "server.py", "smoke.py", "store.py", "usage.py", "write_audit.py",
                
                
                "task_worker.py"}
    primitive = re.compile(
        r"""\bopen\([^)\n]*["'][wax]b?\+?["']|\.write_text\(|\.write_bytes\("""
        r"""|\bos\.remove\(|\bos\.unlink\(|shutil\.rmtree|\bos\.rename\(|\.unlink\(""")
    src = Path(__file__).resolve().parent.parent / "src" / "agent_context"
    found = {p.name for p in src.glob("*.py") if primitive.search(p.read_text())}
    assert found <= reviewed, f"unreviewed writer modules: {sorted(found - reviewed)}"





def test_a_dry_run_shows_the_refusal_and_writes_nothing(store) -> None:
    with as_token("writer"), pytest.raises(WriteDenied):
        dryrun.run(store, lambda: json.dumps(
            entities.upsert_hook(dryrun.active_store(), "h1", event_type="PreToolUse",
                                 script_body="print(1)\n", language="py"), default=str))
    assert entities.get_hook(store, "h1") is None


def test_a_dry_run_that_is_allowed_leaves_the_live_store_alone(store) -> None:
    with as_token("admin"):
        out = dryrun.run(store, lambda: json.dumps(
            entities.upsert_hook(dryrun.active_store(), "h1", event_type="PreToolUse",
                                 script_body="print(1)\n", language="py"), default=str))
    assert json.loads(out)["dry_run"] is True
    assert entities.get_hook(store, "h1") is None





def test_observe_mode_logs_a_would_deny_and_blocks_nothing(
        store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "observe")
    with as_token("writer"):
        _hook(store)
    assert entities.get_hook(store, "h1") is not None
    lines = _audit_lines(tmp_path)
    assert any(line["decision"] == "would-deny" and line["kind"] == "hook" for line in lines)


def test_off_mode_blocks_nothing_and_says_so_at_startup(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "off")
    with as_token("reader"):
        _hook(store)
    assert entities.get_hook(store, "h1") is not None
    notice = write_guard.startup_notice()
    assert notice is not None and "off" in notice.lower()


def test_enforce_mode_blocks_and_has_no_startup_warning(store) -> None:
    with as_token("reader"), pytest.raises(WriteDenied):
        _hook(store)
    assert write_guard.startup_notice() is None


def test_an_unset_mode_is_observe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_GUARD", raising=False)
    assert write_guard.mode() == "observe"


def test_an_unknown_mode_falls_back_to_enforce_not_to_off(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "banana")
    assert write_guard.mode() == "enforce"


def test_a_revoked_token_is_denied_at_write_time_even_inside_an_open_session(
        store, tmp_path: Path) -> None:
    with as_token("writer"):
        memory.upsert_memory(store, "m1", "feedback", "d", "body")
        token_table.revoke(tmp_path / "tokens.json", "writer")
        with pytest.raises(WriteDenied) as info:
            memory.upsert_memory(store, "m2", "feedback", "d", "body")
    assert "revoked" in str(info.value).lower() or "no longer valid" in str(info.value).lower()


def test_a_scope_change_in_the_table_applies_to_an_open_session(
        store, tmp_path: Path) -> None:
    import os
    import time
    with as_token("writer"):
        memory.upsert_memory(store, "m1", "feedback", "d", "body")
        table = tmp_path / "tokens.json"
        data = json.loads(table.read_text())
        for entry in data["tokens"]:
            if entry["id"] == "writer":
                entry["scopes"] = ["read"]
        table.write_text(json.dumps(data))
        os.utime(table, (time.time() + 5, time.time() + 5))
        with pytest.raises(WriteDenied):
            memory.upsert_memory(store, "m2", "feedback", "d", "body")





@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1_000_000.0]
    monkeypatch.setattr(write_guard, "_now", lambda: now[0])
    return now


def test_free_writes_are_limited_per_token_per_hour(
        store, clock: list[float], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_FREE_WRITES_PER_HOUR", "3")
    with as_token("writer"):
        for i in range(3):
            memory.upsert_memory(store, f"m{i}", "feedback", "d", "body")
        with pytest.raises(WriteDenied) as info:
            memory.upsert_memory(store, "m3", "feedback", "d", "body")
        assert "rate" in str(info.value).lower()
        clock[0] += 3601
        memory.upsert_memory(store, "m3", "feedback", "d", "body")


def test_the_default_limit_is_sixty_writes_an_hour(store, clock: list[float]) -> None:
    with as_token("writer"):
        for i in range(60):
            memory.upsert_memory(store, f"m{i}", "feedback", "d", "body")
        with pytest.raises(WriteDenied):
            memory.upsert_memory(store, "over", "feedback", "d", "body")


def test_the_limit_is_per_token(store, clock: list[float],
                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_FREE_WRITES_PER_HOUR", "1")
    with as_token("writer"):
        memory.upsert_memory(store, "a", "feedback", "d", "body")
    with as_token("admin"):
        memory.upsert_memory(store, "b", "feedback", "d", "body")


def test_five_protected_denials_in_ten_minutes_suspend_the_token_and_alert_once(
        store, clock: list[float], alerts: list[str]) -> None:
    with as_token("writer"):
        for i in range(5):
            with pytest.raises(WriteDenied):
                _hook(store, name=f"h{i}")
            clock[0] += 30
        assert len(alerts) == 1 and "writer" in alerts[0]
        with pytest.raises(WriteDenied) as info:
            memory.upsert_memory(store, "m1", "feedback", "d", "body")
        assert "suspend" in str(info.value).lower()
        with pytest.raises(WriteDenied):
            _hook(store, name="h9")
        assert len(alerts) == 1


def test_denials_spread_over_more_than_ten_minutes_do_not_suspend(
        store, clock: list[float], alerts: list[str]) -> None:
    with as_token("writer"):
        for i in range(8):
            with pytest.raises(WriteDenied):
                _hook(store, name=f"h{i}")
            clock[0] += 200
        memory.upsert_memory(store, "m1", "feedback", "d", "body")
    assert alerts == []


def test_suspension_is_persisted_and_user_can_lift_it(
        store, clock: list[float], alerts: list[str]) -> None:
    with as_token("writer"):
        for i in range(5):
            with pytest.raises(WriteDenied):
                _hook(store, name=f"h{i}")
    write_guard.reset_state()
    assert write_guard.is_suspended("writer")
    write_guard.unsuspend("writer")
    with as_token("writer"):
        memory.upsert_memory(store, "m1", "feedback", "d", "body")


def test_observe_mode_counts_but_never_suspends_or_alerts(
        store, clock: list[float], alerts: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "observe")
    with as_token("writer"):
        for i in range(7):
            _hook(store, name=f"h{i}")
        memory.upsert_memory(store, "m1", "feedback", "d", "body")
    assert alerts == []
    assert not write_guard.is_suspended("writer")


def test_the_denial_threshold_is_configurable(
        store, clock: list[float], alerts: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_DENIALS_BEFORE_SUSPEND", "2")
    with as_token("writer"):
        for i in range(2):
            with pytest.raises(WriteDenied):
                _hook(store, name=f"h{i}")
    assert len(alerts) == 1





def _audit_lines(tmp_path: Path) -> list[dict]:
    out: list[dict] = []
    for f in sorted((tmp_path / "audit").glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return out


def test_every_attempt_writes_one_audit_line_with_the_documented_fields(
        store, tmp_path: Path) -> None:
    with as_token("writer"):
        memory.upsert_memory(store, "m1", "feedback", "d", "body")
        with pytest.raises(WriteDenied):
            _hook(store)
    lines = _audit_lines(tmp_path)
    assert [ln["decision"] for ln in lines] == ["allow", "deny"]
    fields = {"ts", "token_id", "machine_id", "session_key", "ip", "op", "kind", "key", "scope",
              "class", "decision", "mode", "reason", "before_sha", "after_sha", "body_len",
              "prev"}
    for line in lines:
        assert fields <= set(line)
    allow, deny = lines
    assert (allow["token_id"], allow["kind"], allow["key"], allow["class"], allow["ip"]) == (
        "writer", "memory", "m1", "free", "100.64.0.9")
    assert (deny["kind"], deny["class"], deny["op"]) == ("hook", "protected", "upsert")
    assert re.fullmatch(r"[0-9a-f]{64}", allow["after_sha"])
    assert deny["reason"]


def test_a_delete_is_audited_with_the_before_hash(store, tmp_path: Path) -> None:
    _hook(store, body="print(1)\n")
    with as_token("admin"):
        generic.delete_entity(store, "hook", "h1")
    last = _audit_lines(tmp_path)[-1]
    assert (last["op"], last["decision"], last["kind"]) == ("delete", "allow", "hook")
    assert last["before_sha"] and not last["after_sha"]


def test_the_audit_chain_verifies_and_a_tampered_line_is_detected(
        store, tmp_path: Path) -> None:
    with as_token("writer"):
        for i in range(3):
            memory.upsert_memory(store, f"m{i}", "feedback", "d", "body")
    logs = sorted((tmp_path / "audit").glob("*.jsonl"))
    assert logs, "no audit log was written"
    log = logs[0]
    assert write_audit.verify_chain(log) == (True, None)
    rows = log.read_text().splitlines()
    doctored = json.loads(rows[1])
    doctored["decision"] = "allow-edited"
    rows[1] = json.dumps(doctored)
    log.write_text("\n".join(rows) + "\n")
    ok, bad_line = write_audit.verify_chain(log)
    assert ok is False and bad_line == 3


def test_a_deleted_line_is_detected(store, tmp_path: Path) -> None:
    with as_token("writer"):
        for i in range(3):
            memory.upsert_memory(store, f"m{i}", "feedback", "d", "body")
    logs = sorted((tmp_path / "audit").glob("*.jsonl"))
    assert logs, "no audit log was written"
    log = logs[0]
    rows = log.read_text().splitlines()
    del rows[1]
    log.write_text("\n".join(rows) + "\n")
    assert write_audit.verify_chain(log)[0] is False


def test_system_writes_are_not_audited_as_remote_calls(store, tmp_path: Path) -> None:
    _hook(store)
    assert _audit_lines(tmp_path) == []


def test_the_log_is_protected_append_only_on_a_best_effort_basis(
        store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    protected: list[str] = []
    monkeypatch.setattr(write_audit, "protect_file", lambda path: protected.append(str(path)))
    write_guard.reset_state()
    with as_token("writer"):
        memory.upsert_memory(store, "m1", "feedback", "d", "body")
    assert len(protected) == 1 and protected[0].endswith(".jsonl")


def test_a_failing_audit_write_never_lets_a_denied_write_through(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(write_audit.WriteAudit, "record", boom)
    with as_token("reader"), pytest.raises(WriteDenied):
        _hook(store)


def test_the_chattr_wrapper_runs_sudo_chattr_plus_a_and_swallows_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        raise FileNotFoundError("sudo")

    monkeypatch.setattr(subprocess, "run", fake_run)
    log = tmp_path / "x.jsonl"
    log.write_text("")
    _REAL_PROTECT_FILE(log)
    assert calls and calls[0][:4] == ["sudo", "-n", "chattr", "+a"] and calls[0][-1] == str(log)
