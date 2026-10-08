"run_gate brings the daemon's own venv up to the lockfile before the suite runs."
import logging
import subprocess

import pytest

from agent_context import daemon


@pytest.fixture
def gate(monkeypatch, tmp_path):
    "A server tree whose .venv is the running interpreter's prefix, a faked uv, and a pytest\n    that passes. Records every subprocess.run argv."
    server = tmp_path / "server"
    (server / "tests").mkdir(parents=True)
    (server / ".venv").mkdir()
    monkeypatch.setenv("AGENT_CONTEXT_GATE_SYNC", "1")
    monkeypatch.setattr(daemon.sys, "prefix", str(server / ".venv"))
    monkeypatch.setattr(daemon, "_source_files", list)
    monkeypatch.setattr(daemon, "_uv_path", lambda: "/fake/uv")
    monkeypatch.setattr(daemon, "_server_dir", lambda: server)
    calls = []
    state = {"uv_rc": 0, "uv_exc": None}

    def run(argv, *a, **k):
        calls.append(list(argv))
        if argv[0] == "/fake/uv":
            if state["uv_exc"]:
                raise state["uv_exc"]
            return subprocess.CompletedProcess(argv, state["uv_rc"], "", "resolution failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(daemon.subprocess, "run", run)
    monkeypatch.setattr(daemon, "_run_pytest", lambda *a, **k: ("pass", "gate passed", []))
    return server, calls, state


def _uv_calls(calls):
    return [c for c in calls if c[0] == "/fake/uv"]


def test_the_gate_syncs_the_dev_group_frozen_and_inexact_before_the_suite(gate):
    server, calls, _ = gate
    assert daemon.run_gate(timeout=60) == (True, "gate passed")
    (uv,) = _uv_calls(calls)
    assert uv[1] == "sync"
    for flag in ("--frozen", "--inexact"):
        assert flag in uv
    assert uv[uv.index("--group") + 1] == "dev"
    assert uv[uv.index("--directory") + 1] == str(server)


def test_a_failed_sync_is_logged_and_does_not_decide_the_gate(gate, caplog):
    _, _, state = gate
    state["uv_rc"] = 2
    with caplog.at_level(logging.INFO, logger="agent-context"):
        assert daemon.run_gate(timeout=60) == (True, "gate passed")
    assert any("gate_venv_sync failed" in r.getMessage() and "resolution failed" in r.getMessage()
               for r in caplog.records)


def test_a_sync_that_times_out_does_not_decide_the_gate(gate, caplog):
    _, _, state = gate
    state["uv_exc"] = subprocess.TimeoutExpired("uv", 1)
    with caplog.at_level(logging.INFO, logger="agent-context"):
        assert daemon.run_gate(timeout=60)[0] is True
    assert any("gate_venv_sync failed: uv sync timed out" in r.getMessage() for r in caplog.records)


def test_a_daemon_on_another_interpreter_leaves_the_venv_alone(gate, monkeypatch, tmp_path):
    _, calls, _ = gate
    monkeypatch.setattr(daemon.sys, "prefix", str(tmp_path / "elsewhere"))
    assert daemon.run_gate(timeout=60)[0] is True
    assert _uv_calls(calls) == []


def test_no_uv_skips_the_sync(gate, monkeypatch):
    _, calls, _ = gate
    monkeypatch.setattr(daemon, "_uv_path", lambda: None)
    assert daemon.run_gate(timeout=60)[0] is True
    assert _uv_calls(calls) == []


def test_the_sync_can_be_switched_off(gate, monkeypatch):
    _, calls, _ = gate
    monkeypatch.setenv("AGENT_CONTEXT_GATE_SYNC", "0")
    assert daemon.run_gate(timeout=60)[0] is True
    assert _uv_calls(calls) == []


def test_a_py_compile_failure_never_syncs(gate, monkeypatch):
    _, calls, _ = gate

    def run(argv, *a, **k):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "SyntaxError")

    monkeypatch.setattr(daemon.subprocess, "run", run)
    ok, detail = daemon.run_gate(timeout=60)
    assert not ok and "py_compile failed" in detail
    assert _uv_calls(calls) == []


def test_uv_is_found_in_local_bin_when_path_is_bare(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon.shutil, "which", lambda name: None)
    monkeypatch.setattr(daemon.Path, "home", classmethod(lambda cls: tmp_path))
    assert daemon._uv_path() is None
    uv = tmp_path / ".local" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    assert daemon._uv_path() == str(uv)


def test_the_suite_turns_the_real_sync_off():
    
    assert not daemon._gate_sync_enabled()
