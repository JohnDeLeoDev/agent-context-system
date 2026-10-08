'One deploy gate at a time per code fingerprint, shared by the daemon and every bridge.\n\n`gate_lock.single_flight` is the shared door: one gate.lock flock, an owner record while it runs, and\na result record per fingerprint that every other party reuses instead of running the suite again.'
from __future__ import annotations

import json
import logging
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agent_context import gate_lock

FP = "a" * 40
OTHER_FP = "b" * 40
PASS = (True, "gate passed")


class Runner:
    'A fake `run_gate`: counts calls, can pause, reports what it was told to.'

    def __init__(self, result: tuple[bool, str] = PASS, pause: threading.Event | None = None,
                 entered: threading.Event | None = None) -> None:
        self.result = result
        self.pause = pause
        self.entered = entered
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> tuple[bool, str]:
        with self._lock:
            self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.pause is not None:
            assert self.pause.wait(30), "test bug: the fake gate was never released"
        return self.result


def flight(state: Path, run: Runner, *, fp: str = FP, role: str = "daemon",
           current: str | None = None, timeout: float = 30.0, retry_secs: float = 3600.0,
           wait_grace: float = 5.0) -> gate_lock.Outcome:
    return gate_lock.single_flight(
        state, fp, role, run,
        current_fingerprint=lambda: fp if current is None else current,
        timeout=timeout, retry_secs=retry_secs, wait_grace=wait_grace, poll=0.02)


def result_path(state: Path) -> Path:
    return state / "gate-result.json"


def owner_path(state: Path) -> Path:
    return state / "gate-owner.json"




def test_the_gate_runs_once_and_records_a_pass(tmp_path: Path) -> None:
    run = Runner()
    out = flight(tmp_path, run)
    assert (out.ok, out.source, run.calls) == (True, "ran", 1)
    rec = json.loads(result_path(tmp_path).read_text())
    assert rec["fingerprint"] == FP and rec["ok"] is True
    assert rec["owner_role"] == "daemon" and rec["owner_pid"] == os.getpid()
    assert rec["duration"] >= 0 and rec["finished_at"] <= time.time() + 1


def test_a_recorded_pass_for_the_same_fingerprint_skips_the_suite(tmp_path: Path) -> None:
    flight(tmp_path, Runner())
    run = Runner()
    out = flight(tmp_path, run, role="bridge")
    assert (out.ok, out.source, run.calls) == (True, "recorded", 0)
    assert out.owner_role == "daemon"


def test_a_pass_for_another_fingerprint_never_skips_the_suite(tmp_path: Path) -> None:
    flight(tmp_path, Runner(), fp=OTHER_FP)
    run = Runner()
    out = flight(tmp_path, run)
    assert (out.ok, run.calls) == (True, 1)


def test_a_recorded_real_failure_holds_the_fingerprint(tmp_path: Path) -> None:
    flight(tmp_path, Runner((False, "failed: 4 failed")))
    run = Runner()
    out = flight(tmp_path, run, role="bridge")
    assert (out.ok, out.source, run.calls) == (False, "recorded", 0)
    assert "4 failed" in out.detail


def test_a_failure_older_than_the_hold_runs_again(tmp_path: Path) -> None:
    flight(tmp_path, Runner((False, "failed: x")))
    run = Runner()
    out = flight(tmp_path, run, retry_secs=0.0)
    assert (out.ok, out.source, run.calls) == (True, "ran", 1)


def test_the_hold_does_not_cover_a_newer_fingerprint(tmp_path: Path) -> None:
    flight(tmp_path, Runner((False, "failed: x")))
    run = Runner()
    assert flight(tmp_path, run, fp=OTHER_FP).ok is True
    assert run.calls == 1




def test_an_interrupted_gate_is_not_recorded(tmp_path: Path) -> None:
    out = flight(tmp_path, Runner((False, "interrupted: SIGTERM; systemd stop")))
    assert (out.ok, out.source) == (None, "ran")
    assert not result_path(tmp_path).exists()
    run = Runner()
    assert flight(tmp_path, run).ok is True and run.calls == 1


def test_an_interrupted_gate_does_not_clear_an_earlier_pass_for_another_fingerprint(
        tmp_path: Path) -> None:
    flight(tmp_path, Runner(), fp=OTHER_FP)
    flight(tmp_path, Runner((False, "interrupted: SIGTERM")))
    assert json.loads(result_path(tmp_path).read_text())["fingerprint"] == OTHER_FP




