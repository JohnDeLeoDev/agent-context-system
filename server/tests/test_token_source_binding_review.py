'Review findings on the address binding: table edits fail closed, parsing is strict, the audit\nnames the offending address, and repeated headers are all read.'
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_context import server, token_table, write_audit, write_guard

SHARED = "the-shared-secret"
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


def _bound(tmp_path: Path) -> str:
    return token_table.issue(tmp_path / "tokens.json", id="bound", scopes=["read"],
                             allowed_ips=["127.0.0.1", "::1"])


def _call(bearer: str, peer: str = "127.0.0.1",
          extra: list[tuple[bytes, bytes]] | None = None) -> int:
    ran: list[int] = []

    async def app(scope, receive, send) -> None:
        ran.append(1)

    async def send(message) -> None:
        pass

    scope = {"type": "http", "method": "POST", "client": (peer, 40000),
             "headers": [(b"authorization", f"Bearer {bearer}".encode()), *(extra or [])]}
    asyncio.run(server._counting_app(app)(scope, None, send))
    return len(ran)


def _denies(tmp_path: Path) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((tmp_path / "audit").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["decision"] == "deny"]





@pytest.mark.parametrize("value", [[], None, 0, "", {}, False, "127.0.0.1", ["banana"], [5]])
def test_a_table_entry_that_names_addresses_badly_is_bound_to_none(
        tmp_path: Path, value) -> None:
    secret = _bound(tmp_path)
    data = json.loads((tmp_path / "tokens.json").read_text())
    data["tokens"][0]["allowed_ips"] = value
    (tmp_path / "tokens.json").write_text(json.dumps(data))
    info = token_table.verify(secret)
    assert info is not None and info.allowed_ips == ("-",)
    assert _call(secret) == 0


def test_a_table_entry_without_the_field_stays_unrestricted(tmp_path: Path) -> None:
    secret = token_table.issue(tmp_path / "tokens.json", id="open", scopes=["read"])
    info = token_table.verify(secret)
    assert info is not None and info.allowed_ips == ()
    assert _call(secret, extra=[(b"x-forwarded-for", REMOTE.encode())]) == 1





@pytest.mark.parametrize("text", ["[::1]junk", "[127.0.0.1]evil", "127.0.0.1:abc", "127.0.0.1%x",
                                  "fe80::1%lo", "[::1", "", "  ", "1.2.3", "::1::1"])
def test_malformed_address_text_is_not_an_address(text: str) -> None:
    assert token_table.normalize_ip(text) is None


@pytest.mark.parametrize("text,canonical", [
    ("127.0.0.1", "127.0.0.1"), ("127.0.0.1:5000", "127.0.0.1"), ("[::1]", "::1"),
    ("[::1]:8765", "::1"), ("::1", "::1"), ("::ffff:127.0.0.1", "127.0.0.1"),
    ("0:0:0:0:0:0:0:1", "::1"), ("localhost", "127.0.0.1"), ("localhost:80", "127.0.0.1"),
    (" 100.111.179.111 ", "100.111.179.111")])
def test_valid_spellings_normalize(text: str, canonical: str) -> None:
    assert token_table.normalize_ip(text) == canonical


def test_issue_refuses_lenient_spellings(tmp_path: Path) -> None:
    for bad in ("[::1]junk", "127.0.0.1:abc", "127.0.0.1%x"):
        with pytest.raises(ValueError, match="allowed_ips"):
            token_table.issue(tmp_path / "tokens.json", id="x", scopes=["read"],
                              allowed_ips=[bad])
    assert not (tmp_path / "tokens.json").exists()


def test_a_forwarded_name_is_not_an_address(tmp_path: Path) -> None:
    secret = _bound(tmp_path)
    assert _call(secret, extra=[(b"x-forwarded-for", b"localhost")]) == 0
    assert _call(secret, extra=[(b"x-forwarded-for", b"127.0.0.1")]) == 1





def test_every_forwarded_for_header_is_read(tmp_path: Path) -> None:
    secret = _bound(tmp_path)
    assert _call(secret, extra=[(b"x-forwarded-for", b"127.0.0.1"),
                                (b"x-forwarded-for", REMOTE.encode())]) == 0
    assert _call(secret, extra=[(b"X-Forwarded-For", REMOTE.encode()),
                                (b"x-forwarded-for", b"127.0.0.1")]) == 0


def test_every_real_ip_header_is_read(tmp_path: Path) -> None:
    secret = _bound(tmp_path)
    assert _call(secret, extra=[(b"x-real-ip", b"127.0.0.1"),
                                (b"x-real-ip", REMOTE.encode())]) == 0





def test_the_deny_row_names_the_offending_address(tmp_path: Path) -> None:
    secret = _bound(tmp_path)
    assert _call(secret, extra=[(b"x-forwarded-for", b"127.0.0.1, 100.111.179.111")]) == 0
    rows = _denies(tmp_path)
    assert rows and rows[-1]["ip"] == "100.111.179.111"


def test_an_unusual_loopback_spelling_of_the_peer_still_defers_to_the_chain(
        tmp_path: Path) -> None:
    secret = _bound(tmp_path)
    assert _call(secret, peer="0:0:0:0:0:0:0:1",
                 extra=[(b"x-forwarded-for", REMOTE.encode())]) == 0
    assert _denies(tmp_path)[-1]["ip"] == REMOTE
