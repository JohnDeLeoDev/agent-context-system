"The two gate callers, the daemon's self-redeploy and a session bridge's `_gated_bounce`, share one\ngate (gate_lock.single_flight) and one result per code fingerprint.\n\nEverything OS-facing is patched: no real pytest run, no real restart. The state dir is the per-test\ntmp dir the suite's conftest installs, so the lock and the records are real files."
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from agent_context import daemon, gate_lock

FP = "c" * 40
NEWER_FP = "d" * 40
OLD = 3600.0 * 5          
FRESH = 10.0


class World:
    'Recorders and knobs for both callers.'

    def __init__(self) -> None:
        self.gate_calls = 0
        self.exec_calls = 0
        self.bounces = 0
        self.notes: list[str] = []
        self.gate_result: tuple[bool, str] = (True, "gate passed")
        self.pause: threading.Event | None = None
        self.entered = threading.Event()
        self.fingerprint = FP
        self.after_gate: Callable[[], None] | None = None
        self.age = OLD
        self.self_deploys = True

    def fake_gate(self, timeout: int = 120) -> tuple[bool, str]:
        self.gate_calls += 1
        self.entered.set()
        if self.pause is not None:
            assert self.pause.wait(30), "test bug: gate never released"
        if self.after_gate is not None:
            self.after_gate()
        return self.gate_result


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    monkeypatch.setattr(daemon, "_do_exec", lambda: setattr(w, "exec_calls", w.exec_calls + 1))
    monkeypatch.setattr(daemon, "run_gate", w.fake_gate)
    monkeypatch.setattr(daemon, "_notify", lambda msg: w.notes.append(msg))
    monkeypatch.setattr(daemon, "_server_tree_dirty", lambda: False)
    monkeypatch.setattr(daemon, "_ATTEMPTED_VERSIONS", {})
    monkeypatch.setattr(daemon, "_STARTED_VERSION", 100.0)
    monkeypatch.setattr(daemon, "_STARTED_FINGERPRINT", "booted")
    monkeypatch.setattr(daemon, "_code_version", lambda: time.time() - w.age)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: w.fingerprint)
    monkeypatch.setattr(daemon, "_self_deploy_enabled", lambda: w.self_deploys)
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "_bounce_daemon", lambda: setattr(w, "bounces", w.bounces + 1))
    monkeypatch.setattr(daemon, "_supervisor", lambda: ("systemd", "agent-context-daemon.service"))
    monkeypatch.delenv("AGENT_CONTEXT_SELF_DEPLOY", raising=False)
    return w


def _state() -> Path:
    return daemon._state_dir()


def _redeploy() -> None:
    daemon.maybe_self_redeploy()




def test_the_daemon_adopts_a_pass_a_bridge_recorded_without_running_the_suite(
        world: World) -> None:
    gate_lock.single_flight(_state(), FP, "bridge", lambda: (True, "gate passed"),
                            current_fingerprint=lambda: FP, timeout=30, retry_secs=3600)
    _redeploy()
    assert (world.gate_calls, world.exec_calls) == (0, 1)


def test_the_daemon_honors_a_failure_a_bridge_recorded(world: World) -> None:
    gate_lock.single_flight(_state(), FP, "bridge", lambda: (False, "failed: 3 failed"),
                            current_fingerprint=lambda: FP, timeout=30, retry_secs=3600)
    _redeploy()
    assert (world.gate_calls, world.exec_calls, world.notes) == (0, 0, [])
    assert "cooling off" in (daemon.code_defer_reason() or "")


def test_a_real_daemon_failure_is_recorded_for_the_bridges(world: World) -> None:
    world.gate_result = (False, "failed: 4 failed")
    _redeploy()
    assert world.gate_calls == 1 and any("gate failed" in m for m in world.notes)
    rec = gate_lock.read_result(_state(), FP)
    assert rec is not None and rec["ok"] is False and rec["owner_role"] == "daemon"
    assert FP in daemon._ATTEMPTED_VERSIONS


def test_an_interrupted_daemon_gate_is_no_failure(world: World) -> None:
    world.gate_result = (False, "interrupted: SIGTERM; requested by systemd")
    _redeploy()
    assert (world.exec_calls, world.notes) == (0, [])
    assert FP not in daemon._ATTEMPTED_VERSIONS
    assert gate_lock.read_result(_state(), FP) is None
    assert "cooling off" not in (daemon.code_defer_reason() or "")
    world.gate_result = (True, "gate passed")
    _redeploy()                                    
    assert (world.gate_calls, world.exec_calls) == (2, 1)


