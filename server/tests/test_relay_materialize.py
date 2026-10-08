"C6: a relay on a non-ls machine fetches the bundle over get_materialized (MCP) and writes\nit locally.\n\nThe bundle is a `{key: content}` map. Keys are prefixed `skills/`, `commands/`, `agents/`,\n`hooks/` or `scripts/`. The relay maps each prefix to the layout the store's own\nprojection uses: skills, commands and agents under `~/.claude`;\nhooks and scripts under `~/.agent-context/global`. No `.git`\nlands there, so this is not a clone. `settings.json` is not in the bundle: the relay runs the\nfetched `home-settings-sync.py` afterwards. The raw bundle is cached for C8's ls-down path.\n\nOnly the ls daemon may write the store, so every failure here degrades to a logged warning\nand never blocks the MCP bridge."
import json
import logging
import stat
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_context import daemon
from agent_context import relay_materialize as R
from agent_context import server as S

REMOTE_HOST = "example.invalid"
TOKEN = "s3cret-token"

SETTINGS_SCRIPT = (
    "import json, os, pathlib\n"
    "home = pathlib.Path(os.environ['HOME'])\n"
    "out = home / '.claude' / 'settings.json'\n"
    "out.parent.mkdir(parents=True, exist_ok=True)\n"
    "out.write_text(json.dumps({'home': str(home)}))\n"
)

