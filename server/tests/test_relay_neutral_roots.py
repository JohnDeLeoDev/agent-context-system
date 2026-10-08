"The relay writes skills, commands and agents to the harness-neutral store layout.\n\n`skills/`, `commands/` and `agents/` bundle keys land under `~/.agent-context/global/<prefix>/`,\nnext to hooks and scripts, and the fetched `home-materialize.py` projects them into each\nharness's own home. The relay runs that script after the settings sync on the start, cached\nand push paths (on a push only when a skills, commands or agents key changed). Files the\nold relay wrote under `~/.claude/{skills,commands,agents}` are removed once the neutral copy\nexists, but only when their bytes still equal what the previous cache recorded."
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from agent_context import relay_materialize as R

TOKEN = "s3cret-token"

SETTINGS_SCRIPT = (
    "import os, pathlib\n"
    "home = pathlib.Path(os.environ['HOME'])\n"
    "with open(home / 'calls.log', 'a') as f:\n"
    "    f.write('settings\\n')\n"
)

_MATERIALIZE_HEAD = (
    "import os, pathlib, sys, time\n"
    "home = pathlib.Path(os.environ['HOME'])\n"
    "skill = home / '.agent-context' / 'global' / 'skills' / 'demo' / 'SKILL.md'\n"
    "with open(home / 'calls.log', 'a') as f:\n"
    "    f.write('materialize home=%s argv=%r store=%r skill=%s\\n' % (\n"
    "        home, sys.argv[1:], os.environ.get('AGENT_CONTEXT_STORE'), skill.exists()))\n"
)
_MATERIALIZE_TAIL = {
    "ok": "",
    "fail": "sys.stderr.write('boom')\nsys.exit(3)\n",
    "hang": "time.sleep(30)\n",
}

BASE: dict[str, str] = {
    "skills/demo/SKILL.md": "# demo skill\n",
    "commands/go.md": "go\n",
    "agents/worker.md": "worker\n",
    "commands/guide.md": "guide\n",
    "hooks/guard.py": "#!/usr/bin/env python3\nprint('guard')\n",
    "scripts/tool.py": "#!/usr/bin/env python3\nprint('tool')\n",
    "scripts/home-settings-sync.py": SETTINGS_SCRIPT,
}


def bundle_with(mode: str | None = "ok") -> dict[str, str]:
    'BASE plus a fake home-materialize.py; mode None leaves the script out.'
    if mode is None:
        return dict(BASE)
    return {**BASE, "scripts/home-materialize.py": _MATERIALIZE_HEAD + _MATERIALIZE_TAIL[mode]}


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


class Served:
    'What the fetch returns; a test edits `bundle` between runs.'

    def __init__(self) -> None:
        self.bundle: dict[str, str] = bundle_with("ok")


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    s = Served()
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda url, token, timeout: dict(s.bundle))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    return s


@pytest.fixture
def no_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> dict[str, str]:
        raise AssertionError("the cached path must not fetch")
    monkeypatch.setattr(R, "fetch_bundle_mcp", refuse)


def _calls(home: Path) -> list[str]:
    log = home / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _kinds(home: Path) -> list[str]:
    return [line.split()[0] for line in _calls(home)]


def _run_home_materialize(home: Path, timeout: float = 10.0) -> bool:
    fn = getattr(R, "run_home_materialize", None)
    assert fn is not None, "relay_materialize.run_home_materialize does not exist"
    return fn(home, timeout)


def _primed(home: Path, bundle: dict[str, str]) -> None:
    'A home that already holds `bundle` and its cache, as after an earlier start.'
    R.apply_bundle(bundle, home)
    R.write_cache(bundle, home)


def _warned_about_home_materialize(caplog: pytest.LogCaptureFixture) -> bool:
    return any(r.levelno >= logging.WARNING and "home-materialize" in r.getMessage()
               for r in caplog.records)


def _neutral(home: Path, rel: str) -> Path:
    return home / ".agent-context" / "global" / rel




@pytest.mark.parametrize("key, rel", [
    ("skills/demo/SKILL.md", ".agent-context/global/skills/demo/SKILL.md"),
    ("commands/go.md", ".agent-context/global/commands/go.md"),
    ("agents/worker.md", ".agent-context/global/agents/worker.md"),
    ("skills/a/b/c.md", ".agent-context/global/skills/a/b/c.md"),
    ("hooks/guard.py", ".agent-context/global/hooks/guard.py"),
    ("scripts/tool.py", ".agent-context/global/scripts/tool.py"),
    ("manifests/mcp-servers.json", ".agent-context/global/mcp-servers.json"),
    ("manifests/hooks-manifest.json", ".agent-context/global/hooks-manifest.json"),
])
def test_target_for_maps_prefixes_to_the_neutral_layout(home: Path, key: str, rel: str) -> None:
    assert R.target_for(key, home) == home / rel


