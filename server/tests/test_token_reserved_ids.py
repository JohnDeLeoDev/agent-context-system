'Operator-issued tokens cannot take an id the server reserves for its own callers.'
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_context import token_table


def _table(tmp_path: Path) -> Path:
    return tmp_path / "tokens.json"


@pytest.mark.parametrize("reserved", ["mobile-oauth", "legacy-shared", "ls-local"])
def test_a_reserved_id_is_refused_and_nothing_is_written(tmp_path: Path, reserved: str) -> None:
    table = _table(tmp_path)
    token_table.issue(table, id="laptop", scopes=["read"])
    before = table.read_bytes()
    with pytest.raises(ValueError, match="reserved"):
        token_table.issue(table, id=reserved, scopes=["read"])
    assert table.read_bytes() == before


@pytest.mark.parametrize("reserved", ["mobile-oauth", "legacy-shared", "ls-local"])
def test_a_reserved_id_is_refused_on_a_missing_table(tmp_path: Path, reserved: str) -> None:
    table = _table(tmp_path)
    with pytest.raises(ValueError):
        token_table.issue(table, id=reserved, scopes=["read"])
    assert not table.exists()


@pytest.mark.parametrize("spelled", ["Mobile-OAuth", " legacy-shared ", "LS-LOCAL", "ls-local\n"])
def test_a_reserved_id_in_another_spelling_is_refused(tmp_path: Path, spelled: str) -> None:
    table = _table(tmp_path)
    with pytest.raises(ValueError):
        token_table.issue(table, id=spelled, scopes=["read"])
    assert not table.exists()


@pytest.mark.parametrize("ok", ["laptop", "m4", "pc", "ls-protected", "mobile-oauth2",
                                "xmobile-oauth", "legacy-shared2", "ls-local-2"])
def test_any_other_valid_id_is_issued_as_before(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ok: str) -> None:
    table = _table(tmp_path)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(table))
    secret = token_table.issue(table, id=ok, machine_uuid="U", machine_id=ok,
                               scopes=["read", "entity-write"])
    assert secret.startswith(f"{token_table.PREFIX}{ok}_")
    info = token_table.lookup(ok)
    assert info is not None and info.id == ok
    assert stat.S_IMODE(os.stat(table).st_mode) == 0o600
    entry = json.loads(table.read_text())["tokens"][0]
    assert entry["id"] == ok and entry["machine_uuid"] == "U"
    assert entry["scopes"] == ["read", "entity-write"] and entry["revoked"] is False


def test_a_table_that_already_holds_a_reserved_id_still_verifies(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table = _table(tmp_path)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(table))
    token_table.issue(table, id="laptop", scopes=["read"])
    data = json.loads(table.read_text())
    data["tokens"][0]["id"] = "ls-local"          
    table.write_text(json.dumps(data))
    assert token_table.lookup("ls-local") is not None


def test_the_reserved_ids_are_one_constant_built_from_the_servers_own_ids() -> None:
    assert token_table.RESERVED_IDS == frozenset({token_table.LEGACY_ID, token_table.OAUTH_ID,
                                                  "ls-local"})
    assert token_table.LEGACY_ID == "legacy-shared" and token_table.OAUTH_ID == "mobile-oauth"


def test_other_invalid_ids_keep_their_own_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lowercase letters"):
        token_table.issue(_table(tmp_path), id="Bad_Id!", scopes=["read"])


def test_a_duplicate_id_still_says_it_exists(tmp_path: Path) -> None:
    table = _table(tmp_path)
    token_table.issue(table, id="laptop", scopes=["read"])
    with pytest.raises(ValueError, match="already exists"):
        token_table.issue(table, id="laptop", scopes=["read"])
