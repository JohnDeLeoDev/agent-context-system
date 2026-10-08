'Relay write side for project-scope and workspace-scope bundle keys.\n\n`projects/<p>/<rel>` lands at `~/.agent-context/projects/<p>/<rel>` and\n`workspaces/<ws>/<rel>` at `~/.agent-context/workspaces/<ws>/<rel>`. Files are mode 0644 and\nNEVER carry an exec bit: project-materialize restores it from the `.meta.toml` sidecar.\n\nNO NEW EXECUTION PATH: applying project/workspace hooks, scripts and skills only WRITES\nfiles. The relay starts no process for those keys (no subprocess.run, Popen, os.system,\nos.exec*), and `_refresh_and_sync` runs neither settings sync nor home-materialize for a\nresult that wrote only `projects/` or `workspaces/` keys.'
from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_context import relay_materialize as R

SCOPED: dict[str, str] = {
    "projects/p/project.toml": 'name = "p"\n',
    "projects/p/skills/x/SKILL.md": '---\nuuid: "u"\n---\nskill\n',
    "projects/p/skills/x/SKILL.md.meta.toml": 'type = "skill"\n',
    "projects/p/commands/c.md": "cmd\n",
    "projects/p/agents/a.md": "agent\n",
    "projects/p/scripts/run.py": "#!/usr/bin/env python3\nprint('run')\n",
    "projects/p/scripts/run.py.meta.toml": "executable = true\n",
    "projects/p/hooks/guard.sh": "#!/bin/bash\necho guard\n",
    "projects/p/parity/parity.md": "parity\n",
    "projects/p/ship/ship.md": "ship\n",
    "workspaces/ws/workspace.toml": 'name = "ws"\n',
    "workspaces/ws/skills/wx/SKILL.md": "ws skill\n",
    "workspaces/ws/commands/c.md": "ws cmd\n",
}
EXEC_CANDIDATES = ("projects/p/scripts/run.py", "projects/p/hooks/guard.sh")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _target(key: str, home: Path) -> Path | None:
    try:
        return R.target_for(key, home)
    except R.BundleError:
        return None


class ProcessSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> ProcessSpy:
    'Record every attempt to start a process; none of them runs.'
    recorder = ProcessSpy()

    def make(name: str):
        def fake(*args: object, **kwargs: object) -> SimpleNamespace:
            recorder.calls.append(f"{name}{args!r}")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        return fake

    monkeypatch.setattr(R.subprocess, "run", make("subprocess.run"))
    monkeypatch.setattr(R.subprocess, "Popen", make("subprocess.Popen"))
    monkeypatch.setattr(os, "system", make("os.system"))
    for name in ("execv", "execve", "execvp", "execvpe", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, make(f"os.{name}"))
    return recorder




def test_target_for_projects_keeps_the_rest_of_the_key(home: Path) -> None:
    assert _target("projects/p/skills/x/SKILL.md", home) == (
        home / ".agent-context" / "projects" / "p" / "skills" / "x" / "SKILL.md")
    assert _target("projects/p/project.toml", home) == (
        home / ".agent-context" / "projects" / "p" / "project.toml")


def test_target_for_workspaces_keeps_the_rest_of_the_key(home: Path) -> None:
    assert _target("workspaces/ws/skills/wx/SKILL.md", home) == (
        home / ".agent-context" / "workspaces" / "ws" / "skills" / "wx" / "SKILL.md")
    assert _target("workspaces/ws/workspace.toml", home) == (
        home / ".agent-context" / "workspaces" / "ws" / "workspace.toml")


def test_roots_map_projects_and_workspaces() -> None:
    assert R._ROOTS.get("projects") == (".agent-context", "projects")
    assert R._ROOTS.get("workspaces") == (".agent-context", "workspaces")


def test_apply_writes_every_scoped_key_with_its_content(home: Path) -> None:
    result = R.apply_bundle(dict(SCOPED), home)
    assert sorted(result.written) == sorted(SCOPED), f"written={result.written}"
    assert result.skipped == []
    for key, content in SCOPED.items():
        path = home / ".agent-context" / key
        assert path.is_file(), f"{key} not written at {path}"
        assert path.read_text(encoding="utf-8") == content