def test_no_root_points_into_dot_claude() -> None:
    assert [p for p, root in R._ROOTS.items() if ".claude" in root] == []


def test_apply_writes_skills_commands_agents_under_the_neutral_root(home: Path) -> None:
    R.apply_bundle(bundle_with(None), home)
    assert _neutral(home, "skills/demo/SKILL.md").is_file()
    assert _neutral(home, "skills/demo/SKILL.md").read_text() == "# demo skill\n"
    assert _neutral(home, "commands/go.md").read_text() == "go\n"
    assert _neutral(home, "agents/worker.md").read_text() == "worker\n"
    assert not (home / ".claude").exists()




def test_run_home_materialize_runs_the_script_with_the_relay_home(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_STORE", str(home / "elsewhere"))
    R.apply_bundle(bundle_with("ok"), home)
    assert _run_home_materialize(home) is True
    assert _calls(home) == [f"materialize home={home} argv=[] store=None skill=True"]


def test_run_home_materialize_is_false_and_warns_when_the_script_fails(
        home: Path, caplog: pytest.LogCaptureFixture) -> None:
    R.apply_bundle(bundle_with("fail"), home)
    with caplog.at_level(logging.WARNING):
        assert _run_home_materialize(home) is False
    assert _warned_about_home_materialize(caplog)


def test_run_home_materialize_is_time_boxed_and_warns(
        home: Path, caplog: pytest.LogCaptureFixture) -> None:
    R.apply_bundle(bundle_with("hang"), home)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert _run_home_materialize(home, timeout=0.5) is False
    assert time.monotonic() - started < 5.0
    assert _warned_about_home_materialize(caplog)


def test_run_home_materialize_is_false_and_warns_when_the_script_is_absent(
        home: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert _run_home_materialize(home) is False
    assert _warned_about_home_materialize(caplog)




def test_start_runs_home_materialize_once_after_files_and_settings(
        served: Served, home: Path) -> None:
    assert R.materialize_on_start(home) is True
    assert _kinds(home) == ["settings", "materialize"]
    assert _calls(home)[1] == f"materialize home={home} argv=[] store=None skill=True"


def test_cached_path_runs_home_materialize_once_after_files_and_settings(
        home: Path, no_fetch: None) -> None:
    R.write_cache(bundle_with("ok"), home)
    assert R.apply_cached(home) is True
    assert _kinds(home) == ["settings", "materialize"]
    assert _calls(home)[1] == f"materialize home={home} argv=[] store=None skill=True"


def test_push_of_a_skill_change_runs_home_materialize_after_the_files(
        served: Served, home: Path) -> None:
    _primed(home, served.bundle)
    served.bundle = {**served.bundle, "skills/demo/SKILL.md": "# v2\n"}
    R._refresh_and_sync(home, 5.0)
    assert _kinds(home) == ["materialize"]
    assert _calls(home)[0].endswith("skill=True")
    assert _neutral(home, "skills/demo/SKILL.md").is_file()
    assert _neutral(home, "skills/demo/SKILL.md").read_text() == "# v2\n"


@pytest.mark.parametrize("key", ["skills/demo/SKILL.md", "commands/go.md", "agents/worker.md"])
def test_push_that_writes_a_projected_key_runs_home_materialize_once(
        served: Served, home: Path, key: str) -> None:
    _primed(home, served.bundle)
    served.bundle = {**served.bundle, key: "changed\n"}
    R._refresh_and_sync(home, 5.0)
    assert _kinds(home) == ["materialize"]


@pytest.mark.parametrize("key", ["skills/demo/SKILL.md", "commands/go.md", "agents/worker.md"])
def test_push_that_prunes_a_projected_key_runs_home_materialize_once(
        served: Served, home: Path, key: str) -> None:
    _primed(home, served.bundle)
    served.bundle = {k: v for k, v in served.bundle.items() if k != key}
    R._refresh_and_sync(home, 5.0)
    assert _kinds(home) == ["materialize"]


def test_push_that_changes_only_hooks_syncs_settings_and_skips_home_materialize(
        served: Served, home: Path) -> None:
    _primed(home, served.bundle)
    served.bundle = {**served.bundle, "hooks/guard.py": "#!/usr/bin/env python3\nprint('v2')\n"}
    R._refresh_and_sync(home, 5.0)
    assert _kinds(home) == ["settings"]


def test_push_that_changes_nothing_runs_neither_script(served: Served, home: Path) -> None:
    _primed(home, served.bundle)
    R._refresh_and_sync(home, 5.0)
    assert _calls(home) == []


def test_push_that_changes_hooks_and_skills_runs_settings_then_home_materialize(
        served: Served, home: Path) -> None:
    _primed(home, served.bundle)
    served.bundle = {**served.bundle, "hooks/guard.py": "#!/usr/bin/env python3\nprint('v2')\n",
                     "skills/demo/SKILL.md": "# v2\n"}
    R._refresh_and_sync(home, 5.0)
    assert _kinds(home) == ["settings", "materialize"]




@pytest.mark.parametrize("mode", ["fail", "hang", "absent"])
def test_start_survives_a_bad_home_materialize(
        served: Served, home: Path, mode: str, caplog: pytest.LogCaptureFixture) -> None:
    served.bundle = bundle_with(None if mode == "absent" else mode)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert R.materialize_on_start(home, timeout=0.5) is True
    assert time.monotonic() - started < 5.0
    assert _kinds(home)[0] == "settings"
    assert R.read_cache(home) == served.bundle
    assert _warned_about_home_materialize(caplog)


@pytest.mark.parametrize("mode", ["fail", "hang", "absent"])
def test_cached_path_survives_a_bad_home_materialize(
        home: Path, no_fetch: None, mode: str, caplog: pytest.LogCaptureFixture) -> None:
    R.write_cache(bundle_with(None if mode == "absent" else mode), home)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert R.apply_cached(home, timeout=0.5) is True
    assert time.monotonic() - started < 5.0
    assert _kinds(home)[0] == "settings"
    assert _warned_about_home_materialize(caplog)


@pytest.mark.parametrize("mode", ["fail", "hang", "absent"])
def test_push_survives_a_bad_home_materialize(
        served: Served, home: Path, mode: str, caplog: pytest.LogCaptureFixture) -> None:
    served.bundle = bundle_with(None if mode == "absent" else mode)
    _primed(home, served.bundle)
    served.bundle = {**served.bundle, "hooks/guard.py": "#!/usr/bin/env python3\nprint('v2')\n",
                     "skills/demo/SKILL.md": "# v2\n"}
    with caplog.at_level(logging.WARNING):
        R._refresh_and_sync(home, 0.5)
    assert _kinds(home)[0] == "settings"
    assert _neutral(home, "skills/demo/SKILL.md").is_file()
    assert _neutral(home, "skills/demo/SKILL.md").read_text() == "# v2\n"
    assert _warned_about_home_materialize(caplog)




def _legacy(home: Path, key: str, content: str) -> Path:
    path = home / ".claude" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _swept(result: R.ApplyResult) -> list[str]:
    swept = getattr(result, "legacy_pruned", None)
    assert swept is not None, "ApplyResult.legacy_pruned does not exist"
    return sorted(swept)


def test_apply_result_starts_with_an_empty_legacy_pruned() -> None:
    assert getattr(R.ApplyResult(), "legacy_pruned", None) == []


@pytest.mark.parametrize("key", ["skills/demo/SKILL.md", "commands/go.md", "agents/worker.md"])
@pytest.mark.parametrize("new_content", ["v1\n", "v2\n"])
def test_a_legacy_file_equal_to_the_cached_bytes_is_removed(
        home: Path, key: str, new_content: str) -> None:
    R.write_cache({key: "v1\n"}, home)
    legacy = _legacy(home, key, "v1\n")
    result = R.apply_bundle({key: new_content}, home)
    assert not legacy.exists()
    assert R.target_for(key, home).read_text() == new_content
    assert _swept(result) == [key]


def test_an_edited_legacy_file_is_left_alone(home: Path) -> None:
    R.write_cache({"skills/demo/SKILL.md": "v1\n"}, home)
    legacy = _legacy(home, "skills/demo/SKILL.md", "v1 plus my edit\n")
    result = R.apply_bundle({"skills/demo/SKILL.md": "v1\n"}, home)
    assert legacy.read_text() == "v1 plus my edit\n"
    assert _swept(result) == []


def test_a_person_file_with_no_cache_entry_is_left_alone(home: Path) -> None:
    R.write_cache({"skills/demo/SKILL.md": "v1\n"}, home)
    legacy = _legacy(home, "skills/demo/SKILL.md", "v1\n")
    mine = _legacy(home, "skills/mine/SKILL.md", "hand-installed\n")
    same_bytes_no_entry = _legacy(home, "commands/go.md", "go\n")
    result = R.apply_bundle({"skills/demo/SKILL.md": "v1\n", "commands/go.md": "go\n"}, home)
    assert not legacy.exists()
    assert mine.read_text() == "hand-installed\n"
    assert same_bytes_no_entry.read_text() == "go\n"
    assert _swept(result) == ["skills/demo/SKILL.md"]


def test_an_emptied_parent_directory_goes_but_the_prefix_root_stays(home: Path) -> None:
    key = "skills/a/b/SKILL.md"
    R.write_cache({key: "v1\n"}, home)
    _legacy(home, key, "v1\n")
    R.apply_bundle({key: "v1\n"}, home)
    assert not (home / ".claude/skills/a").exists()
    assert (home / ".claude/skills").is_dir()


def test_a_parent_directory_holding_a_person_file_stays(home: Path) -> None:
    R.write_cache({"skills/demo/SKILL.md": "v1\n"}, home)
    _legacy(home, "skills/demo/SKILL.md", "v1\n")
    mine = _legacy(home, "skills/demo/notes.md", "mine\n")
    R.apply_bundle({"skills/demo/SKILL.md": "v1\n"}, home)
    assert mine.read_text() == "mine\n"
    assert not (home / ".claude/skills/demo/SKILL.md").exists()


def test_a_second_run_removes_nothing_more(home: Path) -> None:
    bundle = {"skills/demo/SKILL.md": "v1\n", "commands/go.md": "go\n"}
    R.write_cache(bundle, home)
    _legacy(home, "skills/demo/SKILL.md", "v1\n")
    _legacy(home, "commands/go.md", "go\n")
    first = R.apply_bundle(bundle, home)
    R.write_cache(bundle, home)
    second = R.apply_bundle(bundle, home)
    assert _swept(first) == ["commands/go.md", "skills/demo/SKILL.md"]
    assert _swept(second) == []
    assert R.target_for("skills/demo/SKILL.md", home).read_text() == "v1\n"


def test_a_symlink_at_the_legacy_path_is_left_alone(home: Path) -> None:
    R.write_cache({"skills/demo/SKILL.md": "v1\n"}, home)
    real = home / "real.md"
    real.write_text("v1\n")
    link = home / ".claude/skills/demo/SKILL.md"
    link.parent.mkdir(parents=True)
    os.symlink(real, link)
    R.apply_bundle({"skills/demo/SKILL.md": "v1\n"}, home)
    assert link.is_symlink() and real.read_text() == "v1\n"


def test_no_sweep_for_a_key_whose_neutral_file_was_not_written(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    R.write_cache({"skills/demo/SKILL.md": "v1\n"}, home)
    legacy = _legacy(home, "skills/demo/SKILL.md", "v1\n")

    def fail(path: Path, data: bytes, mode: int) -> None:
        raise OSError("disk full")
    monkeypatch.setattr(R, "write_atomic", fail)
    with pytest.raises(OSError):
        R.apply_bundle({"skills/demo/SKILL.md": "v2\n"}, home)
    assert legacy.exists()
    assert legacy.read_text() == "v1\n"


def test_no_sweep_when_a_blocked_bundle_is_refused(home: Path) -> None:
    R.write_cache({"skills/demo/SKILL.md": "v1\n"}, home)
    legacy = _legacy(home, "skills/demo/SKILL.md", "v1\n")
    _neutral(home, "skills/demo/SKILL.md").mkdir(parents=True)
    with pytest.raises(R.BundleError):
        R.apply_bundle({"skills/demo/SKILL.md": "v2\n"}, home)
    assert legacy.exists()
    assert legacy.read_text() == "v1\n"


def test_no_sweep_for_a_key_the_new_bundle_dropped(home: Path) -> None:
    R.write_cache({"skills/demo/SKILL.md": "v1\n", "commands/a.md": "a"}, home)
    legacy = _legacy(home, "skills/demo/SKILL.md", "v1\n")
    result = R.apply_bundle({"commands/a.md": "a"}, home)
    assert legacy.exists()
    assert legacy.read_text() == "v1\n"
    assert _swept(result) == []


def test_start_sweeps_the_legacy_files_the_previous_cache_recorded(
        served: Served, home: Path) -> None:
    served.bundle = bundle_with(None)
    R.write_cache(served.bundle, home)
    legacy = [_legacy(home, key, served.bundle[key])
              for key in ("skills/demo/SKILL.md", "commands/go.md", "agents/worker.md")]
    mine = _legacy(home, "skills/mine/SKILL.md", "hand-installed\n")
    assert R.materialize_on_start(home) is True
    assert [p.exists() for p in legacy] == [False, False, False]
    assert mine.read_text() == "hand-installed\n"


def test_cached_path_sweeps_the_legacy_files_the_cache_recorded(
        home: Path, no_fetch: None) -> None:
    R.write_cache(bundle_with(None), home)
    legacy = _legacy(home, "commands/go.md", "go\n")
    assert R.apply_cached(home) is True
    assert not legacy.exists()
    assert _neutral(home, "commands/go.md").read_text() == "go\n"




def test_manifests_land_beside_the_neutral_roots(home: Path) -> None:
    R.apply_bundle({"manifests/mcp-servers.json": "{}", "skills/demo/SKILL.md": "s"}, home)
    assert (home / ".agent-context/global/mcp-servers.json").read_text() == "{}"


def test_hooks_and_scripts_stay_executable_and_skills_do_not(home: Path) -> None:
    R.apply_bundle(BASE, home)
    for key in ("hooks/guard.py", "scripts/tool.py"):
        assert R.target_for(key, home).stat().st_mode & 0o111 == 0o111
    for key in ("skills/demo/SKILL.md", "commands/go.md", "commands/guide.md"):
        assert not R.target_for(key, home).stat().st_mode & 0o111


def test_a_dropped_commands_key_is_pruned_with_its_emptied_directory(home: Path) -> None:
    bundle = {"commands/proj/a.md": "a", "commands/keep.md": "k"}
    R.apply_bundle(bundle, home)
    R.write_cache(bundle, home)
    result = R.apply_bundle({"commands/keep.md": "k"}, home)
    assert result.pruned == ["commands/proj/a.md"]
    assert not (home / ".agent-context/global/commands/proj").exists()
    assert (home / ".agent-context/global/commands").is_dir()


def test_a_dropped_neutral_skill_is_pruned_and_its_root_stays(home: Path) -> None:
    bundle = {"skills/demo/SKILL.md": "s", "commands/keep.md": "k"}
    R.apply_bundle(bundle, home)
    R.write_cache(bundle, home)
    result = R.apply_bundle({"commands/keep.md": "k"}, home)
    assert result.pruned == ["skills/demo/SKILL.md"]
    assert not _neutral(home, "skills/demo").exists()
    assert _neutral(home, "skills").is_dir()


def test_an_unknown_prefix_is_skipped_and_the_rest_applies(home: Path) -> None:
    result = R.apply_bundle({"future/x.md": "x", "skills/demo/SKILL.md": "s"}, home)
    assert result.skipped == ["future/x.md"]
    assert result.written == ["skills/demo/SKILL.md"]
    assert not (home / "future").exists()


def test_an_unsafe_key_refuses_the_whole_bundle(home: Path) -> None:
    with pytest.raises(R.BundleError):
        R.apply_bundle({"skills/demo/SKILL.md": "s", "skills/../../escape.md": "bad"}, home)
    assert not _neutral(home, "skills/demo/SKILL.md").exists()


def test_the_settings_sync_still_runs_on_start(served: Served, home: Path) -> None:
    assert R.materialize_on_start(home) is True
    assert "settings" in _kinds(home)


@pytest.mark.parametrize("key", ["skills/demo/SKILL.md", "commands/go.md", "agents/worker.md"])
def test_a_projection_generation_behind_a_symlink_is_left_alone(home: Path, key: str) -> None:
    'test a projection generation behind a symlink is left alone.'
    prefix, rest = key.split("/", 1)
    R.write_cache({key: "v1\n"}, home)
    gen = home / ".claude" / (".home-mat-gen-%s-1" % prefix)
    projected = gen / rest
    projected.parent.mkdir(parents=True, exist_ok=True)
    projected.write_text("v1\n")
    (home / ".claude" / prefix).symlink_to(gen.name)
    result = R.apply_bundle({key: "v1\n"}, home)
    assert projected.read_text() == "v1\n"
    assert _swept(result) == []
