'run_gate says WHY it did not pass, and does not judge the code on a run that was cut short.'
import logging
import signal
import subprocess

import pytest

from agent_context import daemon

FAILING_OUT = (
    "....F.....\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_a.py::test_x - assert 1 == 2\n"
    "ERROR tests/test_b.py::test_y - boom\n"
    "1 failed, 1 error, 8 passed\n")


class _World:
    def __init__(self, monkeypatch):
        self.plan = []          
        self.launched = 0
        self.compile_rc = 0
        world = self

        class Proc:
            def __init__(self, *a, **k):
                world.launched += 1
                self.pid = 4000 + world.launched
                self.returncode = None
                self.spec = world.plan.pop(0)
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                rc, out, err = self.spec
                if rc == "timeout" and self.calls == 1:
                    raise subprocess.TimeoutExpired("pytest", timeout or 0.0)
                self.returncode = -9 if rc == "timeout" else rc
                return out, err

            def kill(self):
                pass

        monkeypatch.setattr(daemon.subprocess, "Popen", Proc)
        monkeypatch.setattr(
            daemon.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, world.compile_rc, "", "compile error"))
        monkeypatch.setattr(daemon, "_source_files", list)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT_GATE_RERUN", raising=False)
    return _World(monkeypatch)


def test_a_pytest_killed_by_sigterm_is_interrupted_and_is_not_rerun(world):
    world.plan = [(-signal.SIGTERM, "....\n", "")]
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is False
    assert detail.startswith("interrupted:"), detail
    assert daemon.gate_reason_class(detail) == "interrupted"
    assert "SIGTERM" in detail
    assert world.launched == 1                  


def test_a_keyboard_interrupt_is_interrupted_too(world):
    world.plan = [(2, "!!!!!! KeyboardInterrupt !!!!!!\n", "")]
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is False and daemon.gate_reason_class(detail) == "interrupted"


def test_py_compile_killed_by_a_signal_is_interrupted_and_a_syntax_error_is_failed(world):
    world.compile_rc = -signal.SIGKILL
    ok, detail = daemon.run_gate(timeout=60)
    assert not ok and daemon.gate_reason_class(detail) == "interrupted"
    world.compile_rc = 1
    ok, detail = daemon.run_gate(timeout=60)
    assert not ok and daemon.gate_reason_class(detail) == "failed"
    assert "py_compile failed" in detail
    assert world.launched == 0                  


def test_a_failure_that_reproduces_on_the_rerun_is_failed_and_names_the_tests(world):
    world.plan = [(1, FAILING_OUT, ""), (1, FAILING_OUT, "")]
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is False
    assert daemon.gate_reason_class(detail) == "failed"
    assert "tests/test_a.py::test_x" in detail and "tests/test_b.py::test_y" in detail
    assert "reproduced" in detail
    assert world.launched == 2                  


def test_a_failure_that_passes_on_the_rerun_passes_and_says_what_flaked(world, caplog):
    world.plan = [(1, FAILING_OUT, ""), (0, "10 passed\n", "")]
    with caplog.at_level(logging.WARNING, logger="agent-context"):
        ok, detail = daemon.run_gate(timeout=60)
    assert ok is True
    assert detail.startswith("gate passed")
    assert "tests/test_a.py::test_x" in detail
    assert daemon.gate_reason_class(detail) == ""
    assert world.launched == 2
    assert any("gate_flaked" in r.getMessage() and "tests/test_a.py::test_x" in r.getMessage()
               for r in caplog.records)


