'The token, the machine and the write guard, end to end through the HTTP app.\n\nCriteria 4 to 7 of the steps 0 and 1 plan (write-guard). The ASGI wrapper resolves the\nbearer to a token id, binds it for the session task, refuses a header that names another\nmachine, and pins each MCP session to the token that opened it. A tool call made over HTTP\nmust reach the write guard with that token, which is the assumption the whole design rests on.'
from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from starlette.testclient import TestClient

from agent_context import entities, identity, memory, token_table, write_audit, write_guard
from agent_context.store import emit_toml, stable_uuid

MACHINE = "AAAAAAAA-1111-2222-3333-LAPTOPLAPTOP"
OTHER_MACHINE = "BBBBBBBB-1111-2222-3333-SOMEOTHERONE"
MCP = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}


def _add_machine(store, uuid: str, machine_id: str) -> None:
    meta = {"type": "machine", "machine_uuid": uuid, "hostname": machine_id, "platform": "linux",
            "home_dir": "/home/x", "display_name": machine_id, "machine_id": machine_id}
    path = Path(store.root) / "machines" / f"{uuid}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_toml(meta))
    meta["scope"] = "global"
    meta["uuid"] = stable_uuid("machine", "global", f"{uuid}.toml")
    with store.lock:
        store._index(meta, str(path))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store) -> dict[str, str]:
    "A table with three machines' tokens, the guard enforcing, and a real store behind it."
    table = tmp_path / "tokens.json"
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(table))
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "enforce")
    monkeypatch.setenv("AGENT_CONTEXT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    monkeypatch.setattr(write_audit, "protect_file", lambda path: None)
    write_guard.reset_state()
    secrets = {
        "laptop": token_table.issue(table, id="laptop", machine_uuid=MACHINE, machine_id="laptop",
                               scopes=["read", "entity-write"]),
        "rp": token_table.issue(table, id="rp", machine_uuid=OTHER_MACHINE, machine_id="rp",
                           scopes=["read"]),
    }
    _add_machine(store, MACHINE, "laptop")
    _add_machine(store, OTHER_MACHINE, "rp")
    return secrets


@pytest.fixture
def srv(env: dict[str, str], store, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    'agent_context.server rebuilt under the table, so `mcp` has auth on.'
    from agent_context import server
    module = importlib.reload(server)
    monkeypatch.setattr(module, "_get_conn", lambda: store)
    yield module
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN_TABLE", raising=False)
    importlib.reload(server)


@pytest.fixture
def client(srv: ModuleType) -> Iterator[TestClient]:
    app = srv._counting_app(srv.mcp.streamable_http_app())
    
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as c:
        yield c


def _bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def _audit(tmp_path: Path) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((tmp_path / "audit").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return rows


def _data(response) -> dict:
    'The JSON-RPC message in an SSE or plain JSON response.'
    text = response.text
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(text)


def _open_session(client: TestClient, token: str, **extra: str) -> dict[str, str]:
    headers = {**MCP, **_bearer(token, **extra)}
    first = client.post("/mcp", json=INIT, headers=headers)
    assert first.status_code == 200, first.text
    sid = first.headers["mcp-session-id"]
    headers = {**headers, "mcp-session-id": sid}
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers)
    return headers


def _call(client: TestClient, headers: dict[str, str], name: str, arguments: dict) -> dict:
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    return _data(client.post("/mcp", json=body, headers=headers))


def _hook_body(store) -> str:
    entry = entities.get_hook(store, "h1")
    assert entry is not None
    return str(entry["script_body"])


def _text(message: dict) -> str:
    result = message.get("result") or {}
    parts = [c.get("text", "") for c in result.get("content", [])]
    return " ".join(parts) or json.dumps(message.get("error") or message)





def test_a_relay_that_sends_only_the_bearer_can_initialize_and_read(
        client: TestClient, env: dict[str, str]) -> None:
    assert client.post("/mcp", json=INIT, headers={**MCP, **_bearer(env["laptop"])}
                       ).status_code == 200
    headers = _open_session(client, env["laptop"])
    reply = _call(client, headers, "get_materialized", {})
    assert "error" not in reply, reply


def test_bad_or_missing_tokens_get_401_on_mcp(
        client: TestClient, env: dict[str, str]) -> None:
    for headers in ({}, _bearer("acx_laptop_wrong"), _bearer("nope")):
        assert client.post("/mcp", json=INIT, headers={**MCP, **headers}).status_code == 401


def test_a_revoked_token_gets_401_on_the_next_request_without_a_restart(
        client: TestClient, env: dict[str, str], tmp_path: Path) -> None:
    headers = _bearer(env["laptop"])
    session_headers = _open_session(client, env["laptop"])
    reply = _call(client, session_headers, "get_materialized", {})
    assert "error" not in reply, reply
    token_table.revoke(tmp_path / "tokens.json", "laptop")
    assert client.post("/mcp", json=INIT, headers={**MCP, **headers}).status_code == 401





def _scope(headers: list[tuple[bytes, bytes]]) -> dict:
    return {"type": "http", "method": "POST", "client": ("127.0.0.1", 40000),
            "headers": headers}


def _h(token: str | None = None, machine: str | None = None, session: str | None = None,
       forwarded: str | None = None) -> list[tuple[bytes, bytes]]:
    out: list[tuple[bytes, bytes]] = []
    if token:
        out.append((b"authorization", f"Bearer {token}".encode()))
    if machine:
        out.append((identity.HEADER_MACHINE.encode(), machine.encode()))
    if session:
        out.append((b"mcp-session-id", session.encode()))
    if forwarded:
        out.append((b"x-forwarded-for", forwarded.encode()))
    return out


def test_the_wrapper_binds_the_token_the_machine_and_the_client_ip(
        srv: ModuleType, env: dict[str, str]) -> None:
    seen: list[write_guard.Caller | None] = []

    async def app(scope, receive, send) -> None:
        seen.append(write_guard.current())

    async def run() -> None:
        await asyncio.create_task(srv._counting_app(app)(
            _scope(_h(env["laptop"], forwarded="100.64.0.7, 10.0.0.1")), None, None))
        await asyncio.create_task(srv._counting_app(app)(_scope(_h()), None, None))

    asyncio.run(run())
    bound, unbound = seen
    assert bound is not None
    assert (bound.token_id, bound.machine_id, bound.ip) == ("laptop", "laptop", "100.64.0.7")
    assert bound.session_key
    assert unbound is None


def test_a_header_naming_another_machine_is_refused_403_and_audited(
        srv: ModuleType, env: dict[str, str], tmp_path: Path) -> None:
    called: list[int] = []
    sent: list[dict] = []

    async def app(scope, receive, send) -> None:
        called.append(1)

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(srv._counting_app(app)(
        _scope(_h(env["laptop"], machine=OTHER_MACHINE)), None, send))
    assert called == []
    assert sent[0]["status"] == 403
    rows = _audit(tmp_path)
    assert rows and rows[-1]["decision"] == "deny" and rows[-1]["token_id"] == "laptop"
    assert "machine" in rows[-1]["reason"].lower()


def test_a_header_naming_the_tokens_own_machine_is_accepted(
        srv: ModuleType, env: dict[str, str]) -> None:
    called: list[int] = []

    async def app(scope, receive, send) -> None:
        called.append(1)

    asyncio.run(srv._counting_app(app)(_scope(_h(env["laptop"], machine=MACHINE)), None, None))
    assert called == [1]


def test_a_session_is_pinned_to_the_token_that_opened_it(
        srv: ModuleType, env: dict[str, str], tmp_path: Path) -> None:
    calls: list[str] = []
    sent: list[dict] = []

    async def app(scope, receive, send) -> None:
        who = write_guard.current()
        calls.append(who.token_id if who else "-")
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"mcp-session-id", b"sess-1")]})

    async def send(message: dict) -> None:
        sent.append(message)

    wrapped = srv._counting_app(app)

    async def run() -> None:
        await wrapped(_scope(_h(env["laptop"])), None, send)                       
        await wrapped(_scope(_h(env["laptop"], session="sess-1")), None, send)     
        await wrapped(_scope(_h(env["rp"], session="sess-1")), None, send)         

    asyncio.run(run())
    assert calls == ["laptop", "laptop"]
    assert [m["status"] for m in sent if "status" in m][-1] == 403
    rows = _audit(tmp_path)
    assert rows[-1]["decision"] == "deny" and "session" in rows[-1]["reason"].lower()