def test_scoped_files_are_0644_including_scripts_and_hooks(home: Path) -> None:
    R.apply_bundle(dict(SCOPED), home)
    for key in SCOPED:
        path = home / ".agent-context" / key
        assert path.is_file(), f"{key} not written"
        assert path.stat().st_mode & 0o777 == 0o644, f"{key} mode {oct(path.stat().st_mode)}"


def test_an_existing_executable_scoped_file_is_reset_to_0644(home: Path) -> None:
    key = "projects/p/scripts/run.py"
    path = home / ".agent-context" / key
    path.parent.mkdir(parents=True)
    path.write_text(SCOPED[key])
    path.chmod(0o755)
    R.apply_bundle({key: SCOPED[key]}, home)
    assert path.stat().st_mode & 0o777 == 0o644




@pytest.mark.parametrize("path", [
    "projects/p/project.toml",
    "projects/p/skills/x/SKILL.md",
    "projects/p/skills/x/SKILL.md.meta.toml",
    "projects/p/commands/c.md",
    "projects/p/agents/a.md",
    "projects/p/scripts/run.py",
    "projects/p/hooks/guard.sh",
    "projects/p/parity/parity.md",
    "projects/p/ship/ship.md",
    "workspaces/ws/workspace.toml",
    "workspaces/ws/skills/wx/SKILL.md",
    "workspaces/ws/commands/c.md",
])
def test_scoped_store_paths_affect_the_bundle(path: str) -> None:
    assert R._path_affects_bundle(path) is True
    assert R.affects_bundle([path]) is True


@pytest.mark.parametrize("path", [
    "projects/p/memory/m.md",
    "projects/p/instructions/i.md",
    "projects/p/notes.md",
    "workspaces/ws/docs/d.md",
    "workspaces/ws/memory/m.md",
    "workspaces/ws/instructions/i.md",
    "workspaces/ws/agents/a.md",
    "workspaces/ws/scripts/s.py",
    "workspaces/ws/hooks/h.sh",
    "workspaces/ws",
    "projects/p",
])
def test_other_project_and_workspace_paths_do_not_affect_the_bundle(path: str) -> None:
    assert R._path_affects_bundle(path) is False


def test_project_docs_do_not_affect_the_bundle_and_commands_do() -> None:
    assert R._path_affects_bundle("projects/p/docs/d.md") is False
    assert R._path_affects_bundle("projects/p/commands/c.md") is True




def test_a_dropped_project_skill_is_pruned_with_empty_parents_but_not_the_root(
        home: Path) -> None:
    key = "projects/p/skills/x/SKILL.md"
    bundle = {key: "skill\n"}
    R.apply_bundle(bundle, home)
    R.write_cache(bundle, home)
    path = home / ".agent-context" / key
    assert path.is_file(), "scoped key was not written"
    result = R.apply_bundle({}, home)
    assert result.pruned == [key]
    assert not path.exists()
    assert not (home / ".agent-context" / "projects" / "p").exists()
    assert (home / ".agent-context" / "projects").is_dir()


def test_pruning_one_workspace_key_keeps_its_sibling(home: Path) -> None:
    keep, drop = "workspaces/ws/skills/a/SKILL.md", "workspaces/ws/skills/b/SKILL.md"
    first = {keep: "a\n", drop: "b\n"}
    R.apply_bundle(first, home)
    R.write_cache(first, home)
    root = home / ".agent-context" / "workspaces"
    assert (root / "ws" / "skills" / "b" / "SKILL.md").is_file(), "scoped key was not written"
    R.apply_bundle({keep: "a\n"}, home)
    assert not (root / "ws" / "skills" / "b").exists()
    assert (root / "ws" / "skills" / "a" / "SKILL.md").is_file()
    assert root.is_dir()




