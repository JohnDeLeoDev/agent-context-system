'A table token can be bound to client addresses (the ls-local token is bound to loopback).'
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_context import server, token_table, write_audit, write_guard

SHARED = "the-shared-secret"
LOOPBACK = ["127.0.0.1", "::1"]
REMOTE = "100.111.179.111"


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "observe")
    monkeypatch.setenv("AGENT_CONTEXT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", SHARED)
    monkeypatch.setattr(write_audit, "protect_file", lambda path: None)
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    write_guard.reset_state()
    server._SESSION_LOG_COUNTS.clear()


def _issue(tmp_path: Path, allowed_ips: list[str] | None = None, id: str = "bound") -> str:
    scopes = ["read", "entity-write", "protected-write"]
    if allowed_ips is None:
        return token_table.issue(tmp_path / "tokens.json", id=id, scopes=scopes)
    return token_table.issue(tmp_path / "tokens.json", id=id, scopes=scopes,
                             allowed_ips=allowed_ips)


def _call(bearer: str, peer: str | None = "127.0.0.1", extra: list[tuple[bytes, bytes]] | None = None,
          method: str = "POST") -> tuple[int, int]:
    "(times the app ran, status of the wrapper's own reply or 0)."
    ran: list[int] = []
    sent: list[dict] = []

    async def app(scope, receive, send) -> None:
        ran.append(1)

    async def send(message) -> None:
        sent.append(message)

    headers = [(b"authorization", f"Bearer {bearer}".encode()), *(extra or [])]
    scope: dict = {"type": "http", "method": method, "headers": headers}
    if peer is not None:
        scope["client"] = (peer, 40000)
    asyncio.run(server._counting_app(app)(scope, None, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 0)
    return len(ran), status


def _xff(value: str) -> list[tuple[bytes, bytes]]:
    return [(b"x-forwarded-for", value.encode())]


def _denies(tmp_path: Path) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((tmp_path / "audit").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["decision"] == "deny"]





def test_issue_stores_the_allowed_addresses_in_canonical_form(tmp_path: Path) -> None:
    _issue(tmp_path, ["127.0.0.1", "::ffff:127.0.0.1", "0:0:0:0:0:0:0:1"])
    entry = json.loads((tmp_path / "tokens.json").read_text())["tokens"][0]
    assert entry["allowed_ips"] == ["127.0.0.1", "::1"]


@pytest.mark.parametrize("bad", [["banana"], ["127.0.0.1", ""], ["10.0.0.0/8"], "127.0.0.1", [5]])
def test_an_invalid_allowed_ips_entry_is_refused_and_nothing_is_written(
        tmp_path: Path, bad) -> None:
    with pytest.raises(ValueError, match="allowed_ips"):
        token_table.issue(tmp_path / "tokens.json", id="x", scopes=["read"], allowed_ips=bad)
    assert not (tmp_path / "tokens.json").exists()


def test_a_token_without_allowed_ips_has_none(tmp_path: Path) -> None:
    secret = _issue(tmp_path)
    info = token_table.verify(secret)
    assert info is not None and info.allowed_ips == ()
    assert "allowed_ips" not in json.loads((tmp_path / "tokens.json").read_text())["tokens"][0]


def test_verify_and_lookup_carry_the_allowed_addresses(tmp_path: Path) -> None:
    secret = _issue(tmp_path, LOOPBACK)
    for info in (token_table.verify(secret), token_table.lookup("bound")):
        assert info is not None and info.allowed_ips == ("127.0.0.1", "::1")
        assert "protected-write" in info.scopes





@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"])
def test_a_direct_loopback_caller_passes(tmp_path: Path, peer: str) -> None:
    assert _call(_issue(tmp_path, LOOPBACK), peer=peer) == (1, 0)


def test_a_replay_through_nginx_from_another_address_is_refused(tmp_path: Path) -> None:
    ran, status = _call(_issue(tmp_path, LOOPBACK), peer="127.0.0.1", extra=_xff(REMOTE))
    assert (ran, status) == (0, 403)
    row = _denies(tmp_path)[0]
    assert row["token_id"] == "bound" and row["ip"] == REMOTE and row["op"] == "request"


def test_the_loopback_peer_of_nginx_does_not_make_a_forwarded_client_local(
        tmp_path: Path) -> None:
    assert _call(_issue(tmp_path, LOOPBACK), peer="127.0.0.1",
                 extra=_xff("100.111.179.111"))[0] == 0
    assert _call(_issue(tmp_path, LOOPBACK, id="b2"), peer="::1", extra=_xff(REMOTE))[0] == 0


def test_a_forwarded_loopback_address_from_a_non_loopback_peer_is_judged_by_the_peer(
        tmp_path: Path) -> None:
    ran, status = _call(_issue(tmp_path, LOOPBACK), peer="203.0.113.5", extra=_xff("127.0.0.1"))
    assert (ran, status) == (0, 403)


def test_a_forwarded_chain_passes_only_if_every_entry_is_allowed(tmp_path: Path) -> None:
    secret = _issue(tmp_path, LOOPBACK)
    assert _call(secret, extra=_xff("127.0.0.1, ::1"))[0] == 1
    assert _call(secret, extra=_xff("127.0.0.1, 100.111.179.111"))[0] == 0
    assert _call(secret, extra=_xff("100.111.179.111, 127.0.0.1"))[0] == 0
    assert _call(secret, extra=_xff("127.0.0.1, banana"))[0] == 0


def test_a_real_ip_header_from_another_address_is_refused(tmp_path: Path) -> None:
    assert _call(_issue(tmp_path, LOOPBACK), extra=[(b"x-real-ip", REMOTE.encode())])[0] == 0


def test_a_missing_or_unparsable_address_fails_closed(tmp_path: Path) -> None:
    secret = _issue(tmp_path, LOOPBACK)
    assert _call(secret, peer=None)[0] == 0
    assert _call(secret, peer="not-an-address")[0] == 0
    assert _call(secret, peer="127.0.0.1", extra=_xff(""))[0] == 1


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
def test_every_method_is_refused_from_another_address(tmp_path: Path, method: str) -> None:
    ran, status = _call(_issue(tmp_path, LOOPBACK), extra=_xff(REMOTE), method=method)
    assert (ran, status) == (0, 403)


def test_a_wider_allowed_list_admits_its_own_addresses(tmp_path: Path) -> None:
    secret = _issue(tmp_path, ["127.0.0.1", REMOTE])
    assert _call(secret, extra=_xff(REMOTE))[0] == 1
    assert _call(secret, extra=_xff("100.111.179.112"))[0] == 0





def test_a_token_without_allowed_ips_is_admitted_from_anywhere(tmp_path: Path) -> None:
    secret = _issue(tmp_path)
    assert _call(secret, extra=_xff(REMOTE))[0] == 1
    assert _call(secret, peer="203.0.113.5")[0] == 1


def test_the_shared_token_is_admitted_from_anywhere() -> None:
    assert _call(SHARED, peer="203.0.113.5", extra=_xff(REMOTE))[0] == 1


def test_a_bad_bearer_is_still_left_to_the_auth_layer(tmp_path: Path) -> None:
    _issue(tmp_path, LOOPBACK)
    assert _call("acx_bound_wrong", extra=_xff(REMOTE))[0] == 1     
    assert _denies(tmp_path) == []
