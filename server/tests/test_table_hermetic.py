'No test reads the real token table of the machine it runs on.\n\nA daemon host has a real table at ~/.config/agent-context/tokens.json. With it present,\n`token_table.auth_required()` is true, so tests that expect "no auth configured" failed with 401\non that host and blocked every commit there. The suite points AGENT_CONTEXT_TOKEN_TABLE at a\npath that does not exist for every test; a test that needs a table sets its own path.'
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_context import token_table

REAL_TABLE = Path.home() / ".config" / "agent-context" / "tokens.json"


def test_the_table_path_is_not_the_real_default() -> None:
    path = token_table.table_path()
    assert path != REAL_TABLE
    assert Path(os.environ["AGENT_CONTEXT_TOKEN_TABLE"]) == path


def test_the_default_test_table_does_not_exist(tmp_path: Path) -> None:
    path = token_table.table_path()
    assert not path.exists()
    assert path.parent == tmp_path and path.name == "no-such-token-table.json"


def test_no_table_means_no_auth_when_no_shared_token_is_set(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    assert token_table.auth_required() is False


def test_unsetting_the_variable_does_not_reach_the_real_table(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    "A fixture teardown that deletes the variable and reloads the server used to read the\n    machine's real table and leave auth on for every later test."
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN_TABLE", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    assert token_table.table_path() != REAL_TABLE
    assert token_table.table_path().parent == tmp_path
    assert token_table.auth_required() is False


def test_reloading_the_server_with_the_variable_unset_leaves_auth_off(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from agent_context import server
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN_TABLE", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    module = importlib.reload(server)
    assert module.mcp.settings.auth is None


def test_a_test_can_still_use_its_own_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    own = tmp_path / "mine.json"
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(own))
    secret = token_table.issue(own, id="t1", scopes=["read"])
    assert token_table.verify(secret) is not None
    assert token_table.auth_required() is True


def test_a_real_table_on_this_host_cannot_change_the_result(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    assert token_table.table_path() != REAL_TABLE
    assert token_table.auth_required() is False