def test_no_new_execution_path_apply_bundle_only_writes_scoped_keys(
        home: Path, spy: ProcessSpy) -> None:
    result = R.apply_bundle(dict(SCOPED), home)
    assert sorted(result.written) == sorted(SCOPED), "scoped keys were not written"
    assert spy.calls == []


def test_no_new_execution_path_written_scoped_files_have_no_exec_bit(home: Path) -> None:
    R.apply_bundle(dict(SCOPED), home)
    for key in SCOPED:
        path = home / ".agent-context" / key
        assert path.is_file(), f"{key} not written"
        assert path.stat().st_mode & 0o111 == 0, f"{key} has an exec bit"


def test_no_new_execution_path_refresh_starts_no_process_for_scoped_keys(
        home: Path, spy: ProcessSpy, monkeypatch: pytest.MonkeyPatch) -> None:
    
    
    scripts = home / ".agent-context" / "global" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "home-settings-sync.py").write_text("print('settings')\n")
    (scripts / "home-materialize.py").write_text("print('materialize')\n")
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "t")
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda url, token, timeout: dict(SCOPED))
    R._refresh_and_sync(home, 1.0)
    assert (home / ".agent-context" / "projects" / "p" / "hooks" / "guard.sh").is_file(), \
        "scoped keys were not written by the refresh"
    assert spy.calls == []


def test_scoped_only_result_needs_neither_settings_sync_nor_home_materialize() -> None:
    result = R.ApplyResult(written=list(SCOPED), pruned=["projects/p/scripts/old.py"])
    assert R.needs_settings_sync(result) is False
    assert R.needs_home_materialize(result) is False


def test_global_hooks_still_trigger_the_settings_sync(
        home: Path, spy: ProcessSpy, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = home / ".agent-context" / "global" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "home-settings-sync.py").write_text("print('settings')\n")
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "t")
    monkeypatch.setattr(R, "fetch_bundle_mcp",
                        lambda url, token, timeout: {**SCOPED, "hooks/g.sh": "echo\n"})
    R._refresh_and_sync(home, 1.0)
    assert len(spy.calls) == 1 and spy.calls[0].startswith("subprocess.run")




def test_projects_and_workspaces_prefixes_are_known() -> None:
    assert R._is_unknown_prefix("projects/p/project.toml") is False
    assert R._is_unknown_prefix("workspaces/ws/workspace.toml") is False


def test_an_unrelated_unknown_prefix_is_still_skipped_with_one_warning(
        home: Path, caplog: pytest.LogCaptureFixture) -> None:
    bundle = {"futurekind/x": "x\n", "commands/guide.md": "guide\n"}
    with caplog.at_level(logging.WARNING, logger="agent-context"):
        result = R.apply_bundle(bundle, home)
    assert result.skipped == ["futurekind/x"]
    assert result.written == ["commands/guide.md"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_scoped_keys_are_written_and_only_the_unknown_prefix_is_skipped(
        home: Path, caplog: pytest.LogCaptureFixture) -> None:
    bundle = {**SCOPED, "futurekind/x": "x\n"}
    with caplog.at_level(logging.WARNING, logger="agent-context"):
        result = R.apply_bundle(bundle, home)
    assert result.skipped == ["futurekind/x"]
    assert sorted(result.written) == sorted(SCOPED)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1




@pytest.mark.parametrize("key", [
    "projects/../x",
    "projects/p/../../x",
    "workspaces/../x",
    "projects//x",
    "projects/p//x",
    "workspaces//x",
    "projects/./x",
    "/projects/p/x",
    "/workspaces/ws/x",
    "projects\\p\\x",
    "projects/p/",
])
def test_unsafe_scoped_keys_are_refused(key: str, home: Path) -> None:
    with pytest.raises(R.BundleError):
        R.apply_bundle({key: "x\n", "commands/ok.md": "ok\n"}, home)
    assert not (home / ".agent-context" / "global" / "commands" / "ok.md").exists(), \
        "an unsafe key must refuse the whole bundle before any write"
