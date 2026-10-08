"A server/ tree is tested once (policy): gate_record's key, per-machine record and\ncached run_gate."
import json
import sys

import pytest

from agent_context import gate_record, paths


def _make_server(tmp_path, *, extra_pkg_files=(), extra_test_files=(), pyproject="p\n",
                 uv_lock="l\n"):
    server = tmp_path / "server"
    pkg = server / "src" / "agent_context"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for name, body in extra_pkg_files:
        (pkg / name).write_text(body)
    tests = server / "tests"
    tests.mkdir()
    for name, body in extra_test_files:
        (tests / name).write_text(body)
    if pyproject is not None:
        (server / "pyproject.toml").write_text(pyproject)
    if uv_lock is not None:
        (server / "uv.lock").write_text(uv_lock)
    return server




def test_key_stable_across_mtime_only_touch(tmp_path):
    server = _make_server(tmp_path, extra_pkg_files=[("a.py", "x = 1\n")])
    first = gate_record.gate_key(server)
    (server / "src" / "agent_context" / "a.py").touch()
    assert gate_record.gate_key(server) == first


def test_key_changes_on_test_file_edit(tmp_path):
    server = _make_server(tmp_path, extra_test_files=[("test_a.py", "def test_a(): pass\n")])
    first = gate_record.gate_key(server)
    (server / "tests" / "test_a.py").write_text("def test_a(): assert True\n")
    assert gate_record.gate_key(server) != first


def test_key_changes_on_package_file_edit(tmp_path):
    server = _make_server(tmp_path, extra_pkg_files=[("a.py", "x = 1\n")])
    first = gate_record.gate_key(server)
    (server / "src" / "agent_context" / "a.py").write_text("x = 2\n")
    assert gate_record.gate_key(server) != first


def test_key_changes_on_uv_lock_edit(tmp_path):
    server = _make_server(tmp_path)
    first = gate_record.gate_key(server)
    (server / "uv.lock").write_text("changed\n")
    assert gate_record.gate_key(server) != first


def test_key_changes_on_pyproject_edit(tmp_path):
    server = _make_server(tmp_path)
    first = gate_record.gate_key(server)
    (server / "pyproject.toml").write_text("changed\n")
    assert gate_record.gate_key(server) != first


class _FakeVersionInfo:
    major = 9
    minor = 9


def test_key_changes_with_python_version(tmp_path, monkeypatch):
    server = _make_server(tmp_path)
    first = gate_record.gate_key(server)
    monkeypatch.setattr(gate_record.sys, "version_info", _FakeVersionInfo())
    assert gate_record.gate_key(server) != first




def test_key_uses_the_target_pythons_version_not_the_calling_processs(tmp_path, monkeypatch):
    server = _make_server(tmp_path)
    monkeypatch.setattr(gate_record, "_PY_VERSION_CACHE", {})
    monkeypatch.setattr(gate_record.sys, "version_info", _FakeVersionInfo())  
    queried = {"/venv/py": "3.11", "/system/py3": "3.13"}
    monkeypatch.setattr(gate_record, "_python_version", lambda p: queried[p])
    a = gate_record.gate_key(server, "/venv/py")
    b = gate_record.gate_key(server, "/venv/py")
    c = gate_record.gate_key(server, "/system/py3")
    assert a == b                          
    assert a != c                          
    
    assert a != gate_record.gate_key(server)


def test_python_version_is_queried_once_per_interpreter_and_cached(monkeypatch):
    monkeypatch.setattr(gate_record, "_PY_VERSION_CACHE", {})
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        import subprocess
        return subprocess.CompletedProcess(argv, 0, stdout="3.12\n")

    monkeypatch.setattr(gate_record.subprocess, "run", fake_run)
    assert gate_record._python_version("/some/py") == "3.12"
    assert gate_record._python_version("/some/py") == "3.12"
    assert calls == ["/some/py"]


def _probe_answers(monkeypatch, stdout):
    monkeypatch.setattr(gate_record, "_PY_VERSION_CACHE", {})
    monkeypatch.setattr(gate_record, "_XDIST_CACHE", {})
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        import subprocess
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    monkeypatch.setattr(gate_record.subprocess, "run", fake_run)
    return calls


def test_parallel_args_for_another_interpreter_come_from_the_version_probe(monkeypatch):
    calls = _probe_answers(monkeypatch, "3.12\nxdist\n")
    assert gate_record.parallel_args("/venv/py") == ["-n", "auto"]
    assert gate_record._python_version("/venv/py") == "3.12"
    assert calls == ["/venv/py"]            


def test_parallel_args_are_empty_when_the_interpreter_has_no_xdist(monkeypatch):
    _probe_answers(monkeypatch, "3.12\nserial\n")
    assert gate_record.parallel_args("/venv/py") == []
    _probe_answers(monkeypatch, "3.12\n")   
    assert gate_record.parallel_args("/venv/py") == []


def test_parallel_args_ask_this_interpreter_directly_each_time(monkeypatch):
    calls = _probe_answers(monkeypatch, "3.12\nserial\n")
    import importlib.util
    found = []
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: found.append(name) or (object() if len(found) > 1 else None))
    assert gate_record.parallel_args(sys.executable) == []
    assert gate_record.parallel_args(sys.executable) == ["-n", "auto"]   
    assert calls == [] and found == ["xdist", "xdist"]


