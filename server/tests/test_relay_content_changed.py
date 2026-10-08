"C7: a relay handles the daemon's `content_changed` push by re-fetching the bundle.\n\nThe daemon pushes `notifications/agent-context/content_changed` with STORE-relative paths\n(`global/docs/a.md`). The relay's bridge consumes that notification and does not forward it.\nA `ContentRefresher` re-fetches the full bundle over get_materialized, rewrites the local\nfiles, updates the cache, and re-runs the settings sync only when `hooks/` or `scripts/`\nchanged. Bursts coalesce into one fetch; the blocking work runs off the event loop; every\nfailure degrades to a logged warning."
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCResponse,
)

from agent_context import daemon
from agent_context import relay_materialize as R

REMOTE_HOST = "example.invalid"
TOKEN = "s3cret-token"
METHOD = "notifications/agent-context/content_changed"
DEBOUNCE = 0.05

SETTINGS_SCRIPT = (
    "import os, pathlib\n"
    "home = pathlib.Path(os.environ['HOME'])\n"
    "with open(home / 'settings-runs.log', 'a') as f:\n"
    "    f.write('run\\n')\n"
)

BUNDLE: dict[str, str] = {
    "skills/demo/SKILL.md": "# demo skill\n",
    "commands/go.md": "go\n",
    "commands/guide.md": "guide\n",
    "hooks/guard.py": "#!/usr/bin/env python3\nprint('guard')\n",
    "scripts/tool.py": "#!/usr/bin/env python3\nprint('tool')\n",
    "scripts/home-settings-sync.py": SETTINGS_SCRIPT,
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_PORT", "AGENT_CONTEXT_TOKEN",
                 "AGENT_CONTEXT_TRANSPORT", "AGENT_CONTEXT_NO_DAEMON", "AGENT_CONTEXT_STORE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


class FakeDaemon:
    "Stands in for the ls daemon's get_materialized MCP tool: R.fetch_bundle_mcp is patched\n    to answer from here instead of a real MCP round trip."

    def __init__(self) -> None:
        self.status = 200
        self.body: object = {}
        self.delay = 0.0
        self.requests: list[str] = []  

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
        d.requests.append(token)
        if d.delay:
            time.sleep(d.delay)
        if d.status != 200:
            return None
        body = d.body
        if not (isinstance(body, dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in body.items())):
            return None
        return dict(body)
    monkeypatch.setattr(R, "fetch_bundle_mcp", fetch)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    yield d
    d.close()


@pytest.fixture
def primed(home: Path, fake: FakeDaemon) -> Path:
    'A relay home that already holds BUNDLE and its cache, as after a C6 start.'
    R.apply_bundle(BUNDLE, home)
    R.write_cache(BUNDLE, home)
    return home


def _settings_runs(home: Path) -> int:
    log = home / "settings-runs.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


def _notification(method: str, params: dict[str, object]) -> SessionMessage:
    return SessionMessage(JSONRPCMessage(JSONRPCNotification(
        jsonrpc="2.0", method=method, params=params)))


async def _until(predicate: Callable[[], bool], limit: float = 5.0) -> None:
    with anyio.fail_after(limit):
        while not predicate():
            await anyio.sleep(0.01)


async def _with_refresher(
        home: Path, scenario: Callable[[R.ContentRefresher], Awaitable[None]],
        timeout: float = 5.0) -> None:
    refresher = R.ContentRefresher(home, debounce=DEBOUNCE, timeout=timeout)
    async with anyio.create_task_group() as tg:
        tg.start_soon(refresher.run)
        try:
            await scenario(refresher)
        finally:
            tg.cancel_scope.cancel()




@pytest.mark.parametrize("path", [
    "global/hooks/guard.py",
    "global/scripts/tool.py",
    "global/skills/demo/SKILL.md",
    "global/commands/go.md",
    "global/agents/worker.md",
    "projects/example-app/commands/go.md",
])
def test_bundle_paths_affect_the_bundle(path: str) -> None:
    assert R.affects_bundle([path]) is True


@pytest.mark.parametrize("path", [
    "global/memories/some-memory.md",
    "global/instructions/global.md",
    "global/docs/guide.md",
    "projects/example-app/docs/worktrees.md",
    "projects/example-app/memories/note.md",
    "projects/example-app/instructions/project.md",
    "global/hooks",
    "README.md",
    "",
])
def test_other_paths_do_not_affect_the_bundle(path: str) -> None:
    assert R.affects_bundle([path]) is False


def test_one_bundle_path_among_others_is_enough() -> None:
    assert R.affects_bundle(["global/memories/a.md", "global/commands/b.md"]) is True


@pytest.mark.parametrize("path", ["global/docs/guide.md", "global/docs/onboarding/machine-setup.md"])
def test_a_global_docs_path_does_not_affect_the_bundle(path: str) -> None:
    assert R.affects_bundle([path]) is False


def test_no_paths_affect_nothing() -> None:
    assert R.affects_bundle([]) is False




@pytest.mark.parametrize("result", [
    R.ApplyResult(written=["hooks/guard.py"]),
    R.ApplyResult(written=["scripts/tool.py"]),
    R.ApplyResult(pruned=["hooks/old.py"]),
    R.ApplyResult(pruned=["scripts/old.py"]),
    R.ApplyResult(written=["commands/a.md", "hooks/guard.py"]),
])
def test_hooks_or_scripts_changes_need_a_settings_sync(result: R.ApplyResult) -> None:
    assert R.needs_settings_sync(result) is True


@pytest.mark.parametrize("result", [
    R.ApplyResult(),
    R.ApplyResult(written=["skills/demo/SKILL.md", "commands/go.md", "commands/a.md"]),
    R.ApplyResult(pruned=["agents/old.md"]),
    R.ApplyResult(unchanged=["hooks/guard.py", "scripts/tool.py"]),
])
def test_other_changes_do_not_need_a_settings_sync(result: R.ApplyResult) -> None:
    assert R.needs_settings_sync(result) is False




def test_refresh_rewrites_a_changed_file_and_updates_the_cache(
        primed: Path, fake: FakeDaemon) -> None:
    fake.serve({**BUNDLE, "commands/guide.md": "guide v2\n"})
    result = R.refresh(primed, timeout=5.0)
    assert result is not None
    assert result.written == ["commands/guide.md"]
    assert (primed / ".agent-context" / "global" / "commands" / "guide.md").read_text() == "guide v2\n"
    cached = R.read_cache(primed)
    assert cached is not None and cached["commands/guide.md"] == "guide v2\n"
    assert fake.requests == [TOKEN]


def test_refresh_prunes_a_file_the_daemon_dropped(primed: Path, fake: FakeDaemon) -> None:
    dropped = {k: v for k, v in BUNDLE.items() if k != "commands/go.md"}
    assert (primed / ".agent-context" / "global" / "commands" / "go.md").exists()
    fake.serve(dropped)
    result = R.refresh(primed, timeout=5.0)
    assert result is not None and result.pruned == ["commands/go.md"]
    assert not (primed / ".agent-context" / "global" / "commands" / "go.md").exists()


@pytest.mark.parametrize("failure", ["401", "500", "bad-json", "not-a-map", "bad-key"])
def test_refresh_failure_returns_none_and_touches_nothing(
        primed: Path, fake: FakeDaemon, failure: str) -> None:
    before = {p: p.read_bytes() for p in primed.rglob("*") if p.is_file()}
    if failure == "401":
        fake.status = 401
    elif failure == "500":
        fake.status = 500
    elif failure == "bad-json":
        fake.body = "not json"
    elif failure == "not-a-map":
        fake.body = ["a"]
    else:
        fake.serve({**BUNDLE, "../escape.md": "x"})
    assert R.refresh(primed, timeout=5.0) is None
    assert {p: p.read_bytes() for p in primed.rglob("*") if p.is_file()} == before




def test_a_bundle_push_fetches_once_and_rewrites_the_file(primed: Path, fake: FakeDaemon) -> None:
    fake.serve({**BUNDLE, "commands/guide.md": "guide v2\n"})
    guide = primed / ".agent-context" / "global" / "commands" / "guide.md"

    async def scenario(refresher: R.ContentRefresher) -> None:
        refresher.notify(["global/commands/guide.md"])
        await _until(lambda: guide.read_text() == "guide v2\n")
        await anyio.sleep(0.3)

    anyio.run(_with_refresher, primed, scenario)
    assert len(fake.requests) == 1
    cached = R.read_cache(primed)
    assert cached is not None and cached["commands/guide.md"] == "guide v2\n"


def test_a_push_of_non_bundle_paths_fetches_nothing(primed: Path, fake: FakeDaemon) -> None:
    async def scenario(refresher: R.ContentRefresher) -> None:
        refresher.notify(["global/memories/a.md", "projects/example-app/instructions/i.md"])
        await anyio.sleep(0.4)

    anyio.run(_with_refresher, primed, scenario)
    assert fake.requests == []


def test_a_burst_of_pushes_coalesces_into_one_fetch(primed: Path, fake: FakeDaemon) -> None:
    fake.serve({**BUNDLE, "commands/guide.md": "guide v2\n"})

    async def scenario(refresher: R.ContentRefresher) -> None:
        for i in range(5):
            refresher.notify([f"global/commands/guide{i}.md"])
            await anyio.sleep(DEBOUNCE / 10)
        await _until(lambda: len(fake.requests) >= 1)
        await anyio.sleep(0.4)

    anyio.run(_with_refresher, primed, scenario)
    assert len(fake.requests) == 1


def test_a_later_push_triggers_a_second_fetch(primed: Path, fake: FakeDaemon) -> None:
    guide = primed / ".agent-context" / "global" / "commands" / "guide.md"

    async def scenario(refresher: R.ContentRefresher) -> None:
        fake.serve({**BUNDLE, "commands/guide.md": "guide v2\n"})
        refresher.notify(["global/commands/guide.md"])
        await _until(lambda: guide.read_text() == "guide v2\n")
        fake.serve({**BUNDLE, "commands/guide.md": "guide v3\n"})
        refresher.notify(["global/commands/guide.md"])
        await _until(lambda: guide.read_text() == "guide v3\n")

    anyio.run(_with_refresher, primed, scenario)
    assert len(fake.requests) == 2


def test_a_hooks_change_reruns_the_settings_sync_once(primed: Path, fake: FakeDaemon) -> None:
    fake.serve({**BUNDLE, "hooks/guard.py": "#!/usr/bin/env python3\nprint('v2')\n"})

    async def scenario(refresher: R.ContentRefresher) -> None:
        refresher.notify(["global/hooks/guard.py"])
        await _until(lambda: _settings_runs(primed) >= 1)
        await anyio.sleep(0.3)

    anyio.run(_with_refresher, primed, scenario)
    assert _settings_runs(primed) == 1


def test_a_commands_only_change_does_not_rerun_the_settings_sync(
        primed: Path, fake: FakeDaemon) -> None:
    fake.serve({**BUNDLE, "commands/guide.md": "guide v2\n", "skills/demo/SKILL.md": "# v2\n"})
    guide = primed / ".agent-context" / "global" / "commands" / "guide.md"

    async def scenario(refresher: R.ContentRefresher) -> None:
        refresher.notify(["global/commands/guide.md", "global/skills/demo/SKILL.md"])
        await _until(lambda: guide.read_text() == "guide v2\n")
        await anyio.sleep(0.3)

    anyio.run(_with_refresher, primed, scenario)
    assert _settings_runs(primed) == 0


def test_a_failed_fetch_leaves_files_alone_and_the_refresher_keeps_running(
        primed: Path, fake: FakeDaemon, caplog: pytest.LogCaptureFixture) -> None:
    guide = primed / ".agent-context" / "global" / "commands" / "guide.md"

    async def scenario(refresher: R.ContentRefresher) -> None:
        fake.status = 401
        refresher.notify(["global/commands/guide.md"])
        await _until(lambda: len(fake.requests) >= 1)
        await anyio.sleep(0.2)
        assert guide.read_text() == "guide\n"
        fake.serve({**BUNDLE, "commands/guide.md": "guide v2\n"})
        refresher.notify(["global/commands/guide.md"])
        await _until(lambda: guide.read_text() == "guide v2\n")

    anyio.run(_with_refresher, primed, scenario)
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_a_slow_fetch_does_not_block_the_event_loop(primed: Path, fake: FakeDaemon) -> None:
    fake.delay = 0.6
    fake.serve({**BUNDLE, "commands/guide.md": "guide v2\n"})
    guide = primed / ".agent-context" / "global" / "commands" / "guide.md"
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await anyio.sleep(0.01)
            ticks += 1

    async def scenario(refresher: R.ContentRefresher) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(ticker)
            refresher.notify(["global/commands/guide.md"])
            await _until(lambda: guide.read_text() == "guide v2\n")
            tg.cancel_scope.cancel()

    anyio.run(_with_refresher, primed, scenario)
    assert ticks >= 20, f"the loop only ticked {ticks} times during a 0.6 s fetch"




class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def notify(self, paths: list[str]) -> None:
        self.calls.append(paths)


def test_the_push_is_content_changed_paths() -> None:
    item = _notification(METHOD, {"paths": ["global/docs/a.md", "global/commands/b.md"]})
    assert daemon.content_changed_paths(item) == ["global/docs/a.md", "global/commands/b.md"]


@pytest.mark.parametrize("params", [{}, {"paths": "global/docs/a.md"}, {"paths": [1, 2]}])
def test_a_malformed_push_yields_no_paths(params: dict[str, object]) -> None:
    assert daemon.content_changed_paths(_notification(METHOD, params)) == []


def test_other_messages_are_not_content_changed() -> None:
    other = _notification("notifications/tools/list_changed", {})
    reply = SessionMessage(JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=1, result={})))
    assert daemon.content_changed_paths(other) is None
    assert daemon.content_changed_paths(reply) is None
    assert daemon.content_changed_paths(Exception("boom")) is None


def test_a_remote_relay_consumes_the_push_and_hands_it_to_the_refresher(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", REMOTE_HOST)
    recorder = _Recorder()
    item = _notification(METHOD, {"paths": ["global/docs/a.md"]})
    assert daemon.intercept_content_changed(item, recorder) is True
    assert recorder.calls == [["global/docs/a.md"]]


def test_a_remote_relay_still_forwards_every_other_message(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", REMOTE_HOST)
    recorder = _Recorder()
    other = _notification("notifications/tools/list_changed", {})
    reply = SessionMessage(JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=1, result={})))
    assert daemon.intercept_content_changed(other, recorder) is False
    assert daemon.intercept_content_changed(reply, recorder) is False
    assert recorder.calls == []


def test_a_local_relay_forwards_the_push_untouched() -> None:
    recorder = _Recorder()
    item = _notification(METHOD, {"paths": ["global/docs/a.md"]})
    assert daemon.intercept_content_changed(item, recorder) is False
    assert recorder.calls == []


def test_the_bridge_runs_the_refresher_and_intercepts_the_push() -> None:
    import inspect
    src = inspect.getsource(daemon._bridge)
    assert "intercept_content_changed" in src
    assert "ContentRefresher" in src
    assert "refresher.run" in src
