"Reviewer findings on the gate single-flight change, each pinned by a test.\n\n- a failure is never filed, or alerted, against code the gate did not judge\n- a restart claim that did not take expires, so the daemon is not stuck on old code\n- a self-deploying daemon that never adopts a passed build is restarted after a long patience\n- a clock ahead of the tree's mtime does not defer forever\n- unreadable code identity is never gated, adopted or restarted on\n- a record left by a killed owner does not name a live gate"
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_gate_single_flight_daemon import FP, OLD, World, world  

from agent_context import daemon, gate_lock

OTHER = "e" * 40


def _flight(state: Path, run, *, fp: str = FP, current: str = FP):
    return gate_lock.single_flight(state, fp, "daemon", run, current_fingerprint=lambda: current,
                                   timeout=30.0, retry_secs=3600.0, poll=0.02)


def test_a_failure_on_code_that_changed_during_the_gate_is_not_filed_against_the_old_code(
        tmp_path: Path) -> None:
    out = _flight(tmp_path, lambda: (False, "failed: 2 failed"), current=OTHER)
    assert (out.ok, out.source) == (None, "stale")
    assert gate_lock.read_result(tmp_path, FP) is None          


def test_a_failure_on_unchanged_code_is_still_filed(tmp_path: Path) -> None:
    out = _flight(tmp_path, lambda: (False, "failed: 2 failed"))
    assert (out.ok, out.source) == (False, "ran")
    assert gate_lock.read_result(tmp_path, FP) is not None


def test_a_restart_claim_expires_so_a_restart_that_did_not_take_is_retried(
        tmp_path: Path) -> None:
    _flight(tmp_path, lambda: (True, "gate passed"))
    assert gate_lock.claim_bounce(tmp_path, FP) is True
    assert gate_lock.claim_bounce(tmp_path, FP) is False
    path = tmp_path / gate_lock.RESULT_NAME
    rec = json.loads(path.read_text())
    rec["bounced_at"] = time.time() - gate_lock.CLAIM_TTL_SECS - 5
    path.write_text(json.dumps(rec))
    path.chmod(0o600)
    assert gate_lock.claim_bounce(tmp_path, FP) is True
    assert gate_lock.claim_bounce(tmp_path, FP) is False


@pytest.mark.parametrize("age,expected", [(10.0, False), (-1.0, False), (1e6, True)])
def test_a_self_deploying_daemon_that_never_adopts_gets_restarted_after_the_patience(
        age: float, expected: bool) -> None:
    assert gate_lock.bridge_may_restart(daemon_self_deploys=True, code_age_secs=age) is expected
    assert gate_lock.bridge_may_restart(daemon_self_deploys=False, code_age_secs=age) is True


def test_the_patience_is_the_boundary() -> None:
    p = gate_lock.DAEMON_PATIENCE_SECS
    assert p > 3600.0 * 5                                       
    assert gate_lock.bridge_may_restart(daemon_self_deploys=True, code_age_secs=p - 1) is False
    assert gate_lock.bridge_may_restart(daemon_self_deploys=True, code_age_secs=p) is True


def test_a_tree_stamped_in_the_future_does_not_defer_the_bridge(tmp_path: Path) -> None:
    assert gate_lock.bridge_should_defer(daemon_self_deploys=True, code_age_secs=-50.0) is False


def test_a_dead_owners_record_names_no_one(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    rec = {"pid": child.pid, "role": "bridge", "fingerprint": FP, "started_at": time.time()}
    path = tmp_path / gate_lock.OWNER_NAME
    path.write_text(json.dumps(rec))
    path.chmod(0o600)
    assert gate_lock._owner_label(tmp_path) == "unknown:0"
    rec["pid"] = os.getpid()
    path.write_text(json.dumps(rec))
    assert gate_lock._owner_label(tmp_path) == f"bridge:{os.getpid()}"


def test_a_bridge_restarts_a_daemon_whose_self_deploy_never_adopted(world: World) -> None:  
    world.age = 2000.0 + gate_lock.DAEMON_PATIENCE_SECS
    daemon._gated_bounce()
    assert (world.gate_calls, world.bounces) == (1, 1)
    daemon._gated_bounce()                          
    assert (world.gate_calls, world.bounces) == (1, 1)


def test_a_bridge_inside_the_patience_still_leaves_the_restart_to_the_daemon(
        world: World) -> None:  
    world.age = OLD
    daemon._gated_bounce()
    assert (world.gate_calls, world.bounces) == (1, 0)


def test_unreadable_code_identity_is_never_gated_or_restarted_on(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:  
    world.self_deploys = False
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: None)
    monkeypatch.setattr(daemon, "_code_version", lambda: None)
    daemon._gated_bounce()
    assert (world.gate_calls, world.bounces) == (0, 0)
    assert gate_lock.read_result(daemon._state_dir(), "None") is None


def test_a_pass_is_not_adopted_when_the_code_identity_becomes_unreadable(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:  
    def lose_identity() -> None:
        monkeypatch.setattr(daemon, "_code_fingerprint", lambda: None)
        monkeypatch.setattr(daemon, "_code_version", lambda: None)

    world.after_gate = lose_identity
    daemon.maybe_self_redeploy()
    assert (world.gate_calls, world.exec_calls) == (1, 0)