BUNDLE: dict[str, str] = {
    "skills/demo/SKILL.md": "# demo skill\n",
    "commands/go.md": "go\n",
    "agents/worker.md": "worker\n",
    "commands/guide.md": "guide\n",
    "commands/proj/cmd.md": "project doc\n",
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
        self.body: dict[str, str] = {}
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
            time.sleep(min(d.delay, timeout + 0.1))
            if d.delay > timeout:
                return None
        return d.body if d.status == 200 else None
    monkeypatch.setattr(R, "fetch_bundle_mcp", fetch)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    yield d
    d.close()


def _mtimes(home: Path) -> dict[str, int]:
    return {str(p.relative_to(home)): p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}




@pytest.mark.parametrize("key, rel", [
    ("skills/demo/SKILL.md", ".agent-context/global/skills/demo/SKILL.md"),
    ("commands/go.md", ".agent-context/global/commands/go.md"),
    ("agents/worker.md", ".agent-context/global/agents/worker.md"),
    ("commands/guide.md", ".agent-context/global/commands/guide.md"),
    ("commands/proj/cmd.md", ".agent-context/global/commands/proj/cmd.md"),
    ("hooks/guard.py", ".agent-context/global/hooks/guard.py"),
    ("scripts/tool.py", ".agent-context/global/scripts/tool.py"),
])
def test_prefixes_map_to_the_store_shaped_layout(home: Path, key: str, rel: str) -> None:
    assert R.target_for(key, home) == home / rel


@pytest.mark.parametrize("key", [
    "/etc/passwd",
    "../escape.md",
    "skills/../../escape.md",
    "docs/a/../../../escape.md",
    "mystery/file.md",
    "skills",
    "skills/",
    "",
])
def test_unsafe_or_unknown_keys_are_rejected(home: Path, key: str) -> None:
    with pytest.raises(R.BundleError):
        R.target_for(key, home)




def test_cache_lives_under_the_home_cache_dir_and_round_trips(home: Path) -> None:
    assert R.cache_path(home) == home / ".cache/agent-context/materialized.json"
    R.write_cache(BUNDLE, home)
    assert json.loads(R.cache_path(home).read_text()) == BUNDLE
    assert R.read_cache(home) == BUNDLE


def test_cache_write_is_atomic_and_leaves_no_temp_files(home: Path) -> None:
    R.write_cache({"docs/a.md": "one"}, home)
    R.write_cache({"docs/a.md": "two"}, home)
    assert sorted(p.name for p in R.cache_path(home).parent.iterdir()) == ["materialized.json"]
    assert R.read_cache(home) == {"docs/a.md": "two"}


def test_read_cache_is_none_when_missing_or_corrupt(home: Path) -> None:
    assert R.read_cache(home) is None
    path = R.cache_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("{truncated")
    assert R.read_cache(home) is None




def test_apply_writes_every_entry_at_its_target(home: Path) -> None:
    result = R.apply_bundle(BUNDLE, home)
    for key, content in BUNDLE.items():
        assert R.target_for(key, home).read_text() == content
    assert sorted(result.written) == sorted(BUNDLE)
    assert result.unchanged == [] and result.pruned == []
    assert not (home / ".agent-context/.git").exists()


def test_hooks_and_scripts_are_executable_and_commands_are_not(home: Path) -> None:
    R.apply_bundle(BUNDLE, home)
    for key in ("hooks/guard.py", "scripts/tool.py"):
        assert R.target_for(key, home).stat().st_mode & stat.S_IXUSR
    for key in ("commands/guide.md", "skills/demo/SKILL.md", "agents/worker.md"):
        assert not R.target_for(key, home).stat().st_mode & stat.S_IXUSR


def test_a_second_apply_rewrites_nothing(home: Path) -> None:
    R.apply_bundle(BUNDLE, home)
    before = _mtimes(home)
    result = R.apply_bundle(BUNDLE, home)
    assert result.written == [] and result.pruned == []
    assert sorted(result.unchanged) == sorted(BUNDLE)
    assert _mtimes(home) == before


def test_a_changed_entry_is_rewritten_and_the_rest_left_alone(home: Path) -> None:
    R.apply_bundle(BUNDLE, home)
    before = _mtimes(home)
    result = R.apply_bundle({**BUNDLE, "commands/guide.md": "guide v2\n"}, home)
    assert result.written == ["commands/guide.md"]
    assert R.target_for("commands/guide.md", home).read_text() == "guide v2\n"
    after = _mtimes(home)
    changed = [k for k in after if after[k] != before.get(k)]
    assert changed == [".agent-context/global/commands/guide.md"]


def test_entries_removed_from_the_bundle_are_pruned_only_if_this_relay_wrote_them(
        home: Path) -> None:
    local_skill = home / ".claude/skills/mine/SKILL.md"
    local_skill.parent.mkdir(parents=True)
    local_skill.write_text("hand-installed\n")

    R.apply_bundle(BUNDLE, home)
    R.write_cache(BUNDLE, home)
    smaller = {k: v for k, v in BUNDLE.items() if k != "skills/demo/SKILL.md"}
    result = R.apply_bundle(smaller, home)

    assert result.pruned == ["skills/demo/SKILL.md"]
    assert not R.target_for("skills/demo/SKILL.md", home).exists()
    assert not (home / ".agent-context/global/skills/demo").exists()  
    assert local_skill.read_text() == "hand-installed\n"
    assert (home / ".agent-context/global/skills").is_dir()


def test_an_unsafe_key_rejects_the_whole_bundle_before_any_write(home: Path) -> None:
    with pytest.raises(R.BundleError):
        R.apply_bundle({"docs/ok.md": "fine", "../escape.md": "bad"}, home)
    assert not R.target_for("docs/ok.md", home).exists()
    assert not (home.parent / "escape.md").exists()


def test_a_poisoned_cache_cannot_make_prune_delete_outside_the_targets(
        home: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    cache = R.cache_path(home)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"../victim.txt": "x", "/etc/hosts": "x"}))
    result = R.apply_bundle({"docs/a.md": "a"}, home)
    assert victim.read_text() == "keep me"
    assert result.pruned == []




def test_settings_sync_runs_the_fetched_script_with_the_relay_home(home: Path) -> None:
    R.apply_bundle(BUNDLE, home)
    assert R.run_settings_sync(home) is True
    assert json.loads((home / ".claude/settings.json").read_text()) == {"home": str(home)}


def test_settings_sync_is_false_when_the_script_is_absent(home: Path) -> None:
    assert R.run_settings_sync(home) is False


def test_settings_sync_is_false_when_the_script_fails(home: Path) -> None:
    R.apply_bundle({"scripts/home-settings-sync.py": "import sys\nsys.exit(3)\n"}, home)
    assert R.run_settings_sync(home) is False


def test_settings_sync_is_time_boxed(home: Path) -> None:
    R.apply_bundle({"scripts/home-settings-sync.py": "import time\ntime.sleep(30)\n"}, home)
    started = time.monotonic()
    assert R.run_settings_sync(home, timeout=0.5) is False
    assert time.monotonic() - started < 5.0




def test_materialize_on_start_fetches_writes_syncs_settings_and_caches(
        fake: FakeDaemon, home: Path) -> None:
    assert R.materialize_on_start(home) is True
    for key, content in BUNDLE.items():
        assert R.target_for(key, home).read_text() == content
    assert json.loads((home / ".claude/settings.json").read_text()) == {"home": str(home)}
    assert R.read_cache(home) == BUNDLE
    assert fake.requests == [TOKEN]


