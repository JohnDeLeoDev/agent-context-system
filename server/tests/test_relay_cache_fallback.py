'C8: a relay survives an ls that is down at session start, and re-fetches after a reconnect.\n\nCache fallback: when the fetch fails, the bundle cached by an earlier session is applied\n(files rewritten, settings re-synced) and the cache itself is left alone. Degraded relay:\na remote relay whose fetch failed serves a local stdio MCP server with no upstream (empty\ntool list, an instructions text that says ls is unreachable), never a second local writer,\nand exits 0 when the client closes. Reconnect: the bridge asks the refresher for an\nunconditional re-fetch each time it reconnects to the daemon, because pushes sent during\nan outage are lost.'
from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import ClassVar

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCMessage,
    JSONRPCRequest,
    JSONRPCResponse,
)

from agent_context import daemon
from agent_context import relay_materialize as R
from agent_context import server as S

REMOTE_HOST = "example.invalid"
DOWN_HOST = "ls-down.invalid"
TOKEN = "s3cret-token"
DEBOUNCE = 0.05
SRC = Path(__file__).resolve().parents[1] / "src"

SETTINGS_SCRIPT = (
    "import os, pathlib\n"
    "home = pathlib.Path(os.environ['HOME'])\n"
    "with open(home / 'settings-runs.log', 'a') as f:\n"
    "    f.write('run\\n')\n"
)

BUNDLE: dict[str, str] = {
    "skills/demo/SKILL.md": "# demo skill\n",
    "commands/guide.md": "guide\n",
    "hooks/guard.py": "#!/usr/bin/env python3\nprint('guard')\n",
    "scripts/home-settings-sync.py": SETTINGS_SCRIPT,
    "scripts/home-materialize.py": "#!/usr/bin/env python3\n",
}

ENV_NAMES = ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_PORT", "AGENT_CONTEXT_TOKEN",
             "AGENT_CONTEXT_TRANSPORT", "AGENT_CONTEXT_NO_DAEMON", "AGENT_CONTEXT_STORE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def no_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    'The cache path must not touch the network: any fetch fails the test.'
    def refuse(url: str, token: str, timeout: float) -> None:
        raise AssertionError("the cache fallback must not fetch")
    monkeypatch.setattr(R, "fetch_bundle_mcp", refuse)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)


def _settings_runs(home: Path) -> int:
    log = home / "settings-runs.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


def _cache_bytes(home: Path) -> bytes:
    return R.cache_path(home).read_bytes()




def test_a_cached_bundle_is_applied_when_asked(home: Path, no_fetch: None) -> None:
    R.write_cache(BUNDLE, home)
    assert R.apply_cached(home) is True
    for key, content in BUNDLE.items():
        assert R.target_for(key, home).read_text() == content


def test_the_cache_fallback_restores_a_file_deleted_since_the_last_session(
        home: Path, no_fetch: None) -> None:
    R.apply_bundle(BUNDLE, home)
    R.write_cache(BUNDLE, home)
    R.target_for("commands/guide.md", home).unlink()
    assert R.apply_cached(home) is True
    assert R.target_for("commands/guide.md", home).read_text() == "guide\n"


def test_the_cache_fallback_re_syncs_settings(home: Path, no_fetch: None) -> None:
    R.write_cache(BUNDLE, home)
    assert R.apply_cached(home) is True
    assert _settings_runs(home) == 1


def test_the_cache_fallback_leaves_the_cache_file_byte_identical(
        home: Path, no_fetch: None) -> None:
    R.write_cache(BUNDLE, home)
    before = _cache_bytes(home)
    mtime = R.cache_path(home).stat().st_mtime_ns
    assert R.apply_cached(home) is True
    assert _cache_bytes(home) == before
    assert R.cache_path(home).stat().st_mtime_ns == mtime


def test_the_cache_fallback_logs_one_warning(
        home: Path, no_fetch: None, caplog: pytest.LogCaptureFixture) -> None:
    R.write_cache(BUNDLE, home)
    with caplog.at_level(logging.WARNING):
        assert R.apply_cached(home) is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "cache" in warnings[0].getMessage().lower()
    assert TOKEN not in caplog.text