def test_a_read_only_machine_cannot_write_a_hook_over_http(
        client: TestClient, env: dict[str, str], store) -> None:
    headers = _open_session(client, env["rp"])
    reply = _call(client, headers, "upsert_hook", {
        "name": "evil", "event_type": "SessionStart", "script_body": "print(1)\n",
        "language": "py"})
    assert "denied" in _text(reply).lower()
    assert entities.get_hook(store, "evil") is None


def test_an_entity_write_machine_cannot_write_a_hook_but_can_write_a_memory(
        client: TestClient, env: dict[str, str], store, tmp_path: Path) -> None:
    headers = _open_session(client, env["laptop"], **{"X-Forwarded-For": "100.64.0.7"})
    denied = _call(client, headers, "upsert_hook", {
        "name": "evil", "event_type": "SessionStart", "script_body": "print(1)\n",
        "language": "py"})
    assert "denied" in _text(denied).lower()
    assert entities.get_hook(store, "evil") is None
    ok = _call(client, headers, "upsert_memory", {
        "slug": "from-laptop", "memory_type": "feedback", "description": "d", "body": "hello",
        "load_behavior": "lazy"})
    assert "denied" not in _text(ok).lower()
    assert memory.get_memory(store, "from-laptop") is not None
    rows = _audit(tmp_path)
    allow = next(r for r in rows if r["decision"] == "allow" and r["key"] == "from-laptop")
    assert (allow["token_id"], allow["machine_id"], allow["ip"]) == (
        "laptop", "laptop", "100.64.0.7")
    assert any(r["decision"] == "deny" and r["kind"] == "hook" for r in rows)


def test_bulk_edit_and_edit_body_are_guarded_over_http_too(
        client: TestClient, env: dict[str, str], store) -> None:
    entities.upsert_hook(store, "h1", event_type="SessionStart", script_body="print('aaa')\n",
                         language="py")
    headers = _open_session(client, env["laptop"])
    one = _call(client, headers, "edit_body", {
        "kind": "hook", "key": "h1", "old_string": "aaa", "new_string": "bbb"})
    many = _call(client, headers, "bulk_edit", {"edits": [
        {"kind": "hook", "key": "h1", "replacements": [["aaa", "bbb"]]}]})
    assert "denied" in _text(one).lower()
    assert "denied" in _text(many).lower()
    assert "aaa" in _hook_body(store)