def test_a_change_between_the_gate_and_the_adoption_is_not_adopted(tmp_path: Path) -> None:
    out = flight(tmp_path, Runner(), current=OTHER_FP)
    assert (out.ok, out.source) == (None, "stale")
    
    run = Runner()
    assert flight(tmp_path, run).source == "recorded" and run.calls == 0


def test_a_recorded_pass_is_rechecked_against_the_code_at_adoption(tmp_path: Path) -> None:
    flight(tmp_path, Runner())
    out = flight(tmp_path, Runner(), current=OTHER_FP)
    assert (out.ok, out.source) == (None, "stale")


def test_a_record_for_other_code_is_not_a_record_for_this_code(tmp_path: Path) -> None:
    flight(tmp_path, Runner(), fp=OTHER_FP)
    assert gate_lock.read_result(tmp_path, FP) is None
    assert gate_lock.read_result(tmp_path, OTHER_FP) is not None


def test_a_result_file_writable_by_others_is_ignored(tmp_path: Path) -> None:
    flight(tmp_path, Runner())
    result_path(tmp_path).chmod(0o666)
    run = Runner()
    out = flight(tmp_path, run)
    assert (out.source, run.calls) == ("ran", 1)


def test_a_result_file_writable_by_the_group_is_ignored(tmp_path: Path) -> None:
    flight(tmp_path, Runner())
    result_path(tmp_path).chmod(0o620)
    assert gate_lock.read_result(tmp_path, FP) is None


