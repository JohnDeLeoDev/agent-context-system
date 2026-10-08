"`agent-context refresh` (policy): the push path, run once by an unattended job.\n\nA relay refreshes its bundle on its own only while a session runs it. A job that runs a\nstore script on an idle relay host (token-usage-collect, intellij-server-refresh,\nagent-notify-watch, chezmoi's install-packages) calls this first."
from __future__ import annotations

from pathlib import Path

import pytest
from test_relay_neutral_roots import (TOKEN, Served, _calls, _kinds, _neutral, _primed,
                                      bundle_with)

from agent_context import daemon, relay_env
from agent_context import relay_materialize as R
from agent_context import server as S


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_PORT", "AGENT_CONTEXT_TOKEN",
                 "AGENT_CONTEXT_TRANSPORT", "AGENT_CONTEXT_NO_DAEMON", "AGENT_CONTEXT_STORE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(relay_env, "load_relay_env", lambda env: None)
    monkeypatch.setattr(S, "served_release", lambda: "")   


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    s = Served()
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda url, token, timeout: dict(s.bundle))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    return s


def _refresh(monkeypatch: pytest.MonkeyPatch, home: Path, remote: bool = True) -> int:
    monkeypatch.setattr(daemon, "is_remote", lambda: remote)
    return S.refresh_command(home)


def test_a_changed_bundle_is_written_and_projected(
        served: Served, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _primed(home, served.bundle)
    served.bundle = {**served.bundle, "skills/demo/SKILL.md": "# v3\n"}
    assert _refresh(monkeypatch, home) == 0
    assert _neutral(home, "skills/demo/SKILL.md").read_text() == "# v3\n"
    assert _kinds(home) == ["materialize"]


def test_a_current_bundle_runs_neither_script(
        served: Served, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _primed(home, served.bundle)
    assert _refresh(monkeypatch, home) == 0
    assert _calls(home) == []


def test_the_store_host_has_nothing_to_refresh(
        served: Served, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _refresh(monkeypatch, home, remote=False) == 0
    assert _calls(home) == [] and R.read_cache(home) is None


def test_no_answer_from_ls_is_exit_1_and_leaves_the_files(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _primed(home, bundle_with("ok"))
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda url, token, timeout: None)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    assert _refresh(monkeypatch, home) == 1
    assert _neutral(home, "skills/demo/SKILL.md").is_file()


def _release(monkeypatch, served, own):
    from agent_context import relay_swap
    installed = []
    monkeypatch.setattr(S, "served_release", lambda: served)
    monkeypatch.setattr(relay_swap, "own_release", lambda: own)
    monkeypatch.setattr(relay_swap, "install",
                        lambda etag, home=None: installed.append(etag) or Path("/r"))
    return installed


def test_a_newer_served_release_is_installed(
        served: Served, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = _release(monkeypatch, '"new"', '"old"')
    assert _refresh(monkeypatch, home) == 0
    assert installed == ['"new"']


def test_the_current_release_is_not_reinstalled(
        served: Served, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = _release(monkeypatch, '"same"', '"same"')
    assert _refresh(monkeypatch, home) == 0 and installed == []


def test_no_release_is_installed_when_ls_names_none_or_does_not_answer(
        served: Served, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = _release(monkeypatch, "", '"old"')
    assert _refresh(monkeypatch, home) == 0 and installed == []
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda url, token, timeout: None)
    installed = _release(monkeypatch, '"new"', '"old"')
    assert _refresh(monkeypatch, home) == 1 and installed == []


def test_the_store_host_installs_no_release(
        served: Served, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = _release(monkeypatch, '"new"', '"old"')
    assert _refresh(monkeypatch, home, remote=False) == 0 and installed == []


def test_main_routes_the_refresh_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S.sys, "argv", ["agent-context", "refresh"])
    monkeypatch.setattr(S, "refresh_command", lambda: 7)
    with pytest.raises(SystemExit) as exc:
        S.main()
    assert exc.value.code == 7
