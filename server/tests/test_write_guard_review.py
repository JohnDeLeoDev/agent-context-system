'Findings from the fresh review of the write guard.\n\n`delete_project` removed project.toml with `os.remove` and never reached `Store.delete`, so\na read-only token could delete a project record through `delete_entity`.'
from __future__ import annotations

from pathlib import Path

import pytest

from agent_context import fstools as T
from agent_context import generic, token_table, write_audit, write_guard
from agent_context.write_guard import WriteDenied


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "enforce")
    monkeypatch.setenv("AGENT_CONTEXT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    monkeypatch.setattr(write_audit, "protect_file", lambda path: None)
    write_guard.reset_state()
    token_table.issue(tmp_path / "tokens.json", id="reader", scopes=["read"])
    token_table.issue(tmp_path / "tokens.json", id="writer", scopes=["read", "entity-write"])


def _project(store) -> Path:
    T.upsert_project(store, "git@github.com:acme/api.git", "example-api")
    return Path(store.root) / "projects" / "example-api" / "project.toml"


def test_a_read_only_token_cannot_delete_a_project_record(store) -> None:
    record = _project(store)
    assert record.exists()
    bound = write_guard.bind(write_guard.Caller(token_id="reader"))
    try:
        with pytest.raises(WriteDenied):
            generic.delete_entity(store, "project", "example-api")
    finally:
        write_guard.reset(bound)
    assert record.exists()
    assert any(e.get("display_name") == "example-api" for e in store.entities.values())


def test_an_entity_write_token_can_delete_a_project_record(store) -> None:
    record = _project(store)
    bound = write_guard.bind(write_guard.Caller(token_id="writer"))
    try:
        generic.delete_entity(store, "project", "example-api")
    finally:
        write_guard.reset(bound)
    assert not record.exists()


def test_a_system_caller_can_delete_a_project_record(store) -> None:
    record = _project(store)
    generic.delete_entity(store, "project", "example-api")
    assert not record.exists()


def test_a_denied_project_delete_is_audited(store, tmp_path: Path) -> None:
    _project(store)
    bound = write_guard.bind(write_guard.Caller(token_id="reader"))
    try:
        with pytest.raises(WriteDenied):
            generic.delete_entity(store, "project", "example-api")
    finally:
        write_guard.reset(bound)
    text = "".join(f.read_text() for f in (tmp_path / "audit").glob("*.jsonl"))
    assert '"kind":"project"' in text and '"decision":"deny"' in text
