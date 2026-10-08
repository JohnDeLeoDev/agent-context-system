'test state dir.'
import json
import os

import pytest

from agent_context import daemon, paths, usage


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local" / "state"))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path), 1))
    return tmp_path


def test_state_dir_is_not_the_log_dir_on_macos(fake_home, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    
    assert daemon._state_dir is paths.state_dir and usage._state_dir is paths.state_dir
    assert daemon._state_dir() != daemon._log_dir()
    assert "Logs" not in str(daemon._state_dir())
    assert "Logs" in str(daemon._log_dir())
    assert usage._state_dir() == daemon._state_dir()


def test_legacy_state_migrates_out_of_the_log_dir(fake_home, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    legacy = fake_home / "Library" / "Logs" / "agent-context"
    legacy.mkdir(parents=True)
    (legacy / "daemon.info").write_text('{"pid": 42}')
    (legacy / "usage.json").write_text('{"since": 1, "e": {}}')

    state = daemon._state_dir()
    assert json.loads((state / "daemon.info").read_text())["pid"] == 42
    assert not (legacy / "daemon.info").exists()

    assert json.loads((usage._state_dir() / "usage.json").read_text())["since"] == 1
    assert not (legacy / "usage.json").exists()


def test_health_dir_is_under_the_neutral_state_dir(fake_home):
    "Health verdicts are the store's, not one harness's: harness_paths.state_dir()\n    is ~/.local/state/agent-context on every platform, and health/ sits beneath it."
    want = fake_home / ".local" / "state" / "agent-context" / "health"
    assert paths.health_dir() == want
    assert daemon._health_dir is paths.health_dir
    assert ".claude" not in paths.health_dir().parts


def test_heartbeat_rebuilds_a_wiped_daemon_info(tmp_path, monkeypatch):
    'The core keys come back from the LIVE process, not from disk.'
    info = tmp_path / "daemon.info"
    monkeypatch.setattr(daemon, "_info_path", lambda: info)
    monkeypatch.setattr(daemon, "_STARTED_VERSION", 1234.5)
    monkeypatch.setattr(daemon, "_STARTED_AT", 1000.0)

    daemon._update_daemon_info(last_successful_sync=99.0)   

    got = json.loads(info.read_text())
    assert got["pid"] == os.getpid()
    assert got["code_version"] == 1234.5       
    assert got["started_at"] == 1000.0
    assert got["exec"]["argv"]
    assert got["last_successful_sync"] == 99.0


def test_heartbeat_leaves_an_intact_daemon_info_alone(tmp_path, monkeypatch):
    info = tmp_path / "daemon.info"
    info.write_text(json.dumps({"pid": 7, "code_version": 1.0, "started_at": 2.0,
                                "exec": {"argv": ["x"]}}))
    monkeypatch.setattr(daemon, "_info_path", lambda: info)
    monkeypatch.setattr(daemon, "_STARTED_VERSION", 999.0)

    daemon._update_daemon_info(last_sync_attempt=5.0)

    got = json.loads(info.read_text())
    assert (got["pid"], got["code_version"]) == (7, 1.0)
    assert got["last_sync_attempt"] == 5.0