def test_no_cache_means_no_writes_and_a_logged_warning(
        home: Path, no_fetch: None, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert R.apply_cached(home) is False
    assert not (home / ".claude").exists() and not (home / ".agent-context").exists()
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_an_unreadable_cache_is_treated_as_no_cache(home: Path, no_fetch: None) -> None:
    R.cache_path(home).parent.mkdir(parents=True)
    R.cache_path(home).write_text("{not json")
    assert R.apply_cached(home) is False
    assert not (home / ".agent-context").exists()


def test_a_cache_with_an_unsafe_key_is_refused_whole(home: Path, no_fetch: None) -> None:
    unsafe = {"docs/ok.md": "fine", "../escape.md": "bad"}
    R.cache_path(home).parent.mkdir(parents=True)
    R.cache_path(home).write_text(json.dumps(unsafe))
    assert R.apply_cached(home) is False
    assert not (home / ".agent-context").exists()
    assert json.loads(R.cache_path(home).read_text()) == unsafe


def test_a_failing_settings_sync_does_not_undo_the_cached_files(
        home: Path, no_fetch: None) -> None:
    R.write_cache({**BUNDLE, "scripts/home-settings-sync.py": "import sys\nsys.exit(1)\n"}, home)
    assert R.apply_cached(home) is True
    assert R.target_for("commands/guide.md", home).read_text() == "guide\n"


def test_the_cache_fallback_defaults_to_the_users_home(
        home: Path, no_fetch: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    R.write_cache(BUNDLE, home)
    assert R.apply_cached() is True
    assert (home / ".agent-context/global/commands/guide.md").read_text() == "guide\n"




@pytest.fixture
def start_order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []
    monkeypatch.setattr(daemon, "ensure_daemon", lambda: order.append("ensure") or "url")
    monkeypatch.setattr(daemon, "run_bridge", lambda: order.append("bridge"))
    monkeypatch.setattr(daemon, "run_degraded_relay", lambda: order.append("degraded"))
    monkeypatch.setattr(R, "apply_cached", lambda *a, **k: order.append("cache") or True)
    return order


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", REMOTE_HOST)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)


def _materialize_returns(monkeypatch: pytest.MonkeyPatch, order: list[str], ok: bool) -> None:
    monkeypatch.setattr(R, "materialize_on_start",
                        lambda *a, **k: order.append("materialize") or ok)


def test_a_failed_fetch_applies_the_cache_then_serves_degraded_without_the_bridge(
        start_order: list[str], remote: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _materialize_returns(monkeypatch, start_order, False)
    S.main()
    assert start_order == ["ensure", "materialize", "cache", "degraded"]


def test_a_successful_fetch_runs_the_bridge_and_never_reads_the_cache(
        start_order: list[str], remote: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _materialize_returns(monkeypatch, start_order, True)
    S.main()
    assert start_order == ["ensure", "materialize", "bridge"]


def test_a_crash_in_materialize_still_runs_the_bridge(
        start_order: list[str], remote: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> bool:
        start_order.append("materialize")
        raise RuntimeError("disk full")
    monkeypatch.setattr(R, "materialize_on_start", boom)
    S.main()
    assert start_order == ["ensure", "materialize", "bridge"]


def test_a_crash_in_the_cache_fallback_still_serves_degraded(
        start_order: list[str], remote: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _materialize_returns(monkeypatch, start_order, False)

    def boom(*a: object, **k: object) -> bool:
        start_order.append("cache")
        raise RuntimeError("disk full")
    monkeypatch.setattr(R, "apply_cached", boom)
    S.main()
    assert start_order == ["ensure", "materialize", "cache", "degraded"]


def test_a_local_relay_never_degrades_and_never_reads_the_cache(
        start_order: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _materialize_returns(monkeypatch, start_order, False)
    S.main()
    assert start_order == ["ensure", "bridge"]


def test_a_failure_in_the_degraded_relay_exits_one_and_never_opens_a_local_server(
        start_order: list[str], remote: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _materialize_returns(monkeypatch, start_order, False)

    def boom() -> None:
        start_order.append("degraded")
        raise RuntimeError("no stdio")
    monkeypatch.setattr(daemon, "run_degraded_relay", boom)
    ran_local: list[str] = []
    monkeypatch.setattr(S.mcp, "run", lambda *a, **k: ran_local.append("stdio"))
    with pytest.raises(SystemExit) as raised:
        S.main()
    assert raised.value.code == 1
    assert ran_local == []


def test_the_wiring_reports_whether_the_relay_may_use_the_bridge(
        remote: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "apply_cached", lambda *a, **k: True)
    monkeypatch.setattr(R, "materialize_on_start", lambda *a, **k: False)
    assert S._materialize_if_remote() is False
    monkeypatch.setattr(R, "materialize_on_start", lambda *a, **k: True)
    assert S._materialize_if_remote() is True




def _initialize() -> str:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "c8-test", "version": "0"}}})


def _run_relay(home: Path) -> tuple[int, dict[int, dict[str, object]], str]:
    'Start a remote relay whose ls cannot be reached, talk MCP to it, close stdin.'
    env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
    env.update(HOME=str(home), AGENT_CONTEXT_HOST=DOWN_HOST, AGENT_CONTEXT_TOKEN=TOKEN,
               PYTHONPATH=str(SRC))
    lines = [
        _initialize(),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    replies: dict[int, dict[str, object]] = {}

    def collect(line: str) -> None:
        if line.startswith("{"):
            message = json.loads(line)
            if "id" in message:
                replies[int(message["id"])] = message

    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_context.server"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    
    
    try:
        proc.stdin.write("\n".join(lines) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            collect(line)
            if 2 in replies:
                break
        proc.stdin.close()
        for line in proc.stdout:
            collect(line)
        stderr = proc.stderr.read()
        code = proc.wait(timeout=60)
    finally:
        proc.kill()
        proc.wait()
    return code, replies, stderr


def test_a_relay_with_ls_down_and_no_cache_serves_an_empty_tool_list_and_exits_zero(
        home: Path) -> None:
    code, replies, stderr = _run_relay(home)
    assert code == 0, stderr
    init = replies[1]["result"]
    assert isinstance(init, dict)
    assert "unreachable" in str(init["instructions"]).lower()
    tools = replies[2]["result"]
    assert isinstance(tools, dict)
    assert tools["tools"] == []
    assert not (home / ".agent-context").exists()


def test_a_relay_with_ls_down_materializes_from_the_cache_and_never_opens_a_store(
        home: Path) -> None:
    R.write_cache(BUNDLE, home)
    before = _cache_bytes(home)
    code, replies, stderr = _run_relay(home)
    assert code == 0, stderr
    for key, content in BUNDLE.items():
        assert R.target_for(key, home).read_text() == content
    assert _settings_runs(home) == 1
    assert _cache_bytes(home) == before
    assert not (home / ".agent-context" / ".git").exists()
    init = replies[1]["result"]
    assert isinstance(init, dict)
    assert "unreachable" in str(init["instructions"]).lower()




class FakeDaemon:
    "Stands in for the ls daemon's get_materialized MCP tool: R.fetch_bundle_mcp is\n    patched to answer from here instead of a real MCP round trip."

    def __init__(self) -> None:
        self.status = 200
        self.body: dict[str, str] = {}
        self.requests = 0  

    def serve(self, bundle: dict[str, str]) -> None:
        self.status = 200
        self.body = bundle

    def close(self) -> None:
        pass


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDaemon]:
    d = FakeDaemon()
    d.serve(BUNDLE)

    def fetch(url: str, token: str, timeout: float) -> dict[str, str] | None:
        d.requests += 1
        return d.body if d.status == 200 else None
    monkeypatch.setattr(R, "fetch_bundle_mcp", fetch)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    yield d
    d.close()


@pytest.fixture
def primed(home: Path, fake: FakeDaemon) -> Path:
    R.apply_bundle(BUNDLE, home)
    R.write_cache(BUNDLE, home)
    return home


async def _until(predicate: Callable[[], bool], limit: float = 5.0) -> None:
    with anyio.fail_after(limit):
        while not predicate():
            await anyio.sleep(0.01)


async def _with_refresher(
        home: Path, scenario: Callable[[R.ContentRefresher], Awaitable[None]]) -> None:
    refresher = R.ContentRefresher(home, debounce=DEBOUNCE, timeout=5.0)
    async with anyio.create_task_group() as tg:
        tg.start_soon(refresher.run)
        try:
            await scenario(refresher)
        finally:
            tg.cancel_scope.cancel()


def test_a_request_re_fetches_without_any_pushed_path(primed: Path, fake: FakeDaemon) -> None:
    fake.serve({**BUNDLE, "commands/guide.md": "changed\n"})

    async def scenario(refresher: R.ContentRefresher) -> None:
        refresher.request()
        
        await _until(lambda: R.target_for("commands/guide.md", primed).read_text() == "changed\n"
                     and (R.read_cache(primed) or {}).get("commands/guide.md") == "changed\n")

    anyio.run(_with_refresher, primed, scenario)


def test_requests_in_one_burst_make_one_fetch(primed: Path, fake: FakeDaemon) -> None:
    fake.serve({**BUNDLE, "commands/guide.md": "changed\n"})

    async def scenario(refresher: R.ContentRefresher) -> None:
        refresher.request()
        refresher.request()
        refresher.request()
        await _until(lambda: (R.read_cache(primed) or {}).get("commands/guide.md") == "changed\n")
        await anyio.sleep(DEBOUNCE * 10)  
        assert fake.requests == 1

    anyio.run(_with_refresher, primed, scenario)


def test_a_request_that_finds_a_failing_daemon_logs_and_the_next_one_still_works(
        primed: Path, fake: FakeDaemon, caplog: pytest.LogCaptureFixture) -> None:
    async def scenario(refresher: R.ContentRefresher) -> None:
        fake.status = 500
        with caplog.at_level(logging.WARNING):
            refresher.request()
            await _until(lambda: fake.requests == 1)
            await anyio.sleep(0.3)
        assert any(r.levelno >= logging.WARNING for r in caplog.records)
        fake.serve({**BUNDLE, "commands/guide.md": "later\n"})
        refresher.request()
        await _until(lambda: R.target_for("commands/guide.md", primed).read_text() == "later\n")

    anyio.run(_with_refresher, primed, scenario)


def test_a_push_for_a_path_outside_the_bundle_still_makes_no_fetch(
        primed: Path, fake: FakeDaemon) -> None:
    async def scenario(refresher: R.ContentRefresher) -> None:
        refresher.notify(["global/memories/some-memory.md"])
        await anyio.sleep(0.5)
        assert fake.requests == 0

    anyio.run(_with_refresher, primed, scenario)




class _Conn:
    'One fake HTTP session to the daemon: what the bridge reads and what it wrote.'

    def __init__(self) -> None:
        self.to_bridge, self.d_read = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        self.d_write, self.from_bridge = anyio.create_memory_object_stream[SessionMessage](16)


class _RecordingRefresher:
    instances: ClassVar[list[_RecordingRefresher]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.requests = 0
        self.notified: list[list[str]] = []
        _RecordingRefresher.instances.append(self)

    def request(self) -> None:
        self.requests += 1

    def notify(self, paths: list[str]) -> None:
        self.notified.append(list(paths))

    async def run(self) -> None:
        await anyio.sleep_forever()


def _message(root: JSONRPCRequest | JSONRPCResponse) -> SessionMessage:
    return SessionMessage(JSONRPCMessage(root))


async def _drive_bridge(monkeypatch: pytest.MonkeyPatch, drops: int) -> list[_Conn]:
    'Run the real `_bridge` against fake stdio and fake daemon sessions.\n\n    The client sends `initialize`; the daemon answers; then the daemon session dies\n    `drops` times, and each time the bridge must open a new session and replay the\n    handshake. Returns every session the bridge opened.'
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
    monkeypatch.setattr(R, "ContentRefresher", _RecordingRefresher)

    init = _message(JSONRPCRequest(jsonrpc="2.0", id=1, method="initialize", params={
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "c8-test", "version": "0"}}))
    answer = _message(JSONRPCResponse(jsonrpc="2.0", id=1, result={}))
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(daemon._bridge)
            await c_in_send.send(init)
            for n in range(drops + 1):
                await _until(lambda n=n: len(conns) == n + 1)
                await conns[n].from_bridge.receive()          
                await conns[n].to_bridge.send(answer)
                if n == 0:
                    await c_out_recv.receive()                
                if n < drops:
                    await anyio.sleep(0.05)
                    await conns[n].to_bridge.send(RuntimeError("daemon restarted"))
            await anyio.sleep(0.3)
            await c_in_send.aclose()
    return conns


@pytest.fixture(autouse=True)
def _reset_recorders() -> None:
    _RecordingRefresher.instances = []


def test_a_remote_bridge_requests_a_re_fetch_on_each_reconnect_and_not_on_first_connect(
        remote: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        await _drive_bridge(monkeypatch, drops=1)

    anyio.run(scenario)
    assert len(_RecordingRefresher.instances) == 1
    assert _RecordingRefresher.instances[0].requests == 1


def test_two_reconnects_request_two_re_fetches(
        remote: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        await _drive_bridge(monkeypatch, drops=2)

    anyio.run(scenario)
    assert _RecordingRefresher.instances[0].requests == 2


def test_a_bridge_that_never_reconnects_requests_nothing(
        remote: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        await _drive_bridge(monkeypatch, drops=0)

    anyio.run(scenario)
    assert _RecordingRefresher.instances[0].requests == 0


def test_a_local_bridge_reconnects_without_any_refresher(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        conns = await _drive_bridge(monkeypatch, drops=1)
        assert len(conns) == 2

    anyio.run(scenario)
    assert _RecordingRefresher.instances == []

