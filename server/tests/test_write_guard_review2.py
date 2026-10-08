"Findings from the second review of the write guard.\n\n`.git/` was not protected and a doc key could climb into it; a language change let a locked\nscript's file be removed; one oversized audit row broke the hash chain; dry runs used up the\nlive rate quota and leaked store roots; the shared token was one bucket for the whole fleet."
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_context import docs, dryrun, entities, memory, token_table, write_audit, write_guard
from agent_context.write_guard import Caller, WriteDenied


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "enforce")
    monkeypatch.setenv("AGENT_CONTEXT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_FREE_WRITES_PER_HOUR", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_DENIALS_BEFORE_SUSPEND", raising=False)
    monkeypatch.setattr(write_audit, "protect_file", lambda path: None)
    monkeypatch.setattr(write_guard, "alert", lambda text: None)
    write_guard.reset_state()
    token_table.issue(tmp_path / "tokens.json", id="writer", scopes=["read", "entity-write"])
    token_table.issue(tmp_path / "tokens.json", id="admin",
                      scopes=["read", "entity-write", "protected-write"])


def _as(token_id: str, machine: str | None = None):
    return write_guard.bind(Caller(token_id=token_id, machine_id=machine, ip="1.2.3.4",
                                   session_key="s"))





@pytest.mark.parametrize("rel", [".git/config", ".git/hooks/pre-commit", ".agents/worktrees/x/y"])
def test_git_and_agents_directories_are_protected_paths(rel: str) -> None:
    assert write_guard.classify_path(rel) is True


def test_a_doc_key_cannot_climb_into_dot_git(store) -> None:
    target = Path(store.root) / ".git" / "config"
    bound = _as("writer")
    try:
        with pytest.raises(WriteDenied):
            docs.upsert_doc(store, "../../.git/config", body="[core]\n fsmonitor = x\n")
    finally:
        write_guard.reset(bound)
    assert not target.exists()


@pytest.mark.parametrize("key", ["../escape.md", "a/../../escape.md", "/abs/escape.md"])
def test_a_key_with_traversal_or_an_absolute_path_is_refused(store, key: str) -> None:
    bound = _as("writer")
    try:
        with pytest.raises(WriteDenied) as info:
            docs.upsert_doc(store, key, body="x")
    finally:
        write_guard.reset(bound)
    assert "traversal" in str(info.value).lower() or "path" in str(info.value).lower()


def test_a_skill_name_with_traversal_is_refused(store) -> None:
    bound = _as("writer")
    try:
        with pytest.raises(WriteDenied):
            entities.upsert_skill(store, "../x", "d", body="b")
    finally:
        write_guard.reset(bound)


def test_ordinary_nested_doc_keys_still_work(store) -> None:
    bound = _as("writer")
    try:
        docs.upsert_doc(store, "guides/deep/a.md", body="x")
    finally:
        write_guard.reset(bound)
    assert docs.get_doc(store, "guides/deep/a.md") is not None


@pytest.mark.parametrize("rel", ["Global/Hooks/h.py", "GLOBAL/SCRIPTS/s.py", "agents.md",
                                 "Server/src/x.py", ".GIT/config"])
def test_protected_paths_match_whatever_the_case(rel: str) -> None:
    assert write_guard.classify_path(rel) is True





def test_a_locked_script_cannot_be_removed_by_changing_its_language(store) -> None:
    body = "def test_a():\n    assert True\n"
    entities.upsert_script(store, "test-lock1", script_body=body, language="py")
    locks = Path(store.root) / "global" / "test-locks"
    locks.mkdir(parents=True, exist_ok=True)
    import hashlib
    (locks / "global__scripts__test-lock1.py.json").write_text(json.dumps({
        "path": "global/scripts/test-lock1.py",
        "sha256": hashlib.sha256(body.encode()).hexdigest()}))
    bound = _as("admin")
    try:
        with pytest.raises(WriteDenied) as info:
            entities.upsert_script(store, "test-lock1", script_body="echo 2\n", language="sh")
    finally:
        write_guard.reset(bound)
    assert "lock" in str(info.value).lower()
    assert (Path(store.root) / "global" / "scripts" / "test-lock1.py").exists()





def test_an_oversized_row_does_not_break_the_chain(tmp_path: Path) -> None:
    audit = write_audit.WriteAudit(tmp_path / "chain")
    audit.record({"note": "first"})
    audit.record({"blob": "x" * 40_000})
    audit.record({"note": "third"})
    audit.record({"note": "fourth"})
    log = next((tmp_path / "chain").glob("*.jsonl"))
    assert write_audit.verify_chain(log) == (True, None)


def test_a_huge_key_is_capped_in_the_audit_line(store, tmp_path: Path) -> None:
    bound = _as("writer")
    try:
        write_guard.check_entity(store.root, "upsert", "memory", "k" * 30_000, "global",
                                 body="x")
    finally:
        write_guard.reset(bound)
    line = next((tmp_path / "audit").glob("*.jsonl")).read_text().splitlines()[0]
    assert len(line) < 3000


def test_the_append_only_bit_is_set_outside_the_audit_lock(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit = write_audit.WriteAudit(tmp_path / "locked")
    held: list[bool] = []
    monkeypatch.setattr(write_audit, "protect_file",
                        lambda path: held.append(audit._lock.locked()))
    audit.record({"note": "first"})
    assert held == [False]





def _dry_memory(store, slug: str) -> None:
    dryrun.run(store, lambda: json.dumps(memory.upsert_memory(
        dryrun.active_store(), slug, "feedback", "d", "b"), default=str))


def test_dry_runs_do_not_use_up_the_live_rate_quota(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_FREE_WRITES_PER_HOUR", "5")
    bound = _as("writer")
    try:
        for i in range(8):
            _dry_memory(store, f"dry{i}")
        memory.upsert_memory(store, "real", "feedback", "d", "b")
    finally:
        write_guard.reset(bound)
    assert memory.get_memory(store, "real") is not None


def test_dry_runs_do_not_leave_store_roots_registered(store) -> None:
    before = write_guard.registered_root_count()
    bound = _as("writer")
    try:
        for i in range(4):
            _dry_memory(store, f"dry{i}")
    finally:
        write_guard.reset(bound)
    assert write_guard.registered_root_count() == before





def test_one_machine_using_the_shared_token_cannot_suspend_the_others(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "the-shared-secret")
    write_guard.reset_state()
    bound = _as("legacy-shared", "laptop")
    try:
        for i in range(5):
            with pytest.raises(WriteDenied):
                entities.upsert_hook(store, f"h{i}", event_type="PreToolUse",
                                     script_body="print(1)\n", language="py")
        with pytest.raises(WriteDenied) as info:
            memory.upsert_memory(store, "m1", "feedback", "d", "b")
        assert "suspend" in str(info.value).lower()
    finally:
        write_guard.reset(bound)
    other = _as("legacy-shared", "rp")
    try:
        memory.upsert_memory(store, "m2", "feedback", "d", "b")
    finally:
        write_guard.reset(other)
    assert memory.get_memory(store, "m2") is not None


def test_a_table_token_is_still_one_bucket(store) -> None:
    for machine in ("laptop", "laptop", "laptop", "laptop", "laptop"):
        bound = _as("writer", machine)
        try:
            with pytest.raises(WriteDenied):
                entities.upsert_hook(store, "h", event_type="PreToolUse",
                                     script_body="print(1)\n", language="py")
        finally:
            write_guard.reset(bound)
    assert write_guard.is_suspended("writer")





def test_an_unknown_shebang_falls_back_to_the_language_field(store) -> None:
    bound = _as("admin")
    try:
        with pytest.raises(WriteDenied):
            entities.upsert_hook(store, "hn", event_type="PreToolUse",
                                 script_body="#!/usr/bin/env node\nfunction (((\n",
                                 language="py")
    finally:
        write_guard.reset(bound)


def test_a_body_that_is_not_valid_text_is_denied_not_crashed(store) -> None:
    bound = _as("admin")
    try:
        with pytest.raises(WriteDenied):
            entities.upsert_script(store, "s", script_body="x = 1  # \ud800\n", language="py")
    finally:
        write_guard.reset(bound)
