"Claude Code 2.1.282 opens with a `server/discover` probe and falls back to `initialize` on a\nJSON-RPC error. The daemon's mcp library answered the forwarded probe with HTTP 400, the bridge\ntook that for a lost daemon, reconnected, and its handshake replay swallowed the initialize\nanswer the client never got: CONNECT_TIMEOUT.\n\n  - the bridge answers `server/discover` itself with method-not-found and never forwards it,\n    and answers or drops anything else sent before `initialize`;\n  - a reconnect replay forwards the initialize answer while the client has none;\n  - a request the daemon refuses with HTTP 400 gets a JSON-RPC error and is not replayed."
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import httpx
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCResponse
from test_relay_bridge_activity import _Conn, _until

from agent_context import daemon

INIT = {"protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}}


def _msg(obj: dict) -> SessionMessage:
    return SessionMessage(JSONRPCMessage.model_validate({"jsonrpc": "2.0", **obj}))


def _answer(rid: Any) -> SessionMessage:
    return SessionMessage(JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=rid, result={})))


def _dump(item: SessionMessage) -> dict:
    return item.message.model_dump(by_alias=True, exclude_none=True)


def _refusal(body: dict, status: int = 400) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://127.0.0.1:8765/mcp", json={"jsonrpc": "2.0", **body})
    return httpx.HTTPStatusError(f"Client error '{status}'", request=req,
                                 response=httpx.Response(status, request=req))


async def _run(monkeypatch: pytest.MonkeyPatch,
               body: Callable[[list[_Conn], Any, Any], Any]) -> None:
    'Run the real `_bridge` (local mode) against fake stdio and fake daemon sessions; each\n    (re)connect appends a `_Conn`. `body(conns, client_in, client_out)` drives it.'
    conns: list[_Conn] = []
    c_in_send, c_read = anyio.create_memory_object_stream[SessionMessage | Exception](16)
    c_write, c_out_recv = anyio.create_memory_object_stream[SessionMessage](16)

    @contextlib.asynccontextmanager
    async def fake_stdio() -> AsyncIterator[tuple[object, object]]:
        yield c_read, c_write

    @contextlib.asynccontextmanager
    async def fake_client(url: str, headers: dict[str, str] | None = None
                          ) -> AsyncIterator[tuple[object, object, Callable[[], None]]]:
        conn = _Conn()
        conns.append(conn)
        yield conn.d_read, conn.d_write, lambda: None

    monkeypatch.setattr(daemon, "stdio_server", fake_stdio)
    monkeypatch.setattr(daemon, "streamablehttp_client", fake_client)
    monkeypatch.setattr(daemon, "ensure_daemon", lambda: "url")
    monkeypatch.delenv("AGENT_CONTEXT_HOST", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)

    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(daemon._bridge)
            await _until(lambda: len(conns) == 1)
            await body(conns, c_in_send, c_out_recv)
            await c_in_send.aclose()
            await anyio.sleep(0.1)
            tg.cancel_scope.cancel()


def test_server_discover_before_initialize_gets_method_not_found_and_init_connects(
        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": "server-discover-probe-1", "method": "server/discover",
                              "params": {"_meta": {}}}))
        seen["probe"] = _dump(await c_out.receive())
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        seen["first_forwarded"] = _dump(await conns[0].from_bridge.receive())
        await conns[0].to_bridge.send(_answer(0))
        seen["init"] = _dump(await c_out.receive())
        seen["connections"] = len(conns)

    anyio.run(_run, monkeypatch, body)
    assert seen["probe"]["id"] == "server-discover-probe-1"
    assert seen["probe"]["error"]["code"] == -32601, "the probe must get method-not-found"
    assert seen["first_forwarded"]["method"] == "initialize", "the probe reached the daemon"
    assert seen["init"] == {"jsonrpc": "2.0", "id": 0, "result": {}}
    assert seen["connections"] == 1, "the probe caused a reconnect"


def test_server_discover_after_initialize_is_answered_locally_too(
        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(0))
        await c_out.receive()
        await c_in.send(_msg({"id": 5, "method": "server/discover", "params": {}}))
        seen["probe"] = _dump(await c_out.receive())
        await c_in.send(_msg({"id": 6, "method": "tools/list"}))
        seen["next_forwarded"] = _dump(await conns[0].from_bridge.receive())

    anyio.run(_run, monkeypatch, body)
    assert seen["probe"]["error"]["code"] == -32601
    assert seen["next_forwarded"]["method"] == "tools/list"


