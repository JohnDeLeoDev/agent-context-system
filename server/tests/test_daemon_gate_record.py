'daemon.run_gate honors the shared gate_record (policy): a cached pass skips pytest\nbut still runs py_compile. daemon passes its own `sys.executable` as the target\npython, so the key must match `gate_key(server_dir, sys.executable)`, not the\nno-argument form.'
import sys

import pytest

from agent_context import daemon, gate_record


@pytest.fixture
def gate(monkeypatch, tmp_path):
    server = tmp_path / "server"
    (server / "tests").mkdir(parents=True)
    (server / "src" / "agent_context").mkdir(parents=True)
    (server / "src" / "agent_context" / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(daemon, "_server_dir", lambda: server)
    monkeypatch.setattr(daemon, "_source_files", list)
    monkeypatch.setattr(gate_record, "_PY_VERSION_CACHE",
                        {sys.executable: f"{sys.version_info.major}.{sys.version_info.minor}"})

    import subprocess
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    return server


def test_a_recorded_pass_skips_pytest(gate, monkeypatch):
    launched = []
    monkeypatch.setattr(daemon, "_run_pytest",
                        lambda *a, **k: launched.append(1) or ("pass", "gate passed", []))
    key = gate_record.gate_key(gate, sys.executable)
    gate_record.record_pass(key, {}, "wt-finish")
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is True
    assert "cached" in detail
    assert launched == []                    


def test_a_pass_with_no_record_runs_pytest_and_records_it(gate, monkeypatch):
    monkeypatch.setattr(daemon, "_run_pytest",
                        lambda *a, **k: ("pass", "gate passed", []))
    key = gate_record.gate_key(gate, sys.executable)
    assert gate_record.has_passed(key) is None
    ok, detail = daemon.run_gate(timeout=60)
    assert ok is True and detail == "gate passed"
    assert gate_record.has_passed(key) is not None
