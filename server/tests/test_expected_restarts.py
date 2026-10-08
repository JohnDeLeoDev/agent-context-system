'A restart the deploy machinery asked for is not a crash-loop start.'
import json
import os
import time

from agent_context import daemon, paths

NOW = 1_800_000_000.0


def test_an_expected_start_is_not_counted_and_uses_up_its_marker():
    daemon.expect_restart(NOW - 5)
    assert daemon.record_start(NOW) == 0
    assert daemon.starts_last_hour(NOW) == 0
    assert daemon.record_start(NOW + 1) == 1          


def test_one_marker_excuses_exactly_one_start():
    daemon.expect_restart(NOW - 5)
    daemon.record_start(NOW)
    daemon.record_start(NOW + 2)
    assert daemon.starts_last_hour(NOW + 2) == 1


def test_two_markers_excuse_two_starts():
    daemon.expect_restart(NOW - 5)
    daemon.expect_restart(NOW - 4)
    daemon.record_start(NOW)
    daemon.record_start(NOW + 2)
    assert daemon.starts_last_hour(NOW + 2) == 0


def test_a_start_with_no_marker_still_counts():
    assert daemon.record_start(NOW) == 1
    assert daemon.record_start(NOW + 10) == 2


def test_an_expired_marker_excuses_nothing():
    daemon.expect_restart(NOW - 400)
    assert daemon.record_start(NOW) == 1
    assert daemon.starts_last_hour(NOW) == 1


def test_a_real_loop_is_still_a_verdict_with_deploy_restarts_mixed_in(monkeypatch):
    now = time.time()
    for i in range(5):                                  
        daemon.record_start(now - 3000 + 100 * i)
    for i in range(3):                                  
        daemon.expect_restart(now - 500 + 100 * i - 5)
        daemon.record_start(now - 500 + 100 * i)
    assert daemon.starts_last_hour(now) == 5
    daemon._write_restart_health(daemon.starts_last_hour(now))
    rec = json.loads((paths.health_dir() / "daemon-restarts.json").read_text())
    assert rec["ok"] is False and "started 5 times in the last hour" in rec["failures"][0]
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"pid": 1, "started_at": now - 2, "code_version": 1.0})
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda: None)
    h = daemon.get_health()
    assert h["verdict"] == "restart-loop" and h["starts_last_hour"] == 5


def test_a_deploy_re_exec_records_an_expected_restart(monkeypatch):
    monkeypatch.setattr(os, "execv", lambda path, argv: None)
    daemon._do_exec()
    assert daemon.record_start() == 0                   


def test_a_supervisor_bounce_records_an_expected_restart(monkeypatch):
    monkeypatch.setattr(daemon, "_supervisor", lambda: ("launchd", "job"))
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_wait_port", lambda *a, **k: True)
    daemon._bounce_daemon()
    assert daemon.record_start() == 0
