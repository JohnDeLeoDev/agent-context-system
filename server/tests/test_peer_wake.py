"policy: the bridge wakes its own session with a peer message the daemon pushed.\n\nThe daemon pushes `notifications/agent-context/peer_message` on the bridge's event stream. The\nbridge consumes it, never forwards it, and writes it to the session's inbox socket: the auth\nline, then the message line. A wake that cannot be made is logged and the bridge carries on."
import contextlib
import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from test_bridge_handshake import INIT, _answer, _dump, _msg, _run
from test_relay_bridge_activity import _Conn, _until

from agent_context import claims, daemon, peer_wake

TOKEN = "t0ken"
TEXT = '<cross-session-message from="store · main">hello</cross-session-message>'


class Inbox:
    "A stand-in for Claude Code's per-session inbox socket: it records each connection's lines."

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.posts: list[list[dict]] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while chunk := conn.recv(65536):
                    data += chunk
            self.posts.append([json.loads(line) for line in data.splitlines()])

    def close(self) -> None:
        
        
        try:
            self._server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._server.close()
        self._thread.join(timeout=2)
        assert not self._thread.is_alive(), "the inbox thread outlived its test"


@pytest.fixture
def inbox(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Inbox]:
    
    box = Inbox(tmp_path_factory.mktemp("pw") / "s.sock")
    monkeypatch.setenv(peer_wake.SOCKET_ENV, box.path)
    monkeypatch.setenv(peer_wake.TOKEN_ENV, TOKEN)
    yield box
    box.close()


def _push(params: Any) -> SessionMessage:
    return _msg({"method": peer_wake.PEER_MESSAGE_METHOD, "params": params})


def _root(item: SessionMessage) -> JSONRPCMessage:
    return item.message.root


def test_peer_message_parses_the_push_and_ignores_every_other_message() -> None:
    assert peer_wake.peer_message(_root(_push({"id": "m1", "text": TEXT}))) == {"id": "m1", "text": TEXT}
    assert peer_wake.peer_message(_root(_push({"id": "m1"}))) == {}
    assert peer_wake.peer_message(_root(_push({"text": 3}))) == {}
    other = _msg({"method": "notifications/agent-context/content_changed", "params": {"paths": []}})
    assert peer_wake.peer_message(_root(other)) is None
    assert peer_wake.peer_message(_root(_answer(1))) is None


def test_wake_writes_the_auth_line_then_the_message_line(inbox: Inbox) -> None:
    assert peer_wake.wake(TEXT) == "claude-code"
    assert _wait(lambda: len(inbox.posts) == 1)
    assert inbox.posts[0] == [
        {"type": "auth", "token": TOKEN},
        {"type": "user", "message": {"role": "user", "content": TEXT}},
    ]


def test_wake_without_a_route_raises_and_deliver_reports_none() -> None:
    with pytest.raises(peer_wake.NoRoute):
        peer_wake.wake(TEXT, env={})
    with pytest.raises(peer_wake.NoRoute):
        peer_wake.wake(TEXT, env={peer_wake.SOCKET_ENV: "/nonexistent.sock"})   


def test_deliver_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert peer_wake.deliver({}) is None
    monkeypatch.delenv(peer_wake.SOCKET_ENV, raising=False)
    assert peer_wake.deliver({"id": "m1", "text": TEXT}) is None
    monkeypatch.setenv(peer_wake.SOCKET_ENV, str(tmp_path / "gone.sock"))
    monkeypatch.setenv(peer_wake.TOKEN_ENV, TOKEN)
    assert peer_wake.deliver({"id": "m1", "text": TEXT}) is None


def _waiting(params: Any) -> SessionMessage:
    return _msg({"method": peer_wake.PEER_WAITING_METHOD, "params": params})


def _claim(session: str, pids: list[int]) -> None:
    d = claims.claims_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.json").write_text(json.dumps(
        {"session": session, "pids": pids, "last_seen": time.time(), "started": time.time()}))


def test_peer_waiting_parses_the_count_and_ignores_every_other_message() -> None:
    assert peer_wake.peer_waiting(_root(_waiting({"count": 3}))) == 3
    for bad in ({"count": "3"}, {"count": True}, {"count": -1}, {}, None):
        assert peer_wake.peer_waiting(_root(_waiting(bad))) == 0
    assert peer_wake.peer_waiting(_root(_push({"id": "m1", "text": TEXT}))) is None
    assert peer_wake.peer_waiting(_root(_answer(1))) is None


