"Edges of the token and write guard work that the main batteries do not pin down.\n\nThe shared token maps to a caller that can never write a protected entity; a request from\nanother host cannot pick its own recorded address; an OAuth-issued token is read only; a\nstream for someone else's session is refused."
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_context import entities, memory, paths, server, token_table, write_audit, write_guard

SHARED = "the-shared-secret"


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "enforce")
    monkeypatch.setenv("AGENT_CONTEXT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", SHARED)
    monkeypatch.delenv("AGENT_CONTEXT_LEGACY_SCOPES", raising=False)
    monkeypatch.setattr(write_audit, "protect_file", lambda path: None)
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    write_guard.reset_state()


def _scope(headers: list[tuple[bytes, bytes]], client: str = "127.0.0.1",
           method: str = "POST") -> dict:
    return {"type": "http", "method": method, "client": (client, 40000), "headers": headers}


def _auth(token: str) -> tuple[bytes, bytes]:
    return (b"authorization", f"Bearer {token}".encode())


def test_forwarded_for_is_ignored_when_the_peer_is_not_loopback() -> None:
    seen: list[write_guard.Caller | None] = []

    async def app(scope, receive, send) -> None:
        seen.append(write_guard.current())

    hdrs = [_auth(SHARED), (b"x-forwarded-for", b"100.64.0.7")]
    asyncio.run(server._counting_app(app)(_scope(hdrs, client="203.0.113.5"), None, None))
    asyncio.run(server._counting_app(app)(_scope(hdrs, client="127.0.0.1"), None, None))
    remote, via_nginx = seen
    assert remote is not None and remote.ip == "203.0.113.5"
    assert via_nginx is not None and via_nginx.ip == "100.64.0.7"


def test_the_shared_token_is_bound_as_legacy_shared_even_with_no_machine_headers(store) -> None:
    outcome: list[str] = []

    async def app(scope, receive, send) -> None:
        who = write_guard.current()
        outcome.append(who.token_id if who else "-")
        try:
            memory.upsert_memory(store, "m1", "feedback", "d", "body")
            outcome.append("memory-ok")
            entities.upsert_hook(store, "h1", event_type="PreToolUse",
                                 script_body="print(1)\n", language="py")
            outcome.append("hook-ok")
        except write_guard.WriteDenied:
            outcome.append("hook-denied")

    asyncio.run(server._counting_app(app)(_scope([_auth(SHARED)]), None, None))
    assert outcome == ["legacy-shared", "memory-ok", "hook-denied"]


def test_the_legacy_scopes_can_be_cut_to_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_LEGACY_SCOPES", "read")
    info = token_table.verify(SHARED)
    assert info is not None and tuple(info.scopes) == ("read",)


def test_the_shared_token_can_never_be_given_the_protected_scope(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_LEGACY_SCOPES", "read,entity-write,protected-write")
    info = token_table.verify(SHARED)
    assert info is not None and "protected-write" not in info.scopes


def test_an_oauth_issued_token_is_bound_as_mobile_oauth_and_is_read_only(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(client_for=lambda t: "agent-context-mobile" if t == "oauth-tok" else None)
    monkeypatch.setattr(server, "_oauth", fake)
    outcome: list[str] = []

    async def app(scope, receive, send) -> None:
        who = write_guard.current()
        outcome.append(who.token_id if who else "-")
        try:
            memory.upsert_memory(store, "m1", "feedback", "d", "body")
            outcome.append("written")
        except write_guard.WriteDenied:
            outcome.append("denied")

    asyncio.run(server._counting_app(app)(_scope([_auth("oauth-tok")]), None, None))
    assert outcome == ["mobile-oauth", "denied"]


def test_the_event_stream_of_another_sessions_token_is_refused() -> None:
    table = token_table.table_path()
    a = token_table.issue(table, id="a", scopes=["read"])
    b = token_table.issue(table, id="b", scopes=["read"])
    called: list[int] = []
    sent: list[dict] = []

    async def app(scope, receive, send) -> None:
        called.append(1)
        if scope["method"] == "POST":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"mcp-session-id", b"sess-9")]})

    async def send(message: dict) -> None:
        sent.append(message)

    wrapped = server._counting_app(app)

    async def run() -> None:
        await wrapped(_scope([_auth(a)]), None, send)
        await wrapped(_scope([_auth(b), (b"mcp-session-id", b"sess-9")], method="GET"),
                      None, send)

    asyncio.run(run())
    assert called == [1]
    assert [m["status"] for m in sent if "status" in m][-1] == 403


def test_a_protected_looking_name_outside_every_store_is_left_alone(tmp_path: Path) -> None:
    bound = write_guard.bind(write_guard.Caller(token_id="ghost"))
    try:
        for name in ("AGENTS.md", "setup.sh", "hook-dispatch.json"):
            paths.write_atomic(tmp_path / "some-project" / name, "x")
            assert (tmp_path / "some-project" / name).read_text() == "x"
    finally:
        write_guard.reset(bound)


def test_a_refused_request_that_cannot_be_audited_still_answers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(write_audit.WriteAudit, "record", boom)
    write_guard.record_refusal("a", "laptop", "1.2.3.4", None, "machine mismatch")


def test_the_default_audit_directory_is_under_the_state_dir(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_AUDIT_DIR")
    assert write_audit.audit_dir().name == "audit-writes"
    assert str(write_audit.audit_dir()).startswith(str(tmp_path))


def test_refusal_lines_are_written_as_deny_with_the_reason(tmp_path: Path) -> None:
    write_guard.record_refusal("a", "laptop", "1.2.3.4", None, "machine mismatch: x")
    rows = [json.loads(line) for f in (tmp_path / "audit").glob("*.jsonl")
            for line in f.read_text().splitlines()]
    assert rows[-1]["decision"] == "deny"
    assert rows[-1]["reason"] == "machine mismatch: x"
    assert rows[-1]["token_id"] == "a"