def test_run_gate_passes_the_parallel_args_to_pytest(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    server = _make_server(tmp_path)
    monkeypatch.setattr(gate_record, "parallel_args", lambda python: ["-n", "auto"])
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        import subprocess
        return subprocess.CompletedProcess(argv, 0, stdout=b"1 passed in 0.01s\n")

    monkeypatch.setattr(gate_record.subprocess, "run", fake_run)
    monkeypatch.setattr(gate_record, "gate_key", lambda server_dir, python=None: "k")
    assert gate_record.run_gate(server, "/venv/py", by="cli")[0] is True
    assert seen == [["/venv/py", "-m", "pytest", "-q", "-n", "auto"]]


def test_python_version_is_none_when_the_interpreter_cannot_run(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_record, "_PY_VERSION_CACHE", {})
    server = _make_server(tmp_path)

    def fake_run(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(gate_record.subprocess, "run", fake_run)
    assert gate_record._python_version("/no/such/python") is None
    assert gate_record.gate_key(server, "/no/such/python") is None


def test_key_is_none_when_a_package_file_cannot_be_read(tmp_path, monkeypatch):
    server = _make_server(tmp_path, extra_pkg_files=[("a.py", "x = 1\n")])
    real_read_bytes = gate_record.Path.read_bytes

    def flaky(self):
        if self.name == "a.py":
            raise OSError("gone")
        return real_read_bytes(self)

    monkeypatch.setattr(gate_record.Path, "read_bytes", flaky)
    assert gate_record.gate_key(server) is None




def test_record_pass_then_has_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    assert gate_record.has_passed("k1") is None
    gate_record.record_pass("k1", {"n": 3}, "cli")
    rec = gate_record.has_passed("k1")
    assert rec is not None and rec["counts"] == {"n": 3} and rec["by"] == "cli"


def test_record_is_capped_to_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    for i in range(gate_record._MAX_RECORDS + 5):
        gate_record.record_pass(f"k{i}", {}, "cli")
    data = json.loads((tmp_path / gate_record.RECORD_NAME).read_text())
    assert len(data) == gate_record._MAX_RECORDS
    assert "k0" not in data                 
    assert f"k{gate_record._MAX_RECORDS + 4}" in data


def test_corrupt_record_is_treated_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    (tmp_path / gate_record.RECORD_NAME).write_text("not json{")
    assert gate_record.has_passed("k1") is None
    gate_record.record_pass("k1", {}, "cli")            
    assert gate_record.has_passed("k1") is not None


def test_missing_record_is_treated_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    assert gate_record.has_passed("k1") is None




def test_pytest_counts_parses_the_summary_line():
    out = "....\n" + "10 passed, 2 skipped in 1.23s\n"
    assert gate_record.pytest_counts(out) == {"n": 10, "failed": 0, "skipped": 2, "reruns": 0}


def test_pytest_counts_is_empty_without_a_summary_line():
    assert gate_record.pytest_counts("no summary here\n") == {}








def _no_version_query(monkeypatch):
    monkeypatch.setattr(gate_record, "_PY_VERSION_CACHE", {sys.executable: "test"})


def test_run_gate_runs_and_records_on_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    _no_version_query(monkeypatch)
    server = _make_server(tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        import subprocess
        return subprocess.CompletedProcess(argv, 0, stdout=b"1 passed in 0.01s\n")

    monkeypatch.setattr(gate_record.subprocess, "run", fake_run)
    ok, counts, output, cached = gate_record.run_gate(server, sys.executable, by="cli")
    assert ok is True and cached is False and counts == {"n": 1, "failed": 0, "skipped": 0,
                                                           "reruns": 0}
    assert len(calls) == 1

    
    ok2, counts2, output2, cached2 = gate_record.run_gate(server, sys.executable, by="cli")
    assert ok2 is True and cached2 is True and counts2 == counts and output2 == ""
    assert len(calls) == 1


def test_run_gate_does_not_record_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    _no_version_query(monkeypatch)
    server = _make_server(tmp_path)

    def fake_run(argv, **kwargs):
        import subprocess
        return subprocess.CompletedProcess(argv, 1, stdout=b"1 failed in 0.01s\n")

    monkeypatch.setattr(gate_record.subprocess, "run", fake_run)
    ok, counts, output, cached = gate_record.run_gate(server, sys.executable, by="cli")
    assert ok is False and cached is False
    key = gate_record.gate_key(server, sys.executable)
    assert gate_record.has_passed(key) is None


def test_run_gate_force_bypasses_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    _no_version_query(monkeypatch)
    server = _make_server(tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        import subprocess
        return subprocess.CompletedProcess(argv, 0, stdout=b"1 passed in 0.01s\n")

    monkeypatch.setattr(gate_record.subprocess, "run", fake_run)
    gate_record.run_gate(server, sys.executable, by="cli")
    assert len(calls) == 1
    ok, _counts, _output, cached = gate_record.run_gate(server, sys.executable, by="cli", force=True)
    assert ok is True and cached is False
    assert len(calls) == 2


def test_run_gate_key_and_cache_track_the_target_python_not_the_running_one(tmp_path, monkeypatch):
    "A caller (the system python3 running store-wt-finish.py) targets a different\n    interpreter (the server venv's python) than the one it runs under. The cache is\n    keyed on the target, so a second caller under yet another interpreter still hits\n    the same recorded pass, once both have queried the same target's version."
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(gate_record, "_PY_VERSION_CACHE", {"/venv/py": "3.12"})
    server = _make_server(tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        import subprocess
        return subprocess.CompletedProcess(argv, 0, stdout=b"1 passed in 0.01s\n")

    monkeypatch.setattr(gate_record.subprocess, "run", fake_run)
    ok, _counts, _output, cached = gate_record.run_gate(server, "/venv/py", by="wt-finish")
    assert ok is True and cached is False and len(calls) == 1

    
    
    monkeypatch.setattr(gate_record.sys, "version_info", _FakeVersionInfo())
    ok2, _counts2, _output2, cached2 = gate_record.run_gate(server, "/venv/py", by="precommit")
    assert ok2 is True and cached2 is True
    assert len(calls) == 1                 
