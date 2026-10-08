'T4.1 — daemon liveness heartbeat + watchdog decision logic.\n\nPure-function tests + monkeypatched globals only: NO real threads, sleeps, execv,\nor writes to the live runtime dir.'
import pytest

from agent_context import daemon

T = 900  



def test_not_stalled_no_notify():
    n, r, _ = daemon.watchdog_decision(now=1500, last_success=1000, threshold=T,
                                       already_notified=False, restart_enabled=False)
    assert n is False and r is False


def test_freshly_stalled_notifies_once():
    n, r, reason = daemon.watchdog_decision(now=2000, last_success=1000, threshold=T,
                                            already_notified=False, restart_enabled=False)
    assert n is True and r is False
    assert "stalled" in reason


def test_still_stalled_already_notified_no_repeat():
    n, r, _ = daemon.watchdog_decision(now=2500, last_success=1000, threshold=T,
                                       already_notified=True, restart_enabled=False)
    assert n is False and r is False


def test_recovered_then_stalled_notifies_again():
    notified = False
    
    n, _, _ = daemon.watchdog_decision(2000, 1000, T, notified, False)
    assert n is True
    notified = notified or n            
    
    n2, _, _ = daemon.watchdog_decision(2100, 1000, T, notified, False)
    assert n2 is False
    
    n3, _, _ = daemon.watchdog_decision(2200, 2190, T, notified, False)
    assert n3 is False
    notified = False                    
    
    n4, _, _ = daemon.watchdog_decision(3200, 2190, T, notified, False)
    assert n4 is True


def test_no_baseline_is_noop():
    n, r, _ = daemon.watchdog_decision(now=9999, last_success=None, threshold=T,
                                       already_notified=False, restart_enabled=True,
                                       last_attempt=None)
    assert n is False and r is False



def test_no_restart_when_disabled_even_if_wedged():
    _, r, _ = daemon.watchdog_decision(now=5000, last_success=1000, threshold=T,
                                       already_notified=True, restart_enabled=False,
                                       last_attempt=1000)
    assert r is False


def test_restart_when_enabled_and_wedged():
    
    _, r, _ = daemon.watchdog_decision(now=5000, last_success=1000, threshold=T,
                                       already_notified=True, restart_enabled=True,
                                       last_attempt=1000)
    assert r is True


def test_no_restart_when_attempts_still_fresh():
    
    _, r, _ = daemon.watchdog_decision(now=5000, last_success=1000, threshold=T,
                                       already_notified=True, restart_enabled=True,
                                       last_attempt=4950)
    assert r is False



def _isolate_info(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "_info_path", lambda: tmp_path / "daemon.info")
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", None)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", None)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ERROR", None)


def test_record_sync_success_updates_heartbeat(monkeypatch, tmp_path):
    _isolate_info(monkeypatch, tmp_path)
    daemon.record_sync_success(now=12345.0)
    assert daemon._LAST_SUCCESSFUL_SYNC == 12345.0
    assert daemon._LAST_SYNC_ATTEMPT == 12345.0
    assert daemon._LAST_SYNC_ERROR is None


def test_record_sync_failure_sets_error_not_success(monkeypatch, tmp_path):
    _isolate_info(monkeypatch, tmp_path)
    daemon.record_sync_success(now=100.0)
    daemon.record_sync_failure(RuntimeError("boom"), now=200.0)
    assert daemon._LAST_SUCCESSFUL_SYNC == 100.0     
    assert daemon._LAST_SYNC_ATTEMPT == 200.0
    assert "boom" in daemon._LAST_SYNC_ERROR



