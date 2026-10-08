'`ensure_daemon()` used to run the gate (py_compile + the whole suite) and the bounce\ninline, before returning the URL its caller connects to. Claude Code allows a stdio\nMCP server 30 s to answer `initialize`, so on the first session after any release that\nbudget was blown and the client dropped agent-context for the entire session.\n\nEverything OS-facing is monkeypatched: no real daemon, port, supervisor or pytest run\nis touched, and the flock is taken against a per-test tmp_path.'
import fcntl
import threading
import time

import pytest

from agent_context import daemon


@pytest.fixture(autouse=True)
def _isolated_bounce_state(monkeypatch, tmp_path):
    'Per-test state dir (so the flock is private) and no leaked thread between tests.'
    monkeypatch.setattr(daemon, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(daemon, "_notify", lambda msg: None)
    
    
    monkeypatch.setattr(daemon, "_self_deploy_enabled", lambda: False)
    daemon._bounce_thread = None
    yield
    t = daemon._bounce_thread
    if t is not None and t.is_alive():
        t.join(10)
    daemon._bounce_thread = None




def test_ensure_daemon_does_not_gate_on_the_connect_path(monkeypatch):
    'The regression itself: nothing expensive may run before the URL is returned.'
    inline, backgrounded = [], []
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "run_gate", lambda *a, **k: inline.append("gate") or (True, ""))
    monkeypatch.setattr(daemon, "_bounce_daemon", lambda: inline.append("bounce"))
    monkeypatch.setattr(daemon, "_start_background_bounce", lambda: backgrounded.append(True))

    assert daemon.ensure_daemon() == daemon.URL
    assert inline == []                 
    assert backgrounded == [True]       


def test_ensure_daemon_returns_while_a_slow_gate_is_still_running(monkeypatch):
    'test ensure daemon returns while a slow gate is still running.'
    entered, release = threading.Event(), threading.Event()

    def slow_gate(*a, **k):
        entered.set()
        release.wait(30)
        return False, "still running"

    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "run_gate", slow_gate)

    t0 = time.monotonic()
    assert daemon.ensure_daemon() == daemon.URL
    elapsed = time.monotonic() - t0

    assert entered.wait(10), "the background gate never started"
    assert elapsed < 1.0, f"ensure_daemon blocked for {elapsed:.2f}s on the gate"
    release.set()


def test_no_background_bounce_when_the_daemon_is_already_current(monkeypatch):
    started = []
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: False)
    monkeypatch.setattr(daemon, "_start_background_bounce", lambda: started.append(True))

    assert daemon.ensure_daemon() == daemon.URL
    assert started == []




def test_a_passing_gate_bounces_and_respawns_on_an_unsupervised_host(monkeypatch):
    'Nothing else restarts the daemon on s1/s2 between boots, so the bounce must.'
    seq = []
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "_supervisor", lambda: None)
    monkeypatch.setattr(daemon, "run_gate", lambda *a, **k: (True, "gate passed"))
    monkeypatch.setattr(daemon, "_bounce_daemon", lambda: seq.append("bounce"))
    monkeypatch.setattr(daemon, "ensure_daemon", lambda: seq.append("restart") or daemon.URL)

    daemon._gated_bounce()
    assert seq == ["bounce", "restart"]


def test_a_supervised_bounce_does_not_race_the_supervisor(monkeypatch):
    'test a supervised bounce does not race the supervisor.'
    seq = []
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "_supervisor", lambda: ("launchd", "x.label"))
    monkeypatch.setattr(daemon, "run_gate", lambda *a, **k: (True, "gate passed"))
    monkeypatch.setattr(daemon, "_bounce_daemon", lambda: seq.append("bounce"))
    monkeypatch.setattr(daemon, "ensure_daemon", lambda: seq.append("restart") or daemon.URL)

    daemon._gated_bounce()
    assert seq == ["bounce"], "raced the supervisor with a competing spawn"