def test_code_changing_between_the_gate_and_the_exec_is_not_adopted(world: World) -> None:
    world.after_gate = lambda: setattr(world, "fingerprint", NEWER_FP)
    _redeploy()
    assert (world.gate_calls, world.exec_calls) == (1, 0)
    assert world.notes == []                       
    world.after_gate = None
    _redeploy()                                    
    assert (world.gate_calls, world.exec_calls) == (2, 1)
    assert gate_lock.read_result(_state(), NEWER_FP) is not None


def test_the_pass_and_fail_decision_is_unchanged(world: World) -> None:
    _redeploy()
    assert (world.gate_calls, world.exec_calls) == (1, 1)
    world.exec_calls = 0
    world.fingerprint = NEWER_FP
    world.gate_result = (False, "failed: x")
    _redeploy()
    assert (world.gate_calls, world.exec_calls) == (2, 0)


def test_a_recorded_pass_for_older_code_does_not_skip_the_suite(world: World) -> None:
    gate_lock.single_flight(_state(), FP, "bridge", lambda: (True, "gate passed"),
                            current_fingerprint=lambda: FP, timeout=30, retry_secs=3600)
    world.fingerprint = NEWER_FP
    _redeploy()
    assert (world.gate_calls, world.exec_calls) == (1, 1)




def test_a_bridge_leaves_fresh_code_to_a_self_deploying_daemon(world: World) -> None:
    world.age = FRESH
    daemon._gated_bounce()
    assert (world.gate_calls, world.bounces) == (0, 0)


def test_a_bridge_gates_after_the_grace_but_leaves_the_restart_to_the_daemon(
        world: World) -> None:
    daemon._gated_bounce()
    assert (world.gate_calls, world.bounces) == (1, 0)
    assert gate_lock.read_result(_state(), FP) is not None
    _redeploy()                                    
    assert (world.gate_calls, world.exec_calls) == (1, 1)


def test_without_a_self_deploy_loop_the_bridge_gates_at_once_and_restarts_once(
        world: World, caplog: pytest.LogCaptureFixture) -> None:
    world.self_deploys = False
    world.age = FRESH
    with caplog.at_level(logging.INFO, logger="agent-context"):
        daemon._gated_bounce()
        daemon._gated_bounce()                     
    assert (world.gate_calls, world.bounces) == (1, 1)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"requested_by pid={os.getpid()}" in text
    assert str(_state()) not in text


def test_a_bridge_never_restarts_the_daemon_while_the_daemons_own_gate_runs(
        world: World) -> None:
    world.pause = threading.Event()
    d = threading.Thread(target=_redeploy)
    d.start()
    assert world.entered.wait(10)
    threading.Timer(0.4, world.pause.set).start()
    daemon._gated_bounce()                         
    d.join(15)
    assert (world.gate_calls, world.bounces, world.exec_calls) == (1, 0, 1)


def test_a_bridge_after_a_real_failure_runs_no_suite(world: World) -> None:
    world.gate_result = (False, "failed: x")
    _redeploy()
    world.notes.clear()
    daemon._gated_bounce()
    assert (world.gate_calls, world.bounces, world.notes) == (1, 0, [])


def test_a_bridge_reports_its_own_real_failure_once(world: World) -> None:
    world.gate_result = (False, "failed: x")
    daemon._gated_bounce()
    daemon._gated_bounce()
    assert (world.gate_calls, world.bounces) == (1, 0)
    assert len(world.notes) == 1


def test_an_interrupted_bridge_gate_is_not_recorded_or_reported(world: World) -> None:
    world.self_deploys = False
    world.gate_result = (False, "interrupted: SIGTERM")
    daemon._gated_bounce()
    assert (world.bounces, world.notes) == (0, [])
    assert gate_lock.read_result(_state(), FP) is None


def test_a_bridge_does_not_bounce_onto_code_that_changed_after_its_gate(world: World) -> None:
    world.self_deploys = False
    world.after_gate = lambda: setattr(world, "fingerprint", NEWER_FP)
    daemon._gated_bounce()
    assert world.bounces == 0


def test_a_bridge_whose_daemon_went_away_or_is_current_still_does_nothing(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "_should_bounce", lambda: False)
    daemon._gated_bounce()
    monkeypatch.setattr(daemon, "_should_bounce", lambda: True)
    monkeypatch.setattr(daemon, "_port_open", lambda: False)
    daemon._gated_bounce()
    assert (world.gate_calls, world.bounces) == (0, 0)


def test_the_gate_still_runs_when_the_state_dir_cannot_hold_the_lock(
        world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ro = tmp_path / "ro-state"
    ro.mkdir()
    ro.chmod(0o500)
    monkeypatch.setattr(daemon, "_state_dir", lambda: ro)
    try:
        _redeploy()
        assert (world.gate_calls, world.exec_calls) == (1, 1)
    finally:
        ro.chmod(0o700)
