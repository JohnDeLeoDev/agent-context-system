'Per-machine bearer tokens: a table on ls, hashed at rest, checked on every request.\n\nCriteria 1, 2, 3 and 18 of the steps 0 and 1 plan (write-guard). The shared\nAGENT_CONTEXT_TOKEN keeps working during the migration as `legacy-shared`.'
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from agent_context import token_table

MACHINE = "AAAAAAAA-1111-2222-3333-LAPTOPLAPTOP"


@pytest.fixture
def table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "tokens.json"
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(path))
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    return path


def _issue(table: Path, **kw) -> str:
    kw.setdefault("id", "laptop")
    kw.setdefault("machine_uuid", MACHINE)
    kw.setdefault("machine_id", "laptop")
    kw.setdefault("scopes", ["read", "entity-write"])
    return token_table.issue(table, **kw)





def test_valid_token_resolves_to_its_entry(table: Path) -> None:
    secret = _issue(table)
    info = token_table.verify(secret)
    assert info is not None
    assert info.id == "laptop"
    assert info.machine_uuid == MACHINE
    assert info.machine_id == "laptop"
    assert set(info.scopes) == {"read", "entity-write"}


def test_token_has_the_documented_shape(table: Path) -> None:
    secret = _issue(table, id="m4")
    assert secret.startswith("acx_m4_")
    assert len(secret) > len("acx_m4_") + 32


@pytest.mark.parametrize("bad", [
    "", "nope", "acx_", "acx_laptop", "acx_laptop_", "acx_ghost_abc",
    "Bearer acx_laptop_x", "acx_laptop_" + "x" * 10,
])
def test_unknown_or_malformed_tokens_are_refused(table: Path, bad: str) -> None:
    _issue(table)
    assert token_table.verify(bad) is None


def test_wrong_secret_for_a_real_id_is_refused(table: Path) -> None:
    secret = _issue(table)
    assert token_table.verify(secret[:-1] + ("a" if secret[-1] != "a" else "b")) is None


def test_revoked_token_is_refused_on_the_next_call_without_a_restart(table: Path) -> None:
    secret = _issue(table)
    assert token_table.verify(secret) is not None
    token_table.revoke(table, "laptop")
    assert token_table.verify(secret) is None
    assert token_table.lookup("laptop") is None


def test_expired_token_is_refused(table: Path) -> None:
    secret = _issue(table, expires=time.time() - 5)
    assert token_table.verify(secret) is None


def test_unexpired_token_is_accepted(table: Path) -> None:
    secret = _issue(table, expires=time.time() + 3600)
    assert token_table.verify(secret) is not None


def test_edits_to_the_file_are_picked_up_by_mtime(table: Path) -> None:
    secret = _issue(table)
    assert token_table.verify(secret) is not None
    data = json.loads(table.read_text())
    data["tokens"][0]["scopes"] = ["read"]
    table.write_text(json.dumps(data))
    os.utime(table, (time.time() + 5, time.time() + 5))
    info = token_table.verify(secret)
    assert info is not None and tuple(info.scopes) == ("read",)


def test_lookup_by_id_is_live(table: Path) -> None:
    _issue(table)
    info = token_table.lookup("laptop")
    assert info is not None and info.machine_uuid == MACHINE
    assert token_table.lookup("ghost") is None





def test_legacy_shared_token_still_works_with_read_and_entity_write(
        table: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "the-shared-secret")
    info = token_table.verify("the-shared-secret")
    assert info is not None
    assert info.id == "legacy-shared"
    assert set(info.scopes) == {"read", "entity-write"}
    assert "protected-write" not in info.scopes
    assert token_table.verify("not-the-shared-secret") is None


def test_legacy_and_table_tokens_work_side_by_side(
        table: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "the-shared-secret")
    secret = _issue(table)
    assert token_table.verify("the-shared-secret").id == "legacy-shared"  
    assert token_table.verify(secret).id == "laptop"  


def test_missing_table_means_only_the_legacy_token_can_work(
        table: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not table.exists()
    assert token_table.verify("acx_laptop_whatever") is None
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "the-shared-secret")
    assert token_table.verify("the-shared-secret") is not None


def test_corrupt_table_grants_nothing_and_does_not_raise(table: Path) -> None:
    secret = _issue(table)
    table.write_text("{not json")
    os.utime(table, (time.time() + 5, time.time() + 5))
    assert token_table.verify(secret) is None


def test_auth_is_required_when_a_table_exists_or_a_legacy_token_is_set(
        table: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert token_table.auth_required() is False
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "x")
    assert token_table.auth_required() is True
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN")
    _issue(table)
    assert token_table.auth_required() is True





def test_the_table_holds_a_hash_and_never_the_secret(table: Path) -> None:
    secret = _issue(table)
    raw = table.read_text()
    assert secret not in raw
    assert secret.split("_", 2)[2] not in raw
    entry = json.loads(raw)["tokens"][0]
    assert len(entry["sha256"]) == 64
    assert entry["id"] == "laptop"


def test_the_table_file_is_private(table: Path) -> None:
    _issue(table)
    assert (table.stat().st_mode & 0o777) == 0o600


def test_duplicate_ids_are_refused(table: Path) -> None:
    _issue(table)
    with pytest.raises(ValueError):
        _issue(table)


def test_unknown_scope_is_refused(table: Path) -> None:
    with pytest.raises(ValueError):
        _issue(table, scopes=["read", "root"])





def test_the_mobile_oauth_client_is_read_only() -> None:
    info = token_table.oauth_info("agent-context-mobile")
    assert info.id == "mobile-oauth"
    assert tuple(info.scopes) == ("read",)


def test_any_other_oauth_client_is_read_only_too() -> None:
    assert tuple(token_table.oauth_info("someone-else").scopes) == ("read",)


def test_the_fastmcp_verifier_returns_the_token_id_and_scopes(table: Path) -> None:
    secret = _issue(table)
    verifier = token_table.TableTokenVerifier()
    got = asyncio.run(verifier.verify_token(secret))
    assert got is not None
    assert got.client_id == "laptop"
    assert set(got.scopes) == {"read", "entity-write"}
    assert asyncio.run(verifier.verify_token("acx_laptop_nope")) is None