def test_get_health_shape_and_stalled_flag(monkeypatch):
    
    
    
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: "")
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"pid": 123, "code_version": 1.5, "started_at": 500.0})
    monkeypatch.setenv("AGENT_CONTEXT_SYNC_STALL_SECS", "900")
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", None)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ERROR", None)

    
    import time as _t
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", _t.time())
    h = daemon.get_health()
    
    
    
    expected = {"pid", "code_version", "started_at", "last_successful_sync",
                "last_sync_attempt", "last_sync_error", "sync_stall_secs",
                "seconds_since_sync", "stalled", "working_tree_wedged",
                
                
                "seconds_since_attempt", "loop_alive", "verdict",
                
                
                "log_path",
                
                
                "log_hint",
                
                
                "server_commit",
                
                
                
                
                
                "last_sync_error_at",
                "code_current",
                
                
                
                
                "code_defer_reason",
                
                
                
                "behind",
                
                
                
                "starts_last_hour",
                
                
                "cycle_in_flight_secs", "suspended_secs",
                
                
                "retry_delay_secs"}
    assert set(h.keys()) == expected
    assert h["pid"] == 123
    assert h["sync_stall_secs"] == 900.0
    assert h["stalled"] is False

    
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", _t.time() - 10000)
    h2 = daemon.get_health()
    assert h2["stalled"] is True
    assert h2["seconds_since_sync"] > 900


def test_get_health_no_sync_yet(monkeypatch):
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: "")
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"pid": 1, "started_at": 10.0})
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", None)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", None)
    h = daemon.get_health()
    assert h["last_successful_sync"] is None
    assert h["seconds_since_sync"] is None
    
    
    
    assert h["stalled"] is False
    assert h["verdict"] == "loop-dead"


def test_get_health_never_synced(monkeypatch):
    '`seconds_since_sync` is None both for a daemon that just started and for one\n    that has never succeeded, so every `since > threshold` test was False and the\n    verdict fell through to "healthy" — reported with last_successful_sync null,\n    stalled false, and 51 commits behind all four remotes, for 45 minutes, while a\n    merge conflict aborted every cycle. Nothing in the suite covered it.'
    import time as _t
    now = _t.time()
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: "")
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"pid": 1, "started_at": now - 5000})
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", None)
    
    
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", now - 30)
    h = daemon.get_health()
    assert h["last_successful_sync"] is None
    assert h["seconds_since_sync"] is None
    assert h["loop_alive"] is True
    assert h["stalled"] is True
    assert h["verdict"] == "integration-failing"



def _quiet(monkeypatch, *, started_at, attempt, cycle_start=None, since_attempt_slept=0.0,
           info_extra=None, retry_delay=None):
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: "")
    info = {"pid": 1, "started_at": started_at, **(info_extra or {})}
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: info)
    monkeypatch.setenv("AGENT_CONTEXT_SYNC_STALL_SECS", "900")
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", attempt)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", attempt)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ERROR", None)
    monkeypatch.setattr(daemon, "_CYCLE_STARTED_AT", cycle_start)
    monkeypatch.setattr(daemon, "_NEXT_CYCLE_DELAY", retry_delay)
    monkeypatch.setitem(daemon._SLEEP_STATE, "since_attempt", since_attempt_slept)


def test_a_stale_cycle_start_on_disk_is_ignored_once_the_process_has_a_heartbeat(monkeypatch):
    'test a stale cycle start on disk is ignored once the process has a heartbeat.'
    import time as _t
    now = _t.time()
    _quiet(monkeypatch, started_at=now - 86400, attempt=now - 30, cycle_start=None,
           info_extra={"cycle_started_at": now - 20})
    h = daemon.get_health()
    assert h["cycle_in_flight_secs"] is None


def test_a_reader_with_no_heartbeat_still_reads_the_cycle_start_from_disk(monkeypatch):
    import time as _t
    now = _t.time()
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: "")
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"pid": 1, "started_at": now - 86400,
                                 "last_sync_attempt": now - 1105, "cycle_started_at": now - 40})
    monkeypatch.setenv("AGENT_CONTEXT_SYNC_STALL_SECS", "900")
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", None)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", None)
    monkeypatch.setattr(daemon, "_CYCLE_STARTED_AT", None)
    monkeypatch.setattr(daemon, "_NEXT_CYCLE_DELAY", None)
    monkeypatch.setitem(daemon._SLEEP_STATE, "since_attempt", 0.0)
    h = daemon.get_health()
    assert 30 < h["cycle_in_flight_secs"] < 50
    assert h["loop_alive"] is True