def test_a_result_file_owned_by_another_user_is_ignored(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flight(tmp_path, Runner())
    uid = os.getuid()
    monkeypatch.setattr(gate_lock.os, "getuid", lambda: uid + 1)
    assert gate_lock.read_result(tmp_path, FP) is None


def test_records_are_written_owner_only(tmp_path: Path) -> None:
    seen: list[int] = []

    def run() -> tuple[bool, str]:
        seen.append(stat.S_IMODE(owner_path(tmp_path).stat().st_mode))
        return PASS

    gate_lock.single_flight(tmp_path, FP, "daemon", run, current_fingerprint=lambda: FP,
                            timeout=30.0, retry_secs=3600.0)
    assert seen == [0o600]
    assert stat.S_IMODE(result_path(tmp_path).stat().st_mode) == 0o600


@pytest.mark.parametrize("junk", ["", "not json", "[]", "{}", '{"fingerprint": 5}',
                                  '{"fingerprint": "%s", "ok": "yes"}' % FP])
def test_a_corrupt_result_is_no_record_and_never_a_skip(tmp_path: Path, junk: str) -> None:
    result_path(tmp_path).write_text(junk)
    result_path(tmp_path).chmod(0o600)
    run = Runner()
    out = flight(tmp_path, run)
    assert (out.ok, out.source, run.calls) == (True, "ran", 1)


def test_a_corrupt_owner_record_does_not_break_the_gate(tmp_path: Path) -> None:
    owner_path(tmp_path).write_text("{{{")
    run = Runner()
    assert flight(tmp_path, run).ok is True and run.calls == 1


def test_no_temp_files_are_left_behind(tmp_path: Path) -> None:
    state = tmp_path / "gate-state"
    flight(state, Runner())
    assert sorted(p.name for p in state.iterdir()) == ["gate-result.json", "gate.lock"]


def test_the_owner_record_names_the_running_gate_and_is_removed_after(tmp_path: Path) -> None:
    seen: list[dict] = []

    def run() -> tuple[bool, str]:
        seen.append(json.loads(owner_path(tmp_path).read_text()))
        return PASS

    gate_lock.single_flight(tmp_path, FP, "bridge", run, current_fingerprint=lambda: FP,
                            timeout=30.0, retry_secs=3600.0)
    assert seen[0]["pid"] == os.getpid() and seen[0]["role"] == "bridge"
    assert seen[0]["fingerprint"] == FP and seen[0]["started_at"] > 0
    assert not owner_path(tmp_path).exists()


def test_the_owner_record_is_removed_when_the_gate_raises(tmp_path: Path) -> None:
    def boom() -> tuple[bool, str]:
        raise RuntimeError("gate could not run")

    with pytest.raises(RuntimeError):
        gate_lock.single_flight(tmp_path, FP, "daemon", boom, current_fingerprint=lambda: FP,
                                timeout=30.0, retry_secs=3600.0)
    assert not owner_path(tmp_path).exists()
    run = Runner()                         
    assert flight(tmp_path, run).ok is True




def test_a_daemon_and_three_bridges_starting_together_run_one_suite(tmp_path: Path) -> None:
    pause, entered = threading.Event(), threading.Event()
    run = Runner(pause=pause, entered=entered)
    outs: dict[str, gate_lock.Outcome] = {}

    def go(name: str, role: str) -> None:
        outs[name] = flight(tmp_path, run, role=role)

    threads = [threading.Thread(target=go, args=("d", "daemon"))]
    threads[0].start()
    assert entered.wait(10)
    threads += [threading.Thread(target=go, args=(f"b{i}", "bridge")) for i in range(3)]
    for t in threads[1:]:
        t.start()
    time.sleep(0.3)                        
    assert run.calls == 1
    pause.set()
    for t in threads:
        t.join(15)
    assert run.calls == 1
    assert {o.ok for o in outs.values()} == {True}
    assert outs["d"].source == "ran"
    assert {outs[f"b{i}"].source for i in range(3)} <= {"waited", "recorded"}
    assert all(outs[f"b{i}"].owner_role == "daemon" for i in range(3))
    assert all(outs[f"b{i}"].waited_secs >= 0.2 for i in range(3))


def test_a_waiter_behind_a_gate_for_older_code_runs_its_own_gate(tmp_path: Path) -> None:
    pause, entered = threading.Event(), threading.Event()
    slow = Runner(pause=pause, entered=entered)
    t = threading.Thread(target=lambda: flight(tmp_path, slow, fp=OTHER_FP))
    t.start()
    assert entered.wait(10)
    mine = Runner()
    done: list[gate_lock.Outcome] = []
    w = threading.Thread(target=lambda: done.append(flight(tmp_path, mine, role="bridge")))
    w.start()
    time.sleep(0.2)
    assert mine.calls == 0                 
    pause.set()
    t.join(15)
    w.join(15)
    assert mine.calls == 1 and done[0].ok is True and done[0].source == "ran"


def test_a_waiter_gives_up_after_the_gate_timeout_and_never_runs_a_second_suite(
        tmp_path: Path) -> None:
    pause, entered = threading.Event(), threading.Event()
    holder = Runner(pause=pause, entered=entered)
    t = threading.Thread(target=lambda: flight(tmp_path, holder, fp=OTHER_FP))
    t.start()
    assert entered.wait(10)
    mine = Runner()
    t0 = time.monotonic()
    out = flight(tmp_path, mine, role="bridge", timeout=0.2, wait_grace=0.2)
    assert time.monotonic() - t0 < 5
    assert (out.ok, out.source, mine.calls) == (None, "timeout", 0)
    pause.set()
    t.join(15)


def test_a_dead_owner_leaves_no_result_and_the_next_party_runs(tmp_path: Path) -> None:
    child_code = textwrap.dedent(f"""
        import fcntl, json, os, sys, time
        d = {str(tmp_path)!r}
        fh = open(os.path.join(d, "gate.lock"), "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        with open(os.path.join(d, "gate-owner.json"), "w") as o:
            json.dump({{"pid": os.getpid(), "role": "bridge", "fingerprint": {FP!r},
                       "started_at": time.time()}}, o)
        print("locked", flush=True)
        time.sleep(60)
    """)
    child = subprocess.Popen([sys.executable, "-c", child_code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        run = Runner()
        outs: list[gate_lock.Outcome] = []
        w = threading.Thread(target=lambda: outs.append(flight(tmp_path, run)))
        w.start()
        time.sleep(0.3)
        assert run.calls == 0              
        os.kill(child.pid, signal.SIGKILL)
        w.join(15)
        assert not w.is_alive()
        assert (outs[0].ok, outs[0].source, run.calls) == (True, "ran", 1)
    finally:
        child.kill()
        child.wait()


def test_an_unwritable_state_dir_runs_the_gate_directly(tmp_path: Path) -> None:
    state = tmp_path / "ro"
    state.mkdir()
    state.chmod(0o500)
    try:
        run = Runner()
        out = flight(state, run)
        assert (out.ok, run.calls) == (True, 1)
        assert out.source == "unshared"
    finally:
        state.chmod(0o700)


def test_a_missing_state_dir_is_created(tmp_path: Path) -> None:
    state = tmp_path / "new" / "state"
    assert flight(state, Runner()).ok is True
    assert result_path(state).exists()




def test_a_bounce_is_claimed_once_per_fingerprint(tmp_path: Path) -> None:
    flight(tmp_path, Runner())
    assert gate_lock.claim_bounce(tmp_path, FP) is True
    assert gate_lock.claim_bounce(tmp_path, FP) is False


def test_a_bounce_needs_a_recorded_pass_for_that_fingerprint(tmp_path: Path) -> None:
    assert gate_lock.claim_bounce(tmp_path, FP) is False          
    flight(tmp_path, Runner((False, "failed: x")))
    assert gate_lock.claim_bounce(tmp_path, FP) is False          
    flight(tmp_path, Runner(), fp=OTHER_FP)
    assert gate_lock.claim_bounce(tmp_path, FP) is False          
    assert gate_lock.claim_bounce(tmp_path, OTHER_FP) is True


def test_racing_bounce_claims_have_one_winner(tmp_path: Path) -> None:
    flight(tmp_path, Runner())
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def go() -> None:
        barrier.wait()
        wins.append(gate_lock.claim_bounce(tmp_path, FP))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert wins.count(True) == 1 and len(wins) == 8


def test_a_new_gate_result_resets_the_bounce_claim(tmp_path: Path) -> None:
    flight(tmp_path, Runner())
    assert gate_lock.claim_bounce(tmp_path, FP)
    flight(tmp_path, Runner(), fp=OTHER_FP)
    assert gate_lock.claim_bounce(tmp_path, OTHER_FP)


def test_a_claim_on_a_tampered_result_file_is_refused(tmp_path: Path) -> None:
    flight(tmp_path, Runner())
    result_path(tmp_path).chmod(0o666)
    assert gate_lock.claim_bounce(tmp_path, FP) is False




@pytest.mark.parametrize("self_deploys,age,grace,expected", [
    (True, 10.0, 300.0, True),       
    (True, 299.0, 300.0, True),
    (True, 301.0, 300.0, False),     
    (False, 10.0, 300.0, False),     
    (False, 1e6, 300.0, False),
])
def test_bridge_defers_to_a_self_deploying_daemon_for_one_sync_cycle(
        self_deploys: bool, age: float, grace: float, expected: bool) -> None:
    assert gate_lock.bridge_should_defer(
        daemon_self_deploys=self_deploys, code_age_secs=age, grace_secs=grace) is expected


@pytest.mark.parametrize("self_deploys,expected", [(True, False), (False, True)])
def test_a_bridge_restarts_a_daemon_only_when_nothing_else_will(
        self_deploys: bool, expected: bool) -> None:
    assert gate_lock.bridge_may_restart(daemon_self_deploys=self_deploys) is expected




def test_log_lines_carry_role_pid_duration_and_no_path(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="agent-context"):
        flight(tmp_path, Runner(), role="daemon")
        flight(tmp_path, Runner(), role="bridge")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "gate_start" in text and "gate_end" in text and "gate_adopt" in text
    assert f"role=daemon pid={os.getpid()}" in text
    assert "ok=True" in text and "duration=" in text
    assert FP[:8] in text and FP not in text          
    assert str(tmp_path) not in text
    adopt = next(r.getMessage() for r in caplog.records if "gate_adopt" in r.getMessage())
    assert "role=bridge" in adopt and "from=daemon" in adopt


def test_a_waiter_logs_who_it_waits_behind_and_for_how_long(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    pause, entered = threading.Event(), threading.Event()
    run = Runner(pause=pause, entered=entered)
    t = threading.Thread(target=lambda: flight(tmp_path, run))
    with caplog.at_level(logging.INFO, logger="agent-context"):
        t.start()
        assert entered.wait(10)
        w = threading.Thread(target=lambda: flight(tmp_path, Runner(), role="bridge"))
        w.start()
        time.sleep(0.3)
        pause.set()
        t.join(15)
        w.join(15)
    lines = [r.getMessage() for r in caplog.records if "gate_wait" in r.getMessage()]
    assert lines and "behind=daemon:" in lines[0] and "role=bridge" in lines[0]
    ends = [r.getMessage() for r in caplog.records if "gate_end" in r.getMessage()]
    assert any("waited=" in m for m in ends + lines)