def test_the_rerun_can_be_switched_off(world, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_GATE_RERUN", "0")
    world.plan = [(1, FAILING_OUT, "")]
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is False and daemon.gate_reason_class(detail) == "failed"
    assert world.launched == 1


def test_a_run_interrupted_during_the_rerun_is_interrupted_not_failed(world):
    world.plan = [(1, FAILING_OUT, ""), (-signal.SIGTERM, "..\n", "")]
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is False and daemon.gate_reason_class(detail) == "interrupted"


def test_a_timeout_is_its_own_class_and_is_not_rerun(world):
    world.plan = [("timeout", "", "Timeout (>50.0s)\nFile \"tests/test_hang.py\", line 9, in test_it\n")]
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is False
    assert detail.startswith("timeout:") and daemon.gate_reason_class(detail) == "timeout"
    assert "pid 4001" in detail and "test_it" in detail
    assert world.launched == 1


def test_a_pass_keeps_its_plain_detail(world):
    world.plan = [(0, "10 passed\n", "")]
    assert daemon.run_gate(timeout=60) == (True, "gate passed")


def test_reason_class_reads_only_the_first_word():
    assert daemon.gate_reason_class("failed: pytest failed: x") == "failed"
    assert daemon.gate_reason_class("interrupted: ended by SIGTERM") == "interrupted"
    assert daemon.gate_reason_class("timeout: gate timed out") == "timeout"
    assert daemon.gate_reason_class("gate passed") == ""
    assert daemon.gate_reason_class("pytest failed: an old-style detail") == ""
    assert daemon.gate_reason_class("the run failed: not a prefix") == ""
    assert daemon.gate_reason_text("failed: pytest failed: x") == ("failed", "pytest failed: x")
    assert daemon.gate_reason_text("boom") == ("failed", "boom")   


def test_the_full_output_is_kept_bounded_and_the_previous_run_is_rotated(world):
    big = "x" * (1024 * 1024) + "\nFAILED tests/test_z.py::test_last - end\n"
    world.plan = [(1, big, ""), (1, "second attempt\n", "")]
    ok, detail = daemon.run_gate(timeout=60)
    state = daemon._state_dir()
    last, prev = state / "gate-last-run.log", state / "gate-prev-run.log"
    assert last.is_file() and prev.is_file()
    assert "second attempt" in last.read_text()
    assert "test_last" in prev.read_text()              
    assert prev.stat().st_size <= 300 * 1024            
    assert (last.stat().st_mode & 0o077) == 0           
    assert str(last) in detail                          




@pytest.fixture
def redeploy(monkeypatch):
    seen = {"notify": [], "gate": 0, "result": (False, "failed: pytest failed: x")}

    def fake_gate(timeout=120):
        seen["gate"] += 1
        return seen["result"]

    monkeypatch.setattr(daemon, "_do_exec", lambda: None)
    monkeypatch.setattr(daemon, "run_gate", fake_gate)
    monkeypatch.setattr(daemon, "_notify", lambda msg: seen["notify"].append(msg))
    monkeypatch.setattr(daemon, "_server_tree_dirty", lambda: False)
    monkeypatch.setattr(daemon, "_ATTEMPTED_VERSIONS", {})
    monkeypatch.setattr(daemon, "_STARTED_VERSION", 100.0)
    monkeypatch.setattr(daemon, "_code_version", lambda: 200.0)
    monkeypatch.setattr(daemon, "_STARTED_FINGERPRINT", "booted")
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "on-disk")
    monkeypatch.delenv("AGENT_CONTEXT_SELF_DEPLOY", raising=False)
    return seen


def test_the_failure_branch_names_the_class_in_the_log_and_the_notification(redeploy, caplog):
    redeploy["result"] = (False, "failed: pytest failed: FAILED tests/test_a.py::test_x")
    with caplog.at_level(logging.WARNING, logger="agent-context"):
        daemon.maybe_self_redeploy()
    assert any("gate failed" in m and "reason class failed" in m for m in redeploy["notify"])
    assert any("reason class failed" in r.getMessage() for r in caplog.records)


def test_a_timeout_notification_says_timeout(redeploy):
    redeploy["result"] = (False, "timeout: gate timed out (>240s) in pytest pid 5; stacks: x")
    daemon.maybe_self_redeploy()
    assert any("timed out" in m and "reason class timeout" in m for m in redeploy["notify"])


def test_an_interrupted_gate_notifies_nothing_and_holds_nothing(redeploy):
    redeploy["result"] = (False, "interrupted: pytest ended by SIGTERM; no verdict on this code")
    daemon.maybe_self_redeploy()
    assert redeploy["notify"] == []
    assert daemon._ATTEMPTED_VERSIONS == {}
    assert not (daemon._state_dir() / "gate-result.json").exists()
    daemon.maybe_self_redeploy()                        
    assert redeploy["gate"] == 2