def test_a_scheduled_backoff_longer_than_the_threshold_is_not_a_dead_loop(monkeypatch):
    'test a scheduled backoff longer than the threshold is not a dead loop.'
    import time as _t
    now = _t.time()
    _quiet(monkeypatch, started_at=now - 86400, attempt=now - 2000, retry_delay=2400.0)
    h = daemon.get_health()
    assert h["loop_alive"] is True
    assert h["verdict"] != "loop-dead"
    assert h["retry_delay_secs"] == 2400.0


def test_quiet_past_the_announced_backoff_is_dead(monkeypatch):
    import time as _t
    now = _t.time()
    _quiet(monkeypatch, started_at=now - 86400, attempt=now - 2000, retry_delay=600.0)
    assert daemon.get_health()["verdict"] == "loop-dead"


def test_a_cycle_still_running_keeps_the_loop_alive(monkeypatch):
    'test a cycle still running keeps the loop alive.'
    import time as _t
    now = _t.time()
    _quiet(monkeypatch, started_at=now - 86400, attempt=now - 1105, cycle_start=now - 380)
    h = daemon.get_health()
    assert h["loop_alive"] is True
    assert h["verdict"] != "loop-dead"
    assert 370 < h["cycle_in_flight_secs"] < 390


def test_a_cycle_running_past_the_threshold_is_not_alive(monkeypatch):
    'test a cycle running past the threshold is not alive.'
    import time as _t
    now = _t.time()
    _quiet(monkeypatch, started_at=now - 86400, attempt=now - 2000, cycle_start=now - 1300)
    h = daemon.get_health()
    assert h["loop_alive"] is False
    assert h["verdict"] == "loop-dead"


def test_suspended_time_since_the_attempt_is_discounted(monkeypatch):
    'test suspended time since the attempt is discounted.'
    import time as _t
    now = _t.time()
    _quiet(monkeypatch, started_at=now - 86400, attempt=now - 1000,
           since_attempt_slept=900.0)
    h = daemon.get_health()
    assert h["loop_alive"] is True
    assert h["suspended_secs"] == 900.0


def test_loop_dead_always_carries_a_reason(monkeypatch):
    'test loop dead always carries a reason.'
    import time as _t
    now = _t.time()
    _quiet(monkeypatch, started_at=now - 86400, attempt=now - 2000, cycle_start=now - 1300)
    h = daemon.get_health()
    assert h["verdict"] == "loop-dead"
    assert h["last_sync_error"] is not None
    assert "no completed sync cycle for" in h["last_sync_error"]
    assert "current cycle started" in h["last_sync_error"]


def test_recording_an_attempt_clears_the_cycle_and_the_attempt_sleep_clock(monkeypatch):
    monkeypatch.setattr(daemon, "_update_daemon_info", lambda **k: None)
    monkeypatch.setitem(daemon._SLEEP_STATE, "since_attempt", 0.0)
    daemon.record_cycle_start(now=50.0)
    assert daemon._CYCLE_STARTED_AT == 50.0
    daemon._SLEEP_STATE["since_attempt"] = 120.0
    daemon.record_sync_failure(RuntimeError("x"), now=60.0)
    assert daemon._CYCLE_STARTED_AT is None
    assert daemon.slept_since_attempt_secs() == 0.0
    daemon.record_cycle_start(now=70.0)
    daemon._SLEEP_STATE["since_attempt"] = 30.0
    daemon.record_sync_success(now=80.0)
    assert daemon._CYCLE_STARTED_AT is None
    assert daemon.slept_since_attempt_secs() == 0.0


def test_a_sleep_gap_feeds_both_accumulators():
    daemon._SLEEP_STATE.update({"wall": 0.0, "mono": 0.0, "slept": 0.0, "since_attempt": 0.0})
    daemon.note_sleep_gap(wall=1000.0, mono=100.0)        
    daemon.note_sleep_gap(wall=1700.0, mono=110.0)        
    assert daemon.slept_secs() == 690.0
    assert daemon.slept_since_attempt_secs() == 690.0
    daemon.reset_attempt_sleep_accounting()
    assert daemon.slept_secs() == 690.0                   
    assert daemon.slept_since_attempt_secs() == 0.0



