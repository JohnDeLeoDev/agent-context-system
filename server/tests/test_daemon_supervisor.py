'test daemon supervisor.'
import os
import subprocess

import pytest

from agent_context import daemon


@pytest.fixture(autouse=True)
def _no_real_supervisor(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CONTEXT_SUPERVISOR", raising=False)
    monkeypatch.setattr(daemon, "_LAUNCHD_PLIST", tmp_path / "absent.plist")
    monkeypatch.setattr(daemon, "_SYSTEMD_UNIT_FILE", tmp_path / "absent.service")


def test_supervisor_is_detected_from_the_unit_file(monkeypatch, tmp_path):
    assert daemon._supervisor() is None
    plist = tmp_path / "p.plist"
    plist.write_text("")
    unit = tmp_path / "u.service"
    unit.write_text("")
    monkeypatch.setattr(daemon, "_LAUNCHD_PLIST", plist)
    monkeypatch.setattr(daemon, "_SYSTEMD_UNIT_FILE", unit)
    kind = daemon._supervisor()
    assert kind == (("launchd", daemon._LAUNCHD_LABEL) if daemon.sys.platform == "darwin"
                    else ("systemd", daemon._SYSTEMD_UNIT))
    monkeypatch.setenv("AGENT_CONTEXT_SUPERVISOR", "none")
    assert daemon._supervisor() is None


def test_bounce_asks_the_supervisor_instead_of_signalling(monkeypatch):
    calls, kills = [], []
    monkeypatch.setattr(daemon, "_supervisor", lambda: ("launchd", "x.label"))
    monkeypatch.setattr(daemon.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: kills.append(pid))
    monkeypatch.setattr(daemon, "_wait_port", lambda up, secs: True)
    daemon._bounce_daemon()
    assert calls == [["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/x.label"]]
    assert kills == []


def test_bounce_signals_the_port_owner_not_the_recorded_pid(monkeypatch):
    'daemon.info named a dead lazy spawn while another pid held the port.'
    kills = []
    monkeypatch.setattr(daemon, "_supervisor", lambda: None)
    monkeypatch.setattr(daemon, "_port_owner_pid", lambda: 4242)
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"pid": 79081})
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(daemon, "_wait_port", lambda up, secs: True)
    daemon._bounce_daemon()
    assert kills == [(4242, daemon.signal.SIGTERM)]


def test_bounce_falls_back_to_daemon_info_when_the_os_cannot_say(monkeypatch):
    kills = []
    monkeypatch.setattr(daemon, "_supervisor", lambda: None)
    monkeypatch.setattr(daemon, "_port_owner_pid", lambda: None)
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"pid": 79081})
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: kills.append(pid))
    monkeypatch.setattr(daemon, "_wait_port", lambda up, secs: True)
    daemon._bounce_daemon()
    assert kills == [79081]


def test_ss_row_yields_the_listening_pid():
    row = 'LISTEN 0 128 127.0.0.1:8765 0.0.0.0:* users:(("python3",pid=123,fd=6))'
    assert daemon._parse_ss_pid(row) == 123
    assert daemon._parse_ss_pid("nothing here") is None


def test_ensure_daemon_waits_for_a_supervisor_rather_than_spawning(monkeypatch):
    'The flap: port briefly down during a kickstart → a relay must wait, not spawn.'
    spawned = []
    monkeypatch.setattr(daemon, "_supervisor", lambda: ("launchd", "x"))
    states = iter([False, True])           
    monkeypatch.setattr(daemon, "_port_open", lambda: next(states, True))
    monkeypatch.setattr(daemon, "_wait_port", lambda up, secs: True)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert daemon.ensure_daemon() == daemon.URL
    assert spawned == []


def test_gate_timeout_names_the_hung_test(monkeypatch, tmp_path):
    class FakeProc:
        pid = 4321
        returncode = None
        def __init__(self, *a, **k): self.calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("pytest", timeout)
            return "", ("Timeout (>50.0s) from pytest-timeout\n"
                        "File \"tests/test_hang.py\", line 9, in test_it_hangs\n")
        def kill(self): pass
    monkeypatch.setattr(daemon.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(daemon, "_source_files", list)
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is False
    assert "pid 4321" in detail and "test_it_hangs" in detail


def test_gate_timeout_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_GATE_TIMEOUT", "300")
    assert daemon._gate_timeout_secs(120) == 300
    monkeypatch.setenv("AGENT_CONTEXT_GATE_TIMEOUT", "5")
    assert daemon._gate_timeout_secs(120) == 30          
    monkeypatch.delenv("AGENT_CONTEXT_GATE_TIMEOUT")
    assert daemon._gate_timeout_secs(120) == 120


def test_http_daemon_refuses_to_start_when_the_port_is_taken(monkeypatch):
    'A lazy spawn that lost the bind race must not write daemon.info and then die.'
    from agent_context import server as S
    wrote = []
    monkeypatch.setenv("AGENT_CONTEXT_TRANSPORT", "http")
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "write_daemon_info", lambda: wrote.append(1))
    with pytest.raises(SystemExit) as ex:
        S.main()
    assert ex.value.code == 1
    assert wrote == []
