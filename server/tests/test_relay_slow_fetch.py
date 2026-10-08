"On a loaded laptop a healthy ls daemon took 12-20 s to answer get_materialized, the 10 s\nfetch timed out, and the relay served the degraded, no-tools relay for the whole session.\nA timeout now leads to a plain HTTP probe: a daemon that answers gets the bridge and a\nbackground re-fetch with longer timeouts; one that does not still gets the degraded relay\n(policy). Every other fetch failure is unchanged.\n\nThe degraded relay also answers Claude Code's `server/discover` probe with method-not-found,\nas the bridge does, instead of the SDK's -32602 and a validation warning in the MCP log."
from __future__ import annotations

import asyncio
import http.server
import json
import logging
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_context import daemon
from agent_context import relay_materialize as R

TOKEN = "s3cret-token"
URL = "https://example.invalid/mcp"
BUNDLE: dict[str, str] = {
    "commands/guide.md": "guide\n",
    "scripts/home-settings-sync.py": "import os, pathlib\n"
                                     "p = pathlib.Path(os.environ['HOME']) / 'settings-runs.log'\n"
                                     "p.open('a').write('run\\n')\n",
    "scripts/home-materialize.py": "#!/usr/bin/env python3\n",
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    monkeypatch.setattr(daemon, "mcp_url", lambda: URL)
    return h


class Fetches:
    'Stands in for fetch_bundle_mcp: each call takes the next outcome, a bundle, None, or\n    FetchTimeout, and records the timeout it was given.'

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.timeouts: list[float] = []

    def __call__(self, url: str, token: str, timeout: float) -> dict[str, str] | None:
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if outcome is R.FetchTimeout:
            raise R.FetchTimeout
        return outcome  


@pytest.fixture
def retries(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    started: list[Path] = []
    monkeypatch.setattr(R, "_retry_in_background", started.append)
    return started


def _probe(monkeypatch: pytest.MonkeyPatch, answers: bool) -> list[str]:
    probed: list[str] = []
    monkeypatch.setattr(daemon, "daemon_answers",
                        lambda url, timeout: probed.append(url) or answers)
    return probed




def test_a_fetch_that_times_out_raises_fetch_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(url: str, token: str) -> None:
        await asyncio.sleep(30)
    monkeypatch.setattr(R, "_call_get_materialized", hang)
    with pytest.raises(R.FetchTimeout):
        R.fetch_bundle_mcp(URL, TOKEN, timeout=0.1)


def test_any_other_fetch_failure_still_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(url: str, token: str) -> None:
        raise R.BundleError("get_materialized answered with an error")
    monkeypatch.setattr(R, "_call_get_materialized", fail)
    assert R.fetch_bundle_mcp(URL, TOKEN, timeout=5.0) is None


def test_refresh_still_turns_a_timeout_into_none(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "fetch_bundle_mcp", Fetches(R.FetchTimeout))
    assert R.refresh(home) is None
    assert not (home / ".agent-context").exists()




def test_a_timeout_against_a_daemon_that_answers_serves_tools_and_retries(
        home: Path, monkeypatch: pytest.MonkeyPatch, retries: list[Path]) -> None:
    monkeypatch.setattr(R, "fetch_bundle_mcp", Fetches(R.FetchTimeout))
    probed = _probe(monkeypatch, True)
    assert R.materialize_on_start(home) is True
    assert probed == [URL]
    assert retries == [home]


def test_a_timeout_against_a_daemon_that_does_not_answer_still_degrades(
        home: Path, monkeypatch: pytest.MonkeyPatch, retries: list[Path]) -> None:
    monkeypatch.setattr(R, "fetch_bundle_mcp", Fetches(R.FetchTimeout))
    _probe(monkeypatch, False)
    assert R.materialize_on_start(home) is False
    assert retries == []


def test_a_failure_that_is_not_a_timeout_never_probes(
        home: Path, monkeypatch: pytest.MonkeyPatch, retries: list[Path]) -> None:
    monkeypatch.setattr(R, "fetch_bundle_mcp", Fetches(None))
    probed = _probe(monkeypatch, True)
    assert R.materialize_on_start(home) is False
    assert probed == [] and retries == []


def test_a_timeout_writes_nothing_before_the_retry(
        home: Path, monkeypatch: pytest.MonkeyPatch, retries: list[Path]) -> None:
    monkeypatch.setattr(R, "fetch_bundle_mcp", Fetches(R.FetchTimeout))
    _probe(monkeypatch, True)
    R.materialize_on_start(home)
    assert not (home / ".agent-context").exists()




def test_the_retry_applies_the_bundle_once_a_fetch_answers(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = Fetches(R.FetchTimeout, BUNDLE)
    monkeypatch.setattr(R, "fetch_bundle_mcp", fetches)
    assert R._retry_materialize(home, timeouts=(1.0, 2.0, 3.0)) is True
    assert fetches.timeouts == [1.0, 2.0]
    assert R.target_for("commands/guide.md", home).read_text() == "guide\n"
    assert R.read_cache(home) == BUNDLE
    assert (home / "settings-runs.log").read_text() == "run\n"


def test_the_retry_gives_up_after_every_timeout_and_writes_nothing(
        home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    fetches = Fetches(R.FetchTimeout, R.FetchTimeout)
    monkeypatch.setattr(R, "fetch_bundle_mcp", fetches)
    with caplog.at_level(logging.WARNING):
        assert R._retry_materialize(home, timeouts=(1.0, 2.0)) is False
    assert fetches.timeouts == [1.0, 2.0]
    assert not (home / ".agent-context").exists()
    assert "gave up" in caplog.text
    assert TOKEN not in caplog.text


def test_the_retry_stops_at_a_failure_that_is_not_a_timeout(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = Fetches(None, BUNDLE)
    monkeypatch.setattr(R, "fetch_bundle_mcp", fetches)
    assert R._retry_materialize(home, timeouts=(1.0, 2.0)) is False
    assert fetches.timeouts == [1.0]


def test_the_retry_timeouts_are_longer_than_the_start_timeout() -> None:
    assert all(t > 10.0 for t in R._RETRY_TIMEOUTS)
    assert list(R._RETRY_TIMEOUTS) == sorted(R._RETRY_TIMEOUTS)


def test_the_background_retry_runs_off_the_calling_thread(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = threading.Event()
    threads: list[str] = []

    def record(root: Path) -> bool:
        threads.append(threading.current_thread().name)
        ran.set()
        return True
    monkeypatch.setattr(R, "_retry_materialize", record)
    R._retry_in_background(home)
    assert ran.wait(5.0)
    assert threads == ["relay-materialize-retry"]




@pytest.fixture
def refusing_server() -> Iterator[str]:
    'A loopback HTTP server that refuses every request with 401, as ls does without a token.'
    class Refuse(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    finally:
        server.shutdown()
        server.server_close()


def test_a_daemon_that_refuses_the_probe_still_answers(refusing_server: str) -> None:
    assert daemon.daemon_answers(refusing_server, timeout=5.0) is True


def test_a_closed_port_does_not_answer() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert daemon.daemon_answers(f"http://127.0.0.1:{port}/mcp", timeout=2.0) is False


def test_a_listener_that_never_answers_times_out_as_not_answering() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert daemon.daemon_answers(f"http://127.0.0.1:{port}/mcp", timeout=0.3) is False




def test_the_degraded_relay_answers_server_discover_with_method_not_found(home: Path) -> None:
    lines = [
        {"jsonrpc": "2.0", "id": "probe", "method": "server/discover", "params": {"_meta": {}}},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "obs-490", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_CONTEXT_")}
    env.update(HOME=str(home), AGENT_CONTEXT_HOST="ls-down.invalid", AGENT_CONTEXT_TOKEN=TOKEN,
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.Popen([sys.executable, "-m", "agent_context.server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    replies: dict[object, dict[str, object]] = {}
    try:
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in lines))
        proc.stdin.flush()
        for line in proc.stdout:  
            if line.startswith("{"):
                message = json.loads(line)
                replies[message.get("id")] = message
            if 2 in replies:
                break
        proc.stdin.close()
        stderr = proc.stderr.read()
        assert proc.wait(timeout=60) == 0, stderr
    finally:
        proc.kill()
        proc.wait()
    assert replies["probe"]["error"] == {"code": -32601, "message": "Method not found: server/discover"}
    assert "unreachable" in str(replies[1]["result"]).lower()
    assert replies[2]["result"] == {"tools": []}
    assert "Failed to validate" not in stderr
