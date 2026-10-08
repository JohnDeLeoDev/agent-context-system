"C5: a relay on a non-ls machine connects to the remote daemon instead of a local one.\n\n`AGENT_CONTEXT_HOST` names where the daemon is. Loopback (unset, `127.0.0.1`, `localhost`)\nkeeps today's behavior: probe the port, wait for a supervisor, lazy-spawn. Anything else is a\nremote daemon reached over https with the bearer token, and the relay must never touch the\nlocal machinery or fall back to serving a local store (a second writer)."
import contextlib
import logging
from collections.abc import AsyncIterator, Callable

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCResponse

from agent_context import daemon
from agent_context import server as S

REMOTE_HOST = "example.invalid"
REMOTE_URL = f"https://{REMOTE_HOST}/mcp"
TOKEN = "s3cret-token"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_PORT", "AGENT_CONTEXT_TOKEN",
                 "AGENT_CONTEXT_TRANSPORT", "AGENT_CONTEXT_NO_DAEMON"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", REMOTE_HOST)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)


@pytest.fixture
def no_local_machinery(monkeypatch: pytest.MonkeyPatch) -> None:
    'Any touch of the local daemon path fails the test.'
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("remote mode touched the local daemon machinery")
    monkeypatch.setattr(daemon, "_port_open", boom)
    monkeypatch.setattr(daemon, "_wait_port", boom)
    monkeypatch.setattr(daemon, "_supervisor", boom)
    monkeypatch.setattr(daemon, "_should_bounce", boom)
    monkeypatch.setattr(daemon, "_start_background_bounce", boom)
    monkeypatch.setattr(daemon.subprocess, "Popen", boom)
    monkeypatch.setattr(daemon.socket, "create_connection", boom)
    monkeypatch.setattr(daemon, "flock", boom)




@pytest.mark.parametrize("host", [None, "127.0.0.1", "localhost"])
def test_loopback_is_local_and_keeps_the_plain_http_url(monkeypatch: pytest.MonkeyPatch,
                                                        host: str | None) -> None:
    if host is not None:
        monkeypatch.setenv("AGENT_CONTEXT_HOST", host)
    assert daemon.is_remote() is False
    assert daemon.mcp_url() == f"http://{host or '127.0.0.1'}:8765/mcp"


def test_local_ensure_daemon_still_probes_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    probes: list[int] = []
    monkeypatch.setattr(daemon, "_port_open", lambda: probes.append(1) or True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: False)
    assert daemon.ensure_daemon() == "http://127.0.0.1:8765/mcp"
    assert probes == [1]




def test_remote_host_is_https_on_the_default_port(remote: None) -> None:
    assert daemon.is_remote() is True
    assert daemon.mcp_url() == REMOTE_URL


def test_remote_host_with_an_explicit_port_is_plain_http(remote: None,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_PORT", "9000")
    assert daemon.mcp_url() == f"http://{REMOTE_HOST}:9000/mcp"




def test_remote_ensure_daemon_returns_the_url_and_does_nothing_local(
        remote: None, no_local_machinery: None) -> None:
    assert daemon.ensure_daemon() == REMOTE_URL




def test_remote_without_a_token_fails_fast_naming_the_variable(
        remote: None, no_local_machinery: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN")
    with pytest.raises(RuntimeError, match="AGENT_CONTEXT_TOKEN"):
        daemon.ensure_daemon()




@pytest.mark.parametrize("failing", ["ensure_daemon", "run_bridge"])
def test_remote_failure_exits_nonzero_and_never_serves_stdio(
        remote: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        failing: str) -> None:
    served: list[str] = []

    def fail() -> None:
        raise RuntimeError("remote unreachable")
    monkeypatch.setattr(daemon, "ensure_daemon", fail if failing == "ensure_daemon" else lambda: REMOTE_URL)
    monkeypatch.setattr(daemon, "run_bridge", fail if failing == "run_bridge" else lambda: None)
    monkeypatch.setattr(S.mcp, "run", lambda transport="stdio": served.append(transport))
    
    
    monkeypatch.setattr(S, "_materialize_if_remote", lambda: True)
    with caplog.at_level(logging.ERROR, logger="agent-context"), pytest.raises(SystemExit) as ex:
        S.main()
    assert ex.value.code not in (0, None)
    assert served == []
    assert REMOTE_URL in caplog.text


def test_local_failure_still_falls_back_to_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[str] = []

    def fail() -> None:
        raise RuntimeError("no daemon")
    monkeypatch.setattr(daemon, "ensure_daemon", fail)
    monkeypatch.setattr(S.mcp, "run", lambda transport="stdio": served.append(transport))
    S.main()
    assert served == ["stdio"]




class _Conn:
    'One fake streamable-HTTP connection; records what the bridge sent and where.'

    def __init__(self, url: str, headers: dict[str, str] | None) -> None:
        self.url = url
        self.headers = headers
        self.sent: list[SessionMessage] = []
        self.got_first = anyio.Event()

    async def send(self, item: SessionMessage) -> None:
        self.sent.append(item)
        self.got_first.set()


def _initialize() -> SessionMessage:
    return SessionMessage(JSONRPCMessage(JSONRPCRequest(
        jsonrpc="2.0", id=1, method="initialize", params={})))


def _init_response() -> SessionMessage:
    return SessionMessage(JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=1, result={})))


