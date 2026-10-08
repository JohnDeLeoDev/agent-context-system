'test restart loop.'
import json
import logging
import time

from agent_context import daemon, fleet, paths


def test_starts_are_recorded_and_counted_over_the_last_hour():
    now = 1_800_000_000.0
    assert daemon.record_start(now - 2 * 86400) == 1       
    assert daemon.record_start(now - 7200) == 1            
    assert daemon.record_start(now - 3000) == 1            
    assert daemon.record_start(now - 100) == 2
    assert daemon.record_start(now) == 3
    assert daemon.starts_last_hour(now) == 3
    assert daemon.starts_last_hour(now + 3601) == 0        
    
    kept = (paths.state_dir() / "daemon-starts").read_text().split()
    assert len(kept) == 4


def test_a_loop_is_a_verdict_of_its_own_and_a_health_record(monkeypatch):
    now = time.time()
    for i in range(5):
        daemon.record_start(now - 60 * i)
    daemon._write_restart_health(daemon.starts_last_hour(now))
    rec = json.loads((paths.health_dir() / "daemon-restarts.json").read_text())
    assert rec["ok"] is False and rec["component"] == "agent-context daemon"
    assert "started 5 times in the last hour" in rec["failures"][0]
    
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"pid": 1, "started_at": now - 2, "code_version": 1.0})
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda: None)
    h = daemon.get_health()
    assert h["verdict"] == "restart-loop" and h["starts_last_hour"] == 5
    
    daemon._write_restart_health(daemon.starts_last_hour(now + 3601))
    assert json.loads((paths.health_dir() / "daemon-restarts.json").read_text())["ok"] is True


def test_three_starts_in_an_hour_is_not_a_loop():
    now = time.time()
    for i in range(3):
        daemon.record_start(now - 60 * i)
    daemon._write_restart_health(daemon.starts_last_hour(now))
    assert json.loads((paths.health_dir() / "daemon-restarts.json").read_text())["ok"] is True


def test_the_fleet_row_carries_the_count_and_problems_names_it():
    row = json.dumps({"machine_id": "m4", "machine_uuid": "U", "updated_at": 100})
    assert "starts_last_hour" not in json.loads(fleet.with_live(row, starts_last_hour=0))
    live = json.loads(fleet.with_live(row, starts_last_hour=42))
    assert live["starts_last_hour"] == 42
    now = time.time()
    rows = [{"machine_id": "m4", "machine_uuid": "U", "updated_at": now, "age_secs": 5,
             "stale": False, "sleeps": False, "code_current": True, "verdict": "starting",
             "starts_last_hour": 42},
            {"machine_id": "rp", "machine_uuid": "V", "updated_at": now, "age_secs": 5,
             "stale": False, "sleeps": False, "code_current": True, "verdict": "healthy",
             "starts_last_hour": 2}]
    probs = fleet.problems(rows, now=now)
    assert len(probs) == 1 and probs[0].startswith("m4: its daemon has started 42 times")
    assert "restart loop" in probs[0] and "not the first suspect" in probs[0]
    assert fleet.RESTART_LOOP_STARTS == daemon.RESTART_LOOP_STARTS


def test_shutdown_signal_is_logged_before_uvicorn_acts(monkeypatch, caplog):
    import signal

    from agent_context import server

    class Srv:
        def __init__(self):
            self.calls = []

        def handle_exit(self, sig, frame):
            self.calls.append(sig)
    s = Srv()
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"started_at": time.time() - 2})
    monkeypatch.setattr(daemon, "_supervisor", lambda: "launchd")
    server._log_shutdown_signal(s)
    with caplog.at_level(logging.WARNING, logger="agent-context"):
        s.handle_exit(signal.SIGTERM, None)
    assert s.calls == [signal.SIGTERM]
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "shutdown on SIGTERM after 2s up" in msg and "supervisor launchd" in msg
    assert "reloading the job in a loop" in msg
