"C9b: every harness launches the agent-context relay from the manifest's `command`. It used to be\nthe clone's venv script, which a machine without a clone does not have. `{RELAY}` expands to the\nstandalone install (~/.local/bin/agent-context, from agent-context-relay-install) when it exists\nand to the clone's venv script otherwise, so one manifest serves both kinds of machine."
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

STORE = Path(__file__).resolve().parents[2]
SCRIPT = STORE / "global" / "scripts" / "harness-materialize.py"
MANIFEST = STORE / "global" / "mcp-servers.json"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("harness_materialize_for_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HM = _load()


@pytest.fixture
def home(exec_capable_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    
    
    
    monkeypatch.setattr(HM, "HOME", str(exec_capable_tmp_path))
    return exec_capable_tmp_path


def _install_relay(home: Path, *, executable: bool = True) -> Path:
    path = home / ".local" / "bin" / "agent-context"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755 if executable else 0o644)
    return path


def _clone_script(home: Path) -> str:
    return str(home / ".agent-context" / "server" / ".venv" / "bin" / "agent-context")




def test_relay_expands_to_the_standalone_install_when_it_exists(home: Path) -> None:
    installed = _install_relay(home)
    assert HM.subst("{RELAY}") == str(installed)


def test_relay_expands_to_the_clone_script_when_there_is_no_install(home: Path) -> None:
    assert HM.subst("{RELAY}") == _clone_script(home)


def test_a_non_executable_install_does_not_count(home: Path) -> None:
    _install_relay(home, executable=False)
    assert HM.subst("{RELAY}") == _clone_script(home)


def test_a_directory_named_agent_context_does_not_count(home: Path) -> None:
    (home / ".local" / "bin" / "agent-context").mkdir(parents=True)
    assert HM.subst("{RELAY}") == _clone_script(home)


def test_relay_expands_inside_an_argv_list(home: Path) -> None:
    installed = _install_relay(home)
    assert HM.subst(["{RELAY}", "--flag"]) == [str(installed), "--flag"]


def test_the_choice_follows_the_machine_at_each_call(home: Path) -> None:
    before = HM.subst("{RELAY}")
    installed = _install_relay(home)
    assert before == _clone_script(home)
    assert HM.subst("{RELAY}") == str(installed)


def test_home_expansion_is_unchanged(home: Path) -> None:
    assert HM.subst("{HOME}/x") == f"{home}/x"
    assert HM.subst(["{HOME}/a", "b"]) == [f"{home}/a", "b"]




def _servers() -> dict[str, dict]:
    return json.loads(MANIFEST.read_text())["servers"]


def test_the_agent_context_server_uses_the_relay_placeholder() -> None:
    assert _servers()["agent-context"]["command"] == "{RELAY}"


def test_no_other_server_uses_the_placeholder() -> None:
    others = {n: s for n, s in _servers().items() if n != "agent-context"}
    assert not [n for n, s in others.items() if "{RELAY}" in json.dumps(s)]


def test_the_agent_context_server_keeps_its_harnesses() -> None:
    assert set(_servers()["agent-context"]["harnesses"]) == {
        "claude", "claude-desktop", "opencode", "copilot", "antigravity", "xcode", "codex"}




def test_claude_rendering_uses_the_installed_relay(home: Path) -> None:
    installed = _install_relay(home)
    spec = dict(_servers()["agent-context"])
    entry = HM.render_claude(spec, {})
    assert entry["command"] == str(installed)


def test_claude_rendering_falls_back_to_the_clone_script(home: Path) -> None:
    spec = dict(_servers()["agent-context"])
    assert HM.render_claude(spec, {})["command"] == _clone_script(home)


def test_opencode_rendering_uses_the_installed_relay(home: Path) -> None:
    installed = _install_relay(home)
    spec = dict(_servers()["agent-context"])
    assert HM.render_opencode(spec, {})["command"][0] == str(installed)


def test_copilot_rendering_uses_the_installed_relay(home: Path) -> None:
    installed = _install_relay(home)
    spec = dict(_servers()["agent-context"])
    assert HM.render_copilot(spec, {})["command"] == str(installed)


def test_rendering_twice_gives_the_same_result(home: Path) -> None:
    _install_relay(home)
    spec = dict(_servers()["agent-context"])
    assert HM.render_claude(spec, {}) == HM.render_claude(spec, {})


def test_the_install_is_found_by_its_mode_not_only_its_name(home: Path) -> None:
    path = _install_relay(home)
    assert stat.S_IMODE(path.stat().st_mode) & 0o111
    assert os.access(path, os.X_OK)