def test_note_waiting_leaves_a_flag_named_for_the_hook_claim_that_owns_the_bridge() -> None:
    assert peer_wake.note_waiting(2) is None            
    _claim("0a1b2c3d-session", [os.getpid()])
    flag = Path(peer_wake.note_waiting(2) or "")
    
    assert flag == claims.claims_dir().parent / "peer-waiting" / "0a1b2c3d"
    assert flag.read_text() == "2"
    assert peer_wake.note_waiting(0) is None            


def test_a_wake_that_fails_holds_the_message_for_the_sessions_hook(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(peer_wake.SOCKET_ENV, str(tmp_path / "gone.sock"))   
    monkeypatch.setenv(peer_wake.TOKEN_ENV, TOKEN)
    assert peer_wake.deliver({"id": "m0", "text": TEXT}) is None      
    waiting = claims.claims_dir().parent / "peer-waiting"
    assert not waiting.exists()
    _claim("0a1b2c3d-session", [os.getpid()])
    assert peer_wake.deliver({"id": "m1", "text": TEXT}) is None
    assert peer_wake.deliver({"id": "m2", "text": "second"}) is None
    held = waiting / "0a1b2c3d.held"
    assert [json.loads(line) for line in held.read_text().splitlines()] == [
        {"text": TEXT}, {"text": "second"}]

    def boom(text: str, env: Any = None) -> str:
        raise ValueError("any failure of the wake holds the message")

    monkeypatch.setattr(peer_wake, "wake", boom)
    assert peer_wake.deliver({"id": "m3", "text": "third"}) is None
    assert len(held.read_text().splitlines()) == 3


def test_a_report_is_a_tool_call_of_the_bridges_own_and_its_answer_is_read() -> None:
    rid = peer_wake.report_id()
    assert rid.startswith("ac-peer-") and rid != peer_wake.report_id()
    body = peer_wake.undelivered({"id": "m1", "text": TEXT, "sender": "uuid-ls.11112222"})
    assert body == {"event": "undelivered", "id": "m1", "text": TEXT, "sender": "uuid-ls.11112222"}
    request = _dump(peer_wake.report_request(rid, body))
    assert request["id"] == rid and request["method"] == "tools/call"
    assert request["params"]["name"] == "relay_report"
    assert request["params"]["arguments"]["kind"] == "peer"
    assert json.loads(request["params"]["arguments"]["body"]) == body

    def answer(result: Any = None, error: Any = None) -> Any:
        return SimpleNamespace(result=result, error=error)

    def text(payload: Any) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}

    assert peer_wake.report_accepted(answer(text({"queued": "m1"})))
    assert not peer_wake.report_accepted(answer(text({"error": "no address", "status": 409})))
    assert not peer_wake.report_accepted(answer({**text({"queued": "m1"}), "isError": True}))
    assert not peer_wake.report_accepted(answer(error={"code": -32602, "message": "bad kind"}))
    assert not peer_wake.report_accepted(answer({"content": [{"type": "text", "text": "words"}]}))
    assert not peer_wake.report_accepted(answer({"content": []}))


def test_the_watch_push_leaves_a_file_for_the_sessions_hook() -> None:
    assert peer_wake.peer_watch(_root(_msg({"method": peer_wake.PEER_WATCH_METHOD, "params": {}})))
    assert peer_wake.peer_watch(_root(_push({"id": "m1", "text": TEXT}))) is None
    assert peer_wake.note_watch() is None                 
    _claim("0a1b2c3d-session", [os.getpid()])
    watch = Path(peer_wake.note_watch() or "")
    assert watch == claims.claims_dir().parent / "peer-waiting" / "0a1b2c3d.watch"
    assert watch.read_text() == peer_wake.bridge_id()     