def test_materialize_on_start_defaults_to_the_users_home(
        fake: FakeDaemon, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    assert R.materialize_on_start() is True
    assert (home / ".agent-context/global/commands/guide.md").read_text() == "guide\n"


def test_materialize_on_start_prunes_what_the_daemon_dropped(
        fake: FakeDaemon, home: Path) -> None:
    assert R.materialize_on_start(home) is True
    fake.serve({k: v for k, v in BUNDLE.items() if k != "commands/guide.md"})
    assert R.materialize_on_start(home) is True
    assert not R.target_for("commands/guide.md", home).exists()
    assert "commands/guide.md" not in (R.read_cache(home) or {})


def test_a_rejected_token_warns_writes_nothing_and_keeps_the_old_cache(
        fake: FakeDaemon, home: Path, caplog: pytest.LogCaptureFixture) -> None:
    R.write_cache({"docs/old.md": "old"}, home)
    fake.status = 401
    with caplog.at_level(logging.WARNING):
        assert R.materialize_on_start(home) is False
    assert not (home / ".claude").exists() and not (home / ".agent-context").exists()
    assert R.read_cache(home) == {"docs/old.md": "old"}
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert TOKEN not in caplog.text


def test_an_unsafe_bundle_writes_nothing_and_keeps_the_old_cache(
        fake: FakeDaemon, home: Path) -> None:
    R.write_cache({"docs/old.md": "old"}, home)
    fake.serve({"docs/ok.md": "fine", "../escape.md": "bad"})
    assert R.materialize_on_start(home) is False
    assert not (home / ".agent-context").exists()
    assert R.read_cache(home) == {"docs/old.md": "old"}


def test_a_hung_daemon_returns_within_the_timeout_and_writes_nothing(
        fake: FakeDaemon, home: Path) -> None:
    fake.delay = 30.0
    started = time.monotonic()
    assert R.materialize_on_start(home, timeout=0.5) is False
    assert time.monotonic() - started < 5.0
    assert not (home / ".agent-context").exists()


def test_no_token_means_no_request_and_no_writes(
        fake: FakeDaemon, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN")
    assert R.materialize_on_start(home) is False
    assert fake.requests == []
    assert not (home / ".agent-context").exists()


def test_a_failing_settings_sync_does_not_undo_the_materialized_files(
        fake: FakeDaemon, home: Path) -> None:
    fake.serve({**BUNDLE, "scripts/home-settings-sync.py": "import sys\nsys.exit(1)\n"})
    assert R.materialize_on_start(home) is True
    assert R.target_for("commands/guide.md", home).read_text() == "guide\n"
    assert R.read_cache(home) is not None




@pytest.fixture
def start_order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []
    monkeypatch.setattr(daemon, "ensure_daemon", lambda: order.append("ensure") or "url")
    monkeypatch.setattr(daemon, "run_bridge", lambda: order.append("bridge"))
    monkeypatch.setattr(R, "materialize_on_start",
                        lambda *a, **k: order.append("materialize") or True)
    return order


def test_remote_relay_materializes_after_connecting_and_before_the_bridge(
        start_order: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", REMOTE_HOST)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    S.main()
    assert start_order == ["ensure", "materialize", "bridge"]


def test_a_crash_in_materialize_never_stops_the_bridge(
        start_order: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> bool:
        start_order.append("materialize")
        raise RuntimeError("disk full")
    monkeypatch.setattr(R, "materialize_on_start", boom)
    monkeypatch.setenv("AGENT_CONTEXT_HOST", REMOTE_HOST)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    S.main()
    assert start_order == ["ensure", "materialize", "bridge"]


def test_local_relay_never_materializes(start_order: list[str]) -> None:
    S.main()
    assert start_order == ["ensure", "bridge"]


def test_a_remote_relay_that_cannot_connect_never_materializes(
        start_order: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> str:
        start_order.append("ensure")
        raise RuntimeError("AGENT_CONTEXT_TOKEN is not set")
    monkeypatch.setattr(daemon, "ensure_daemon", refuse)
    monkeypatch.setenv("AGENT_CONTEXT_HOST", REMOTE_HOST)
    with pytest.raises(SystemExit):
        S.main()
    assert start_order == ["ensure"]
