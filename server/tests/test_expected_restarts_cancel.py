'A restart that was announced and never happened must not excuse a later crash.\n\nFound in review of test_expected_restarts.py: `_bounce_daemon` recorded its marker before\nit knew a restart would happen, so a failed `launchctl`, or no supervisor and no pid, left\na marker that excused an unrelated crash start for the next 180 seconds. The same holds\nwhen `os.execv` raises. Also: a marker rounded a millisecond into the future must still\ncount as live.'
import os
import types

import pytest

from agent_context import daemon

NOW = 1_800_000_000.0


def test_a_failed_supervisor_restart_leaves_no_marker(monkeypatch):
    monkeypatch.setattr(daemon, "_supervisor", lambda: ("launchd", "job"))
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=113))
    monkeypatch.setattr(daemon, "_wait_port", lambda *a, **k: True)
    daemon._bounce_daemon()
    assert daemon.record_start() == 1               


def test_a_supervisor_that_raises_leaves_no_marker(monkeypatch):
    def boom(*a, **k):
        raise OSError("launchctl missing")

    monkeypatch.setattr(daemon, "_supervisor", lambda: ("launchd", "job"))
    monkeypatch.setattr(daemon.subprocess, "run", boom)
    monkeypatch.setattr(daemon, "_wait_port", lambda *a, **k: True)
    daemon._bounce_daemon()
    assert daemon.record_start() == 1


def test_no_supervisor_and_no_pid_leaves_no_marker(monkeypatch):
    monkeypatch.setattr(daemon, "_supervisor", lambda: None)
    monkeypatch.setattr(daemon, "_port_owner_pid", lambda: None)
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: None)
    daemon._bounce_daemon()
    assert daemon.record_start() == 1


def test_a_failed_signal_leaves_no_marker(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(daemon, "_supervisor", lambda: None)
    monkeypatch.setattr(daemon, "_port_owner_pid", lambda: 4242)
    monkeypatch.setattr(daemon, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(os, "kill", gone)
    daemon._bounce_daemon()
    assert daemon.record_start() == 1


def test_a_failed_exec_leaves_no_marker_and_still_raises(monkeypatch):
    def refuse(path, argv):
        raise OSError("exec format error")

    monkeypatch.setattr(os, "execv", refuse)
    with pytest.raises(OSError):
        daemon._do_exec()
    assert daemon.record_start() == 1


def test_a_marker_a_moment_in_the_future_is_still_live():
    daemon.expect_restart(NOW + 0.4)
    assert daemon.record_start(NOW) == 0