def test_pre_initialize_ping_is_answered_and_notification_dropped(
        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"method": "notifications/cancelled",
                              "params": {"requestId": "server-discover-probe-1"}}))
        await c_in.send(_msg({"id": 1, "method": "ping"}))
        seen["ping"] = _dump(await c_out.receive())
        await c_in.send(_msg({"id": 2, "method": "tools/list"}))
        seen["early_call"] = _dump(await c_out.receive())
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        seen["first_forwarded"] = _dump(await conns[0].from_bridge.receive())

    anyio.run(_run, monkeypatch, body)
    assert seen["ping"] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert seen["early_call"]["error"]["code"] == -32601
    assert seen["first_forwarded"]["method"] == "initialize", \
        "a pre-initialize message reached the daemon"


def test_reconnect_replay_forwards_the_initialize_answer_the_client_never_got(
        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(RuntimeError("daemon restarted"))   
        await _until(lambda: len(conns) == 2)
        seen["replayed"] = _dump(await conns[1].from_bridge.receive())
        await conns[1].to_bridge.send(_answer(0))
        with anyio.fail_after(5):
            seen["init"] = _dump(await c_out.receive())

    anyio.run(_run, monkeypatch, body)
    assert seen["replayed"]["method"] == "initialize"
    assert seen["init"] == {"jsonrpc": "2.0", "id": 0, "result": {}}, \
        "the replay swallowed the only initialize answer"


def test_reconnect_replay_still_swallows_a_second_initialize_answer(
        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(0))
        await c_out.receive()
        await c_in.send(_msg({"id": 3, "method": "tools/list"}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(RuntimeError("daemon restarted"))
        await _until(lambda: len(conns) == 2)
        await conns[1].from_bridge.receive()                 
        await conns[1].to_bridge.send(_answer(0))
        seen["resent"] = _dump(await conns[1].from_bridge.receive())
        await conns[1].to_bridge.send(_answer(3))
        seen["next_to_client"] = _dump(await c_out.receive())

    anyio.run(_run, monkeypatch, body)
    assert seen["resent"]["method"] == "tools/list"
    assert seen["next_to_client"]["id"] == 3, "a second initialize answer reached the client"


def test_a_request_the_daemon_refuses_with_400_is_answered_and_not_replayed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def ordered(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(0))
        await c_out.receive()
        await c_in.send(_msg({"method": "notifications/initialized"}))
        await conns[0].from_bridge.receive()
        await c_in.send(_msg({"id": 7, "method": "tools/call", "params": {"name": "x"}}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_refusal({"id": 7, "method": "tools/call"}))
        seen["answer"] = _dump(await c_out.receive())
        await _until(lambda: len(conns) == 2)
        seen["replay"] = [_dump(await conns[1].from_bridge.receive())["method"]]
        await conns[1].to_bridge.send(_answer(0))
        seen["replay"].append(_dump(await conns[1].from_bridge.receive())["method"])
        with anyio.move_on_after(0.5):
            seen["extra"] = _dump(await conns[1].from_bridge.receive())

    anyio.run(_run, monkeypatch, ordered)
    assert seen["answer"]["id"] == 7 and seen["answer"]["error"]["code"] == -32600
    assert seen["replay"] == ["initialize", "notifications/initialized"]
    assert "extra" not in seen, f"the refused request was replayed: {seen.get('extra')}"


def test_rejected_request_finds_the_refusal_inside_an_exception_group() -> None:
    refusal = _refusal({"id": "server-discover-probe-1", "method": "server/discover"})
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [refusal])
    assert daemon._rejected_request(group) == ("server-discover-probe-1", "server/discover", 400)
    assert daemon._rejected_request(daemon._DaemonGone(refusal))[2] == 400


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502])
def test_rejected_request_leaves_auth_session_and_server_errors_to_the_reconnect_path(
        status: int) -> None:
    assert daemon._rejected_request(_refusal({"id": 1, "method": "tools/call"}, status)) is None


def test_a_refused_notification_is_not_a_rejected_request() -> None:
    assert daemon._rejected_request(_refusal({"method": "notifications/cancelled"})) is None