def test_the_bridge_reports_a_failed_wake_and_holds_the_message_when_the_daemon_refuses(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(peer_wake.SOCKET_ENV, str(tmp_path / "gone.sock"))   
    monkeypatch.setenv(peer_wake.TOKEN_ENV, TOKEN)
    _claim("0a1b2c3d-session", [os.getpid()])
    held = claims.claims_dir().parent / "peer-waiting" / "0a1b2c3d.held"
    seen: dict[str, Any] = {}

    def ok(rid: str, payload: dict) -> SessionMessage:
        return _msg({"id": rid, "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}})

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(0))
        await c_out.receive()
        
        await conns[0].to_bridge.send(_push({"id": "m1", "text": TEXT, "sender": "k"}))
        first = _dump(await conns[0].from_bridge.receive())
        seen["first"] = first
        await conns[0].to_bridge.send(ok(first["id"], {"queued": "m1"}))
        
        await conns[0].to_bridge.send(_push({"id": "m2", "text": "second"}))
        second = _dump(await conns[0].from_bridge.receive())
        await conns[0].to_bridge.send(ok(second["id"], {"error": "no address", "status": 409}))
        await _until(held.exists)
        
        await c_in.send(_msg({"id": 7, "method": "tools/list"}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(7))
        seen["next"] = _dump(await c_out.receive())

    anyio.run(_run, monkeypatch, body)
    args = seen["first"]["params"]["arguments"]
    assert seen["first"]["params"]["name"] == "relay_report" and args["kind"] == "peer"
    assert json.loads(args["body"]) == {"event": "undelivered", "id": "m1", "text": TEXT,
                                        "sender": "k"}
    assert [json.loads(line) for line in held.read_text().splitlines()] == [{"text": "second"}]
    assert seen["next"] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def _turn_claim(session: str, state: str | None, stamp: int) -> Path:
    'The claim of the session that owns this process, as its hook leaves it.'
    path = claims.claims_dir() / f"{session}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"session": session, "pids": [os.getpid()], "last_seen": time.time(),
           "started": time.time()}
    path.write_text(json.dumps(rec if state is None else {**rec, "turn": state}))
    os.utime(path, ns=(stamp, stamp))       
    return path


def test_the_turn_watch_reports_each_change_of_the_claim_once() -> None:
    watch = peer_wake.TurnWatch()
    assert watch.poll() is None                         
    assert watch.woke() is None
    watch.tried = 0.0                                   
    path = _turn_claim("0a1b2c3d-session", None, 1)
    assert watch.poll() is None and watch.woke() is None    
    _turn_claim("0a1b2c3d-session", "busy", 2)
    assert watch.poll() == "busy"
    assert watch.poll() is None                         
    _turn_claim("0a1b2c3d-session", "busy", 3)          
    assert watch.poll() is None
    _turn_claim("0a1b2c3d-session", "idle", 4)
    assert watch.poll() == "idle"
    
    assert watch.woke() == "busy"
    assert watch.poll() is None and watch.woke() is None
    _turn_claim("0a1b2c3d-session", "idle", 5)          
    assert watch.poll() == "idle"
    watch.forget()                                      
    assert watch.poll() == "idle"
    path.write_text("not json")
    os.utime(path, ns=(6, 6))
    assert watch.poll() is None
    path.unlink()                                       
    assert watch.poll() is None and watch.path is None
    assert peer_wake.turn("idle") == {"event": "turn", "state": "idle"}


def test_the_bridge_reports_its_sessions_turn_and_keeps_the_answer_from_the_client(
        monkeypatch: pytest.MonkeyPatch, inbox: Inbox) -> None:
    monkeypatch.setattr(peer_wake, "TURN_POLL_SECONDS", 0.01)
    _turn_claim("0a1b2c3d-session", "idle", 1)
    seen: dict[str, Any] = {}

    def body_of(request: dict) -> dict:
        assert request["params"]["name"] == "relay_report"
        return json.loads(request["params"]["arguments"]["body"])

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(0))
        await c_out.receive()
        first = _dump(await conns[0].from_bridge.receive())
        seen["first"] = body_of(first)
        
        await conns[0].to_bridge.send(_msg({"id": first["id"], "result": {
            "content": [{"type": "text", "text": json.dumps({"error": "no address"})}],
            "isError": False}}))
        
        await conns[0].to_bridge.send(_push({"id": "m1", "text": TEXT}))
        seen["woke"] = body_of(_dump(await conns[0].from_bridge.receive()))
        _turn_claim("0a1b2c3d-session", "idle", 2)
        seen["stopped"] = body_of(_dump(await conns[0].from_bridge.receive()))
        await c_in.send(_msg({"id": 7, "method": "tools/list"}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(7))
        seen["next"] = _dump(await c_out.receive())

    anyio.run(_run, monkeypatch, body)
    assert seen["first"] == {"event": "turn", "state": "idle"}
    assert seen["woke"] == {"event": "turn", "state": "busy"}
    assert seen["stopped"] == {"event": "turn", "state": "idle"}
    assert seen["next"] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert not (claims.claims_dir().parent / "peer-waiting").exists()


def test_note_waiting_leaves_no_flag_for_a_claim_the_daemon_wrote() -> None:
    _claim(f"pid{os.getpid()}", [os.getpid()])          
    assert peer_wake.note_waiting(1) is None
    assert not (claims.claims_dir().parent / "peer-waiting").exists()


def _wait(cond: Any, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_bridge_consumes_the_push_and_wakes_the_session(
        monkeypatch: pytest.MonkeyPatch, inbox: Inbox) -> None:
    seen: dict[str, Any] = {}

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(0))
        await c_out.receive()
        await conns[0].to_bridge.send(_push({"id": "m1", "text": TEXT}))
        await _until(lambda: len(inbox.posts) == 1)
        
        await c_in.send(_msg({"id": 7, "method": "tools/list"}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(7))
        seen["next"] = _dump(await c_out.receive())
        seen["connections"] = len(conns)

    anyio.run(_run, monkeypatch, body)
    assert inbox.posts[0][1]["message"]["content"] == TEXT
    assert seen["next"] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert seen["connections"] == 1, "a peer message caused a reconnect"


def test_a_client_that_names_itself_is_the_route_and_gets_the_push_forwarded(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(peer_wake.SOCKET_ENV, raising=False)
    monkeypatch.delenv(peer_wake.TOKEN_ENV, raising=False)
    assert peer_wake.route({peer_wake.CLIENT_ENV: "something else"}) is None
    assert not peer_wake.forwards({})
    monkeypatch.setenv(peer_wake.CLIENT_ENV, "pi")
    assert peer_wake.route() == "pi" and peer_wake.forwards()
    assert peer_wake.connect_headers()["x-bridge-wake"] == "pi"
    seen: dict[str, Any] = {}

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(0))
        await c_out.receive()
        await conns[0].to_bridge.send(_push({"id": "m1", "text": TEXT}))
        seen["next"] = _dump(await c_out.receive())

    anyio.run(_run, monkeypatch, body)
    assert seen["next"] == {"jsonrpc": "2.0", "method": peer_wake.PEER_MESSAGE_METHOD,
                            "params": {"id": "m1", "text": TEXT}}


def test_codex_is_woken_by_queueing_on_the_thread_of_the_claim_that_owns_the_bridge(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(peer_wake.SOCKET_ENV, raising=False)
    monkeypatch.setenv(peer_wake.CLIENT_ENV, "codex")
    assert peer_wake.route() == "codex" and not peer_wake.forwards()
    with pytest.raises(peer_wake.NoRoute, match="no thread"):      
        peer_wake.wake(TEXT)
    d = claims.claims_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "01a10d18-thread.json").write_text(json.dumps(
        {"session": "01a10d18-thread", "pids": [os.getpid()], "cwd": str(tmp_path),
         "last_seen": time.time(), "started": time.time()}))
    ran: list[tuple[list[str], Any]] = []

    def fake_run(argv: list[str], **kw: Any) -> Any:
        ran.append((argv, kw.get("cwd")))
        return SimpleNamespace(returncode=len(ran) - 1, stdout="", stderr="thread not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert peer_wake.wake(TEXT) == "codex"
    assert ran == [(["codex", "queue", "--thread", "01a10d18-thread", "--message", TEXT],
                    str(tmp_path))]
    with pytest.raises(OSError, match="thread not found"):         
        peer_wake.wake(TEXT)
    assert peer_wake.deliver({"id": "m1", "text": TEXT}) is None    


def test_the_parent_program_names_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(peer_wake.SOCKET_ENV, raising=False)
    monkeypatch.delenv(peer_wake.CLIENT_ENV, raising=False)
    monkeypatch.setattr(peer_wake, "_parent_command", lambda: "pytest")
    assert peer_wake.route() is None                    
    monkeypatch.setattr(peer_wake, "_parent_command", lambda: "codex")
    assert peer_wake.route() == "codex"
    assert peer_wake.route({}) is None                  
    assert peer_wake.connect_headers()["x-bridge-wake"] == "codex"


def test_opencode_is_woken_through_the_spool_its_plugin_watches(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(peer_wake.SOCKET_ENV, raising=False)
    monkeypatch.delenv(peer_wake.CLIENT_ENV, raising=False)
    monkeypatch.setattr(peer_wake, "_parent_command", lambda: "opencode")
    monkeypatch.chdir(tmp_path)
    spool = peer_wake._opencode_spool()
    name = hashlib.sha256(os.path.realpath(tmp_path).encode()).hexdigest()[:16]
    assert spool == claims.claims_dir().parent / "peer-spool" / str(os.getppid()) / name
    
    assert peer_wake.route() is None
    assert peer_wake.connect_headers().get("x-bridge-notice") == "hook"
    spool.mkdir(parents=True)                      
    assert peer_wake.route() == "opencode" and not peer_wake.forwards()
    assert peer_wake.wake(TEXT) == "opencode"
    assert peer_wake.wake(TEXT) == "opencode"
    left = sorted(spool.iterdir())
    assert len(left) == 2 and all(p.suffix == ".json" for p in left)
    assert [json.loads(p.read_text()) for p in left] == [{"text": TEXT}] * 2


def test_bridge_survives_a_push_it_cannot_deliver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(peer_wake.SOCKET_ENV, raising=False)
    monkeypatch.delenv(peer_wake.TOKEN_ENV, raising=False)
    seen: dict[str, Any] = {}

    async def body(conns: list[_Conn], c_in: Any, c_out: Any) -> None:
        await c_in.send(_msg({"id": 0, "method": "initialize", "params": INIT}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(0))
        await c_out.receive()
        await conns[0].to_bridge.send(_push({"id": "m1", "text": TEXT}))
        await conns[0].to_bridge.send(_push({"text": ""}))
        await conns[0].to_bridge.send(_waiting({"count": 1}))     
        await c_in.send(_msg({"id": 7, "method": "tools/list"}))
        await conns[0].from_bridge.receive()
        await conns[0].to_bridge.send(_answer(7))
        seen["next"] = _dump(await c_out.receive())
        seen["connections"] = len(conns)

    anyio.run(_run, monkeypatch, body)
    assert seen["next"] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert seen["connections"] == 1


def test_a_local_bridge_reconnects_when_its_event_stream_drops(
        monkeypatch: pytest.MonkeyPatch) -> None:
    "An idle bridge on the daemon's host learns of a daemon restart only from its event\n    stream, and a peer message reaches it only down that stream."
    conns: list[_Conn] = []
    request_hooks: list[Any] = []
    c_in_send, c_read = anyio.create_memory_object_stream[SessionMessage | Exception](16)
    c_write, _c_out = anyio.create_memory_object_stream[SessionMessage](16)

    @contextlib.asynccontextmanager
    async def fake_stdio() -> AsyncIterator[tuple[object, object]]:
        yield c_read, c_write

    @contextlib.asynccontextmanager
    async def fake_client(url: str, headers: dict[str, str] | None = None,
                          httpx_client_factory: Any = None) -> AsyncIterator[tuple[Any, Any, Any]]:
        client = httpx_client_factory()
        request_hooks.append(client.event_hooks["request"])
        conn = _Conn()
        conns.append(conn)
        try:
            yield conn.d_read, conn.d_write, lambda: None
        finally:
            await client.aclose()

    monkeypatch.setattr(daemon, "stdio_server", fake_stdio)
    monkeypatch.setattr(daemon, "streamablehttp_client", fake_client)
    monkeypatch.setattr(daemon, "ensure_daemon", lambda: "url")
    monkeypatch.delenv("AGENT_CONTEXT_HOST", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)

    async def main() -> None:
        with anyio.fail_after(30):
            async with anyio.create_task_group() as tg:
                tg.start_soon(daemon._bridge)
                await _until(lambda: len(conns) == 1)
                get = httpx.Request("GET", "http://127.0.0.1:8765/mcp")
                for _ in range(2):          
                    for hook in request_hooks[0]:
                        await hook(get)
                await _until(lambda: len(conns) == 2)
                await c_in_send.aclose()
                await anyio.sleep(0.1)
                tg.cancel_scope.cancel()

    anyio.run(main)
    assert len(conns) == 2
