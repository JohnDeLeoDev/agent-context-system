'Shared fixtures and helpers for the relay self-refresh tests (X1 to X9).\n\nNot a test module. The test files import the fixtures from here:\n    from relay_self_refresh_support import *  # noqa: F401,F403'
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_context import relay_update as U

TOKEN = "s3cret-token-for-self-refresh"
CURRENT = '"v2"'
STALE = '"v1"'
NEWER = '"v3"'

ENV_NAMES = ("AGENT_CONTEXT_RELAY_CHECK_SECS", "AGENT_CONTEXT_RELAY_IDLE_SECS",
             "AGENT_CONTEXT_RELAY_EXIT", "AGENT_CONTEXT_RELAY_RESPAWN_WAIT_SECS")


def need(module: object, name: str) -> Any:
    'The implementation adds `name` to `module`; until then the test fails on this assertion.'
    obj = getattr(module, name, None)
    assert obj is not None, f"{getattr(module, '__name__', module)}.{name} is missing"
    return obj


class LsState:
    def __init__(self) -> None:
        self.etag = CURRENT
        self.status: int | None = None
        self.seen: list[tuple[str, dict[str, str]]] = []


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        state: LsState = self.server.state  
        state.seen.append((self.path, dict(self.headers)))
        try:
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._reply(401)
            elif state.status is not None:
                self._reply(state.status)
            elif self.headers.get("If-None-Match") == state.etag:
                self._reply(304, state.etag)
            else:
                self._reply(200, state.etag, b"tarball" * 100)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _reply(self, status: int, etag: str = "", body: bytes = b"") -> None:
        self.send_response(status)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(autouse=True)
def clean_relay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ls(monkeypatch: pytest.MonkeyPatch) -> Iterator[LsState]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    state = LsState()
    server.state = state  
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AGENT_CONTEXT_HOST", "127.0.0.1")
    monkeypatch.setenv("AGENT_CONTEXT_PORT", str(server.server_address[1]))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def home(exec_capable_tmp_path: Path) -> Path:
    h = exec_capable_tmp_path / "home"
    h.mkdir()
    return h


def conditional_gets(ls: LsState) -> list[dict[str, str]]:
    return [h for path, h in ls.seen if path == "/relay-source"]


def etag_file(home: Path) -> Path:
    return home / ".local" / "share" / "agent-context" / "relay-source.etag"


def store_etag(home: Path, etag: str) -> None:
    path = etag_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(etag + "\n")


def write_installer(home: Path, log: Path, *, stores: str | None = CURRENT) -> Path:
    'A counting stand-in installer that records the ETag it installed, like the real one.'
    path = home / ".local" / "bin" / "agent-context-relay-install"
    path.parent.mkdir(parents=True, exist_ok=True)
    store = ""
    if stores is not None:
        store = (f"p = Path({str(etag_file(home))!r}); p.parent.mkdir(parents=True, exist_ok=True); "
                 f"p.write_text({stores!r} + '\\n')\n")
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"log = open({str(log)!r}, 'a')\n"
        "log.write('start\\n'); log.flush()\n"
        f"{store}"
        "log.write('end\\n'); log.flush()\n"
        "sys.exit(0)\n")
    path.chmod(0o755)
    return path


def write_notify(home: Path, log: Path) -> Path:
    'A stand-in `~/.local/bin/notify` that appends its argv, one JSON list per call.'
    path = home / ".local" / "bin" / "notify"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    path.chmod(0o755)
    return path


def notify_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


def count(log: Path, word: str) -> int:
    return log.read_text().splitlines().count(word) if log.is_file() else 0


def wait_for(predicate: Callable[[], object], seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def dead_pid() -> int:
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done.pid


def exit_record(home: Path) -> Path:
    return home / ".cache" / "agent-context" / "relay-exit.json"


def health_record(home: Path) -> Path:
    return home / ".local" / "state" / "agent-context" / "health" / "relay-exit.json"


def alive_record(home: Path) -> Path:
    return home / ".cache" / "agent-context" / "relay-alive.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def child_env(home: Path) -> dict[str, str]:
    return dict(os.environ, HOME=str(home), PYTHONPATH=os.pathsep.join(sys.path))


class Rig:
    'One RelayUpdater with every outside effect recorded and a fake clock.'

    def __init__(self, home: Path, *, rand: float = 0.5, pid: int | None = None,
                 activity: Any = None, now: float = 1_800_000_000.0) -> None:
        self.events: list[str] = []
        self.sleeps: list[float] = []
        self.armed: list[tuple[str, int, str]] = []
        self.exits = 0
        self.gets_at_sleep: list[int] = []
        self.ls: LsState | None = None
        self.now = now
        self.pid = pid if pid is not None else dead_pid()
        self.at_exit: Callable[[], None] | None = None

        def request_exit() -> None:
            self.exits += 1
            self.events.append("exit")
            if self.at_exit is not None:
                self.at_exit()

        def arm_waiter(h: Path, waiter_pid: int, etag: str) -> bool:
            self.armed.append((str(h), waiter_pid, etag))
            self.events.append("arm")
            return True

        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            if self.ls is not None:
                self.gets_at_sleep.append(len(conditional_gets(self.ls)))

        cls = need(U, "RelayUpdater")
        self.updater = cls(home, request_exit=request_exit, arm_waiter=arm_waiter,
                           activity=activity if activity is not None
                           else need(U, "ActivityTracker")(),
                           sleep=sleep, rand=lambda: rand, wall=lambda: self.now, pid=self.pid)