def test_stall_secs_env_override(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT_SYNC_STALL_SECS", raising=False)
    assert daemon._sync_stall_secs() == 900.0
    monkeypatch.setenv("AGENT_CONTEXT_SYNC_STALL_SECS", "120")
    assert daemon._sync_stall_secs() == 120.0
    monkeypatch.setenv("AGENT_CONTEXT_SYNC_STALL_SECS", "garbage")
    assert daemon._sync_stall_secs() == 900.0   


def test_watchdog_restart_env_toggle(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT_WATCHDOG_RESTART", raising=False)
    assert daemon._watchdog_restart_enabled() is False
    monkeypatch.setenv("AGENT_CONTEXT_WATCHDOG_RESTART", "1")
    assert daemon._watchdog_restart_enabled() is True
    monkeypatch.setenv("AGENT_CONTEXT_WATCHDOG_RESTART", "off")
    assert daemon._watchdog_restart_enabled() is False


def test_get_health_reports_wedge_as_stalled(monkeypatch):
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: "stale index.lock (> 10 min old)")
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"pid": 1, "started_at": 10.0})
    import time as _t
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", _t.time())
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", None)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ERROR", None)
    h = daemon.get_health()
    assert h["stalled"] is True
    assert h["working_tree_wedged"].startswith("stale index.lock")
















def test_note_sleep_gap_seeds_then_measures_only_the_wall_excess():
    daemon._SLEEP_STATE.update({"wall": 0.0, "mono": 0.0, "slept": 0.0})
    
    assert daemon.note_sleep_gap(wall=1000.0, mono=500.0) == 0.0
    
    assert daemon.note_sleep_gap(wall=1010.0, mono=510.0) == 0.0
    
    assert daemon.note_sleep_gap(wall=1610.0, mono=515.0) == pytest.approx(595.0)
    assert daemon.slept_secs() == pytest.approx(595.0)
    
    assert daemon.note_sleep_gap(wall=1630.0, mono=530.0) == 0.0
    assert daemon.slept_secs() == pytest.approx(595.0)
    daemon.reset_sleep_accounting()
    assert daemon.slept_secs() == 0.0


def test_a_sleep_explained_gap_is_healthy_and_says_so():
    'test a sleep explained gap is healthy and says so.'
    notify, restart, reason = daemon.watchdog_decision(
        now=12119.0, last_success=10000.0, threshold=900.0,
        already_notified=False, restart_enabled=True, last_attempt=10000.0,
        slept=2119.0)
    assert notify is False and restart is False
    assert reason.startswith("healthy")
    assert "suspended" in reason and "asleep, not stalled" in reason


def test_time_awake_and_not_syncing_is_still_a_stall():
    'The discount must not become a way to sleep through a real fault: only the\n    suspended portion is forgiven, and what is left is judged normally.'
    notify, _restart, reason = daemon.watchdog_decision(
        now=13000.0, last_success=10000.0, threshold=900.0,
        already_notified=False, restart_enabled=False, last_attempt=12999.0,
        slept=1000.0)                      
    assert notify is True
    assert reason.startswith("stalled")
    assert "discounting 1000s suspended" in reason


def test_a_frozen_process_is_not_a_wedged_one():
    '`wedged` triggers a re-exec, so the suspend discount has to reach it too --\n    a process that was suspended was not failing to attempt.'
    _, restart, _ = daemon.watchdog_decision(
        now=12119.0, last_success=10000.0, threshold=900.0,
        already_notified=False, restart_enabled=True, last_attempt=10000.0,
        slept=2119.0)
    assert restart is False
    
    _, restart_awake, _ = daemon.watchdog_decision(
        now=12119.0, last_success=10000.0, threshold=900.0,
        already_notified=False, restart_enabled=True, last_attempt=10000.0,
        slept=0.0)
    assert restart_awake is True