def _method(item: SessionMessage) -> str | None:
    return getattr(item.message.root, "method", None)


ClientScript = Callable[[anyio.Event, list[_Conn]], AsyncIterator[SessionMessage]]
DaemonScript = Callable[[_Conn, int, anyio.Event], AsyncIterator[SessionMessage]]


def _install_fakes(monkeypatch: pytest.MonkeyPatch, client_script: ClientScript,
                   daemon_script: DaemonScript) -> list[_Conn]:
    "Patch the bridge's two transports. `client_script(done, conns)` is the stdio side's\n    read stream; `daemon_script(conn, index, done)` is one HTTP connection's read stream."
    conns: list[_Conn] = []
    done = anyio.Event()

    @contextlib.asynccontextmanager
    async def fake_stdio() -> AsyncIterator[tuple[AsyncIterator[SessionMessage], _Conn]]:
        yield client_script(done, conns), _Conn("stdio", None)

    @contextlib.asynccontextmanager
    async def fake_http(url: str, headers: dict[str, str] | None = None
                        ) -> AsyncIterator[tuple[AsyncIterator[SessionMessage], _Conn, object]]:
        conn = _Conn(url, headers)
        conns.append(conn)
        yield daemon_script(conn, len(conns), done), conn, (lambda: None)

    monkeypatch.setattr(daemon, "stdio_server", fake_stdio)
    monkeypatch.setattr(daemon, "streamablehttp_client", fake_http)
    return conns


async def _no_messages(done: anyio.Event, conns: list[_Conn]) -> AsyncIterator[SessionMessage]:
    return
    yield  


async def _closed(conn: _Conn, index: int, done: anyio.Event) -> AsyncIterator[SessionMessage]:
    return
    yield  


def test_bridge_connects_to_the_remote_url_with_the_bearer_token(
        remote: None, no_local_machinery: None, monkeypatch: pytest.MonkeyPatch) -> None:
    conns = _install_fakes(monkeypatch, _no_messages, _closed)
    anyio.run(daemon._bridge)
    assert [c.url for c in conns] == [REMOTE_URL]
    assert (conns[0].headers or {})["Authorization"] == f"Bearer {TOKEN}"


def test_bridge_reconnects_to_the_remote_and_replays_the_handshake(
        remote: None, no_local_machinery: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def client(done: anyio.Event, conns: list[_Conn]) -> AsyncIterator[SessionMessage]:
        yield _initialize()
        await done.wait()

    async def daemon_side(conn: _Conn, index: int, done: anyio.Event) -> AsyncIterator[SessionMessage]:
        await conn.got_first.wait()
        if index == 1:
            return  
        yield _init_response()  
        done.set()
        await anyio.sleep_forever()

    conns = _install_fakes(monkeypatch, client, daemon_side)
    anyio.run(daemon._bridge)
    assert len(conns) == 2
    assert {c.url for c in conns} == {REMOTE_URL}
    assert all((c.headers or {})["Authorization"] == f"Bearer {TOKEN}" for c in conns)
    assert [_method(m) for m in conns[1].sent] == ["initialize"]