def test_a_failing_gate_keeps_the_running_daemon(monkeypatch):
    "The gate's guarantee is unchanged by moving it: never bounce into unverified code."
    bounced, notes = [], []
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "run_gate", lambda *a, **k: (False, "pytest failed: test_x"))
    monkeypatch.setattr(daemon, "_bounce_daemon", lambda: bounced.append(True))
    monkeypatch.setattr(daemon, "_notify", lambda msg: notes.append(msg))

    daemon._gated_bounce()
    assert bounced == []
    assert notes and "gate failed" in notes[0] and "test_x" in notes[0]


def test_gated_bounce_rechecks_after_taking_the_lock(monkeypatch):
    'The winner of the flock may already have bounced while this one queued.'
    calls = []
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: False)
    monkeypatch.setattr(daemon, "run_gate", lambda *a, **k: calls.append(True) or (True, ""))

    daemon._gated_bounce()
    assert calls == []


def test_gated_bounce_gives_up_when_the_daemon_went_away(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "_port_open", lambda: False)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "run_gate", lambda *a, **k: calls.append(True) or (True, ""))

    daemon._gated_bounce()
    assert calls == []




def test_another_process_holding_the_lock_is_not_joined_by_a_second_suite(monkeypatch, tmp_path):
    'After a release EVERY relay sees _should_bounce(); without the flock they would\n    each run the full suite. flock is per open-file-description, so a second handle in\n    this process models the second process faithfully.'
    calls = []
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "run_gate", lambda *a, **k: calls.append(True) or (True, ""))
    monkeypatch.setattr(daemon, "_bounce_daemon", lambda: None)
    monkeypatch.setattr(daemon, "ensure_daemon", lambda: daemon.URL)

    holder = open(tmp_path / "bounce.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        daemon._gated_bounce()
        assert calls == [], "ran the suite while another process held the bounce lock"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    daemon._gated_bounce()          
    assert calls == [True]


def test_only_one_bounce_thread_per_process(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            self.target = target

        def start(self):
            started.append(self)

        def is_alive(self):
            return True

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(daemon.threading, "Thread", FakeThread)
    daemon._start_background_bounce()
    daemon._start_background_bounce()
    assert len(started) == 1




def test_exit_waits_for_a_bounce_a_short_lived_caller_started(monkeypatch):
    'The Synology maintenance cron calls ensure_daemon() and exits at once; a bare\n    daemon thread would be killed mid-gate, orphaning pytest.'
    finished = threading.Event()

    def slow_gate(*a, **k):
        time.sleep(0.3)
        finished.set()
        return False, "done"

    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "run_gate", slow_gate)

    daemon._start_background_bounce()
    assert not finished.is_set()
    daemon._join_bounce_thread()
    assert finished.is_set(), "exit abandoned the gate instead of waiting for it"


def test_join_is_a_noop_when_nothing_is_bouncing():
    daemon._bounce_thread = None
    daemon._join_bounce_thread()        


def test_the_join_is_bounded_by_the_gate_timeout(monkeypatch):
    "A wedged gate (#183's symptom) must delay exit by a bounded amount, never pin it."
    monkeypatch.delenv("AGENT_CONTEXT_GATE_TIMEOUT", raising=False)
    assert daemon._bounce_join_secs() == daemon._GATE_TIMEOUT_SECS + 30.0
    monkeypatch.setenv("AGENT_CONTEXT_GATE_TIMEOUT", "200")
    assert daemon._bounce_join_secs() == 230.0


def test_the_bounce_thread_never_pins_the_interpreter(monkeypatch):
    'daemon=True is the backstop behind the bounded join.'
    made = {}

    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            made.update(name=name, daemon=daemon)

        def start(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(daemon.threading, "Thread", FakeThread)
    daemon._start_background_bounce()
    assert made["daemon"] is True and made["name"] == "agent-context-bounce"
