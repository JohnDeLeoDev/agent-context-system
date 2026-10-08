"* the word KeyboardInterrupt in a failing test's output is not an interrupted run;\n* only pytest exit code 1 (tests failed) earns the whole-suite rerun: 3, 4, 5 and a collection\n  error mean the run itself is broken;\n* a gate-output write that fails leaves the file that was already there."
import subprocess

import pytest

from agent_context import daemon

FAILED_MENTIONING_IT = (
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_a.py::test_x - KeyboardInterrupt handled badly\n"
    "1 failed, 9 passed\n")


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT_GATE_RERUN", raising=False)
    state = {"plan": [], "launched": 0}

    class Proc:
        def __init__(self, *a, **k):
            state["launched"] += 1
            self.pid = 5000 + state["launched"]
            self.returncode = None
            self.spec = state["plan"].pop(0)

        def communicate(self, timeout=None):
            self.returncode, out, err = self.spec
            return out, err

        def kill(self):
            pass

    monkeypatch.setattr(daemon.subprocess, "Popen", Proc)
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(daemon, "_source_files", list)
    return state


def test_a_failing_test_that_prints_keyboardinterrupt_is_failed_and_gets_the_rerun(world):
    world["plan"] = [(1, FAILED_MENTIONING_IT, ""), (1, FAILED_MENTIONING_IT, "")]
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is False and daemon.gate_reason_class(detail) == "failed"
    assert world["launched"] == 2


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_a_broken_run_is_failed_and_is_not_rerun(world, code):
    world["plan"] = [(code, "ERROR collecting tests/test_a.py\n", "")]
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is False and daemon.gate_reason_class(detail) == "failed"
    assert world["launched"] == 1


def test_a_failed_output_write_keeps_the_previous_file(monkeypatch):
    daemon._save_gate_output("first run output", "", "attempt 1")
    state = daemon._state_dir()
    last = state / "gate-last-run.log"
    assert "first run output" in last.read_text()

    def refuse(*a, **k):
        raise OSError("no space left")

    monkeypatch.setattr(daemon.os, "open", refuse)
    assert daemon._save_gate_output("second run output", "", "attempt 2") == ""
    monkeypatch.undo()
    assert "first run output" in last.read_text()             
    assert not (state / "gate-prev-run.log").exists()          
    assert not list(state.glob(".gate-last-run.*.tmp"))        
