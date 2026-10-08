'G1 the default budget is 1500 s everywhere it is spelled, and the env override still wins.\nG2 a party waiting behind a gate waits for the whole-suite rerun too, so it never gives up\n   between the first run and the rerun.\nG3 a timeout says how far the run got and how long a full run needs.\nG4 the classes and prefixes of a gate result do not change.'
from __future__ import annotations

import inspect
import sys
import textwrap
from pathlib import Path

import pytest

from agent_context import daemon, gate_lock


def _new(name):
    'A daemon helper this change adds, reached by name so the file typechecks before it exists.'
    return getattr(daemon, name)


def test_the_default_budget_is_1500_seconds():
    assert daemon._GATE_TIMEOUT_SECS == 1500


def test_the_self_redeploy_default_uses_the_same_constant():
    default = inspect.signature(daemon.maybe_self_redeploy).parameters["gate_timeout"].default
    assert default == daemon._GATE_TIMEOUT_SECS == 1500


def test_the_env_override_still_wins_and_keeps_its_floor(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT_GATE_TIMEOUT", raising=False)
    assert daemon._gate_timeout_secs(daemon._GATE_TIMEOUT_SECS) == 1500
    monkeypatch.setenv("AGENT_CONTEXT_GATE_TIMEOUT", "900")
    assert daemon._gate_timeout_secs(daemon._GATE_TIMEOUT_SECS) == 900
    monkeypatch.setenv("AGENT_CONTEXT_GATE_TIMEOUT", "10")
    assert daemon._gate_timeout_secs(daemon._GATE_TIMEOUT_SECS) == 30
    monkeypatch.setenv("AGENT_CONTEXT_GATE_TIMEOUT", "soon")
    assert daemon._gate_timeout_secs(daemon._GATE_TIMEOUT_SECS) == 1500


def test_the_wall_budget_covers_the_rerun_when_the_rerun_is_on(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT_GATE_RERUN", raising=False)
    assert daemon._gate_rerun_enabled()
    assert _new("_gate_wall_budget")(600) == 1200
    assert _new("_gate_wall_budget")(240) == 480


def test_the_wall_budget_is_one_run_when_the_rerun_is_off(monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_GATE_RERUN", "0")
    assert not daemon._gate_rerun_enabled()
    assert _new("_gate_wall_budget")(600) == 600


@pytest.mark.parametrize("rerun,expected", [("1", 2.0), ("0", 1.0)])
def test_waiters_are_given_the_wall_budget(monkeypatch, tmp_path, rerun, expected):
    '_shared_gate hands single_flight the budget of a whole gate, rerun included.'
    monkeypatch.setenv("AGENT_CONTEXT_GATE_RERUN", rerun)
    monkeypatch.setattr(daemon, "_state_dir", lambda: tmp_path)
    seen: dict[str, float] = {}

    def fake_single_flight(state_dir, key, role, run, *, current_fingerprint, timeout,
                           retry_secs, **kw):
        seen["timeout"] = timeout
        return gate_lock.Outcome(True, "gate passed", "ran", 0.0, role, 0, 0.0)

    monkeypatch.setattr(gate_lock, "single_flight", fake_single_flight)
    daemon._shared_gate("key", gate_lock.ROLE_DAEMON, 100)
    assert seen["timeout"] == 100 * expected


def test_a_departing_process_still_waits_one_budget_plus_30(monkeypatch):
    'G4: the exit-time join is not one of the waiters G2 changes.'
    monkeypatch.delenv("AGENT_CONTEXT_GATE_TIMEOUT", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_GATE_RERUN", raising=False)
    assert daemon._bounce_join_secs() == daemon._GATE_TIMEOUT_SECS + 30.0


def test_a_projection_names_progress_and_the_full_duration():
    out = ("." * 72 + " [ 33%]\n" + "." * 72 + " [ 66%]\n")
    text = _new("_timeout_projection")(out, 240)
    assert "66%" in text and "240" in text
    assert "364" in text                                   
    assert "AGENT_CONTEXT_GATE_TIMEOUT" in text


def test_a_projection_is_empty_without_a_usable_percentage():
    assert _new("_timeout_projection")("", 240) == ""
    assert _new("_timeout_projection")("no progress here", 240) == ""
    assert _new("_timeout_projection")("." * 10 + " [  0%]", 240) == ""
    assert _new("_timeout_projection")("." * 10 + " [100%]", 240) == ""


SLOW_TAIL_SUITE = textwrap.dedent('''
    import time

    import pytest

    @pytest.mark.parametrize("n", range(400))
    def test_quick(n):
        pass

    def test_slow_tail():
        time.sleep(60)
''')


def test_a_real_timeout_keeps_its_class_and_carries_the_projection(tmp_path, monkeypatch):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_tail.py").write_text(SLOW_TAIL_SUITE)
    monkeypatch.setattr(daemon, "_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(daemon, "_save_gate_output", lambda *a, **k: None)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    kind, detail, ids = daemon._run_pytest(sys.executable, suite, suite, 6, 6, 1)
    assert kind == "timeout"
    assert detail.startswith(daemon.GATE_TIMEOUT)                
    assert "gate timed out (>6s)" in detail
    assert daemon.gate_reason_class(detail) == "timeout"
    assert "% in 6 s" in detail and "AGENT_CONTEXT_GATE_TIMEOUT" in detail
    assert Path(suite).is_dir()
