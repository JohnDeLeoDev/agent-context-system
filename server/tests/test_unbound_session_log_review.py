'Review findings on the session audit lines: bounded volume, bounded size, no false `bound`.'
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_context import server, write_audit, write_guard

SHARED = "the-shared-secret"
NET = (b"x-forwarded-for", b"100.111.179.111")


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
    server._UNBOUND_LINE_TIMES.clear()


def _rows(tmp_path: Path, op: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((tmp_path / "audit").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if op is None or r.get("op") == op]


def _call(headers: list[tuple[bytes, bytes]], client: str = "127.0.0.1") -> int:
    ran: list[int] = []

    async def app(scope, receive, send) -> None:
        ran.append(1)

    async def send(message) -> None:
        pass

    scope = {"type": "http", "method": "POST", "client": (client, 40000), "headers": headers}
    asyncio.run(server._counting_app(app)(scope, None, send))
    return len(ran)


def _auth() -> tuple[bytes, bytes]:
    return (b"authorization", f"Bearer {SHARED}".encode())


def test_fresh_session_ids_cannot_flood_the_unbound_request_lines(tmp_path: Path) -> None:
    for i in range(300):
        assert _call([_auth(), NET, (b"mcp-session-id", f"r{i}".encode())]) == 1
    assert len(_rows(tmp_path, "unbound-request")) <= 120


def test_a_flood_of_header_names_gives_a_small_line(tmp_path: Path) -> None:
    hdrs = [_auth(), NET] + [(f"x-agent-context-{i}".encode(), b"v") for i in range(3000)]
    hdrs += [(b"x-agent-context-same", b"1"), (b"x-agent-context-same", b"2")]
    _call(hdrs)
    line = _rows(tmp_path, "session-init")[0]
    assert len(line["headers"]) <= 20
    assert len(set(line["headers"])) == len(line["headers"])
    assert len(json.dumps(line)) < 4000


def test_a_huge_forwarded_address_is_capped_in_the_line(tmp_path: Path) -> None:
    _call([_auth(), (b"x-forwarded-for", b"9" * 20000)])
    line = _rows(tmp_path, "session-init")[0]
    assert len(line["ip"]) <= 64


def test_a_refused_identity_is_not_logged_as_bound(tmp_path: Path) -> None:
    hdrs = [_auth(), NET, (b"x-agent-context-machine", b"NOT-A-REGISTERED-MACHINE"),
            (b"x-agent-context-machine-id", b"ghost")]
    assert _call(hdrs) == 0                     
    assert [r for r in _rows(tmp_path, "session-init") if r["bound"] is True] == []
