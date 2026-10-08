"The relay's only materialize path is the daemon's get_materialized MCP tool\n(policy/policy); an old daemon that lacks the tool, or any other failed fetch, is a failed\nmaterialize (see relay_materialize.fetch_bundle_mcp, refresh). A real FastMCP app, served\nover a real socket, stands in for the daemon so the round trip is genuine MCP, not a stub."
from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import uvicorn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from agent_context import daemon
from agent_context import relay_materialize as R
from agent_context.materialize import build_materialized_map
from agent_context.store import ContextStore

TOKEN = "s3cret-token-for-tests"


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    _put(root / "global" / "scripts" / "g.py", "print('g')\n")
    _put(root / "global" / "hooks" / "g.sh", "#!/bin/bash\necho g\n")
    _put(root / "global" / "mcp-servers.json", "{}\n")
    return root


class _UvicornThread(threading.Thread):
    'A real daemon: the actual FastMCP app, on a real loopback socket.'

    def __init__(self, app) -> None:
        super().__init__(daemon=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    @property
    def port(self) -> int:
        return self.server.servers[0].sockets[0].getsockname()[1]

    def wait_started(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("daemon did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(timeout=5.0)


@pytest.fixture
def daemon_app(monkeypatch: pytest.MonkeyPatch, store_root: Path) -> Iterator[ModuleType]:
    "The real server module, its MCP app served on a real port, its store the fixture's."
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    monkeypatch.delenv("AGENT_CONTEXT_ALLOWED_HOSTS", raising=False)
    from agent_context import server as srv

    srv = importlib.reload(srv)
    store = ContextStore(root=str(store_root))
    monkeypatch.setattr(srv, "_get_conn", lambda: store)
    thread = _UvicornThread(srv.mcp.streamable_http_app())
    thread.start()
    thread.wait_started()
    monkeypatch.setattr(daemon, "mcp_url", lambda: f"http://127.0.0.1:{thread.port}/mcp")
    yield srv
    thread.stop()
    importlib.reload(srv)


def test_fetch_bundle_mcp_round_trips_over_real_mcp(daemon_app: ModuleType, store_root: Path) -> None:
    bundle = R.fetch_bundle_mcp(daemon.mcp_url(), TOKEN, timeout=5.0)
    assert bundle == build_materialized_map(str(store_root))


def test_fetch_bundle_mcp_rejects_a_wrong_token_and_warns(
        daemon_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="agent-context"):
        assert R.fetch_bundle_mcp(daemon.mcp_url(), "wrong-token", timeout=5.0) is None
    ours = [r for r in caplog.records if r.name == "agent-context"]
    assert any("get_materialized over MCP failed" in r.getMessage() for r in ours)
    assert "wrong-token" not in caplog.text


def test_fetch_bundle_mcp_returns_none_quietly_when_the_tool_is_absent(
        daemon_app: ModuleType, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    'Old-daemon compat: the tool-absent path is INFO, never WARNING.'
    tools = daemon_app.mcp._tool_manager._tools
    monkeypatch.delitem(tools, "get_materialized")
    with caplog.at_level(logging.INFO, logger="agent-context"):
        assert R.fetch_bundle_mcp(daemon.mcp_url(), TOKEN, timeout=5.0) is None
    ours = [r for r in caplog.records if r.name == "agent-context"]
    assert not [r for r in ours if r.levelno >= logging.WARNING]
    assert any("no get_materialized tool" in r.getMessage() for r in ours)


def test_fetch_bundle_mcp_warns_when_the_tool_is_present_but_errors(
        daemon_app: ModuleType, caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch) -> None:
    "A daemon that HAS the tool but answers with a problem is not the transitional compat\n    case, so it is logged at WARNING (the error's class only, no body, no token). Only the\n    registered tool's own function is broken, not the route's shared helper, so this is\n    the tool erroring, not the underlying data."
    def boom(*a: object, **k: object):
        raise RuntimeError("boom")
    monkeypatch.setattr(daemon_app.mcp._tool_manager._tools["get_materialized"], "fn", boom)
    with caplog.at_level(logging.WARNING, logger="agent-context"):
        assert R.fetch_bundle_mcp(daemon.mcp_url(), TOKEN, timeout=5.0) is None
    ours = [r for r in caplog.records if r.name == "agent-context"]
    assert any("get_materialized over MCP failed" in r.getMessage() for r in ours)
    assert "boom" not in caplog.text


def test_refresh_falls_back_to_nothing_when_the_tool_is_absent(
        daemon_app: ModuleType, home: Path, caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch) -> None:
    'A new relay against an old daemon (get_materialized not registered) treats that as a\n    failed fetch: nothing is written, and refresh reports failure (policy/policy: no fallback).'
    tools = daemon_app.mcp._tool_manager._tools
    monkeypatch.delitem(tools, "get_materialized")
    with caplog.at_level(logging.INFO, logger="agent-context"):
        result = R.refresh(home, timeout=5.0)
    assert result is None
    assert not (home / ".agent-context/global/scripts/g.py").exists()


def test_refresh_fails_and_warns_when_the_tool_errors(
        daemon_app: ModuleType, store_root: Path, home: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    'A daemon that HAS the tool but answers with a problem is a failed fetch too, visible\n    in the log (unlike the tool-absent case).'
    def boom(*a: object, **k: object):
        raise RuntimeError("boom")
    monkeypatch.setattr(daemon_app.mcp._tool_manager._tools["get_materialized"], "fn", boom)
    with caplog.at_level(logging.WARNING, logger="agent-context"):
        result = R.refresh(home, timeout=5.0)
    assert result is None
    assert not (home / ".agent-context/global/scripts/g.py").exists()
    ours = [r for r in caplog.records if r.name == "agent-context"]
    assert any("get_materialized over MCP failed" in r.getMessage() for r in ours)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h
