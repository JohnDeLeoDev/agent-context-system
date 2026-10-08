"The suite's hermetic guard (hermetic_guard.py) and the default-off relay updater."
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import hermetic_guard as HG
from hermetic_guard import GUARD, Guard, HermeticViolation

TESTS_DIR = Path(__file__).resolve().parent




@pytest.fixture
def real(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    return root


def test_a_write_under_a_protected_root_is_refused_and_named(real):
    g = Guard([str(real)])
    g.current = "test_x.py::test_one"
    with pytest.raises(HermeticViolation) as exc:
        g.audit("open", (str(real / "daemon.info"), "w", 0))
    text = str(exc.value)
    assert "test_x.py::test_one" in text and "daemon.info" in text
    assert "opened for writing" in text and str(real) in text
    assert "main or not started by a test" in text
    [v] = g.take("test_x.py::test_one")
    assert v["kind"] == "write" and v["path"].endswith("daemon.info")
    assert g.take("test_x.py::test_one") == []                 


@pytest.mark.parametrize("mode,flags", [("w", 0), ("a", 0), ("x", 0), ("r+", 0),
                                        (None, os.O_WRONLY), (None, os.O_RDWR),
                                        (None, os.O_CREAT), (None, os.O_TRUNC)])
def test_every_way_to_open_for_writing_is_caught(real, mode, flags):
    g = Guard([str(real)])
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(real / "f"), mode, flags))


def test_reads_and_writes_elsewhere_are_allowed(real, tmp_path):
    g = Guard([str(real)])
    g.audit("open", (str(real / "f"), "r", os.O_RDONLY | os.O_CLOEXEC))    
    g.audit("open", (str(real / "f"), "rb", 0))
    g.audit("open", (str(tmp_path / "elsewhere"), "w", 0))                 
    g.audit("open", (str(tmp_path / "real2" / "f"), "w", 0))               
    g.audit("open", (3, "w", 0))                                           
    
    
    g.audit("open", (str(real / "global" / "scripts" / "__pycache__" / "x.cpython-314.pyc.123"),
                     "wb", 0))
    assert g.violations == []


def test_rename_remove_mkdir_and_rmdir_under_a_root_are_caught(real, tmp_path):
    g = Guard([str(real)])
    with pytest.raises(HermeticViolation):
        g.audit("os.rename", (str(tmp_path / "a"), str(real / "b"), None, None))   
    with pytest.raises(HermeticViolation):
        g.audit("os.rename", (str(real / "a"), str(tmp_path / "b"), None, None))   
    with pytest.raises(HermeticViolation):
        g.audit("os.remove", (str(real / "a"), None))
    with pytest.raises(HermeticViolation):
        g.audit("os.mkdir", (str(real / "sub"), 0o777, None))
    with pytest.raises(HermeticViolation):
        g.audit("os.rmdir", (str(real / "sub"), None))
    g.audit("os.rename", (str(tmp_path / "a"), str(tmp_path / "b"), None, None))   
    assert len(g.violations) == 5


def test_the_root_itself_is_protected(real):
    'A root that does not exist yet cannot be created; mkdir of an existing one is a no-op\n    and is allowed (see test_hermetic_guard_mkdir.py).'
    g = Guard([str(real / "absent-root")])
    with pytest.raises(HermeticViolation):
        g.audit("os.mkdir", (str(real / "absent-root"), 0o777, None))


def test_an_allowlisted_prefix_is_not_refused(real, monkeypatch):
    g = Guard([str(real)])
    monkeypatch.setattr(HG, "ALLOWED_WRITE_PREFIXES", [str(real / "ok")])
    g.audit("open", (str(real / "ok" / "f"), "w", 0))
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(real / "other"), "w", 0))


def test_both_allowlists_start_empty():
    assert HG.ALLOWED_WRITE_PREFIXES == [] and HG.ALLOWED_HOSTS == []


def test_a_thread_started_by_an_earlier_test_is_named_when_it_outlives_it(real):
    g = Guard([str(real)])
    g.current = "test_a.py::test_starts_it"
    caught: list[str] = []
    go = threading.Event()

    def later():
        go.wait(5)
        try:
            g.audit("open", (str(real / "x"), "w", 0))
        except HermeticViolation as e:
            caught.append(str(e))

    t = threading.Thread(target=later, name="sync-loop")
    t.start()
    
    
    t._hermetic_started_in = "test_a.py::test_starts_it"  
    g.current = "test_b.py::test_runs_next"               
    go.set()
    t.join(5)
    assert len(caught) == 1
    assert "test_b.py::test_runs_next" in caught[0]
    assert "'sync-loop' was started by test_a.py::test_starts_it and outlived that test" in caught[0]
    [v] = g.take("test_b.py::test_runs_next")
    assert v["outlived"] is True and v["thread_test"] == "test_a.py::test_starts_it"


def test_a_thread_started_and_finished_inside_its_own_test_is_not_outlived(real):
    g = Guard([str(real)])
    g.current = "test_a.py::test_one"
    box: list[str] = []
    go = threading.Event()

    def now():
        go.wait(5)
        try:
            g.audit("open", (str(real / "x"), "w", 0))
        except HermeticViolation as e:
            box.append(str(e))

    t = threading.Thread(target=now)
    t.start()
    t._hermetic_started_in = "test_a.py::test_one"  
    go.set()
    t.join(5)
    assert "started in this test" in box[0] and "outlived" not in box[0]


def test_threads_are_tagged_with_the_test_that_started_them():
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    assert t._hermetic_started_in == GUARD.current  
    assert GUARD.current and GUARD.current.endswith(
        "test_threads_are_tagged_with_the_test_that_started_them")


def test_the_real_roots_include_the_state_dir_and_the_store_entity_dirs(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT_GUARD_ROOTS", raising=False)
    home = os.path.expanduser("~")
    joined = " ".join(HG.default_roots())
    assert os.path.join(home, ".agent-context", "machines") in joined
    assert os.path.join(home, ".agent-context", "global") in joined
    assert "state" in joined and ".agent-context/server" not in joined




@pytest.mark.parametrize("host,loopback", [
    ("127.0.0.1", True), ("127.9.9.9", True), ("::1", True), ("localhost", True),
    ("", True), ("0.0.0.0", True), ("app.localhost", True),
    ("192.0.2.1", False), ("93.184.216.34", False), ("100.106.166.75", False),
    ("example.invalid", False), ("2001:db8::1", False)])
def test_is_loopback(host, loopback):
    assert HG.is_loopback(host) is loopback


def test_a_connection_to_a_non_loopback_host_is_refused_and_recorded():
    s = socket.socket()
    try:
        with pytest.raises(HermeticViolation):
            s.connect(("192.0.2.1", 9))                 
    finally:
        s.close()
    [v] = GUARD.take(GUARD.current)                     
    assert v["kind"] == "network" and v["target"] == "192.0.2.1:9"
    assert "is not a loopback host" in HG.describe(v)


def test_connect_ex_is_refused_too():
    s = socket.socket()
    try:
        with pytest.raises(HermeticViolation):
            s.connect_ex(("93.184.216.34", 443))
    finally:
        s.close()
    assert len(GUARD.take(GUARD.current)) == 1


def test_loopback_and_unix_sockets_are_not_touched_by_the_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   
    s = socket.socket()
    try:
        with pytest.raises(OSError) as exc:
            s.connect(("127.0.0.1", 1))                 
        assert not isinstance(exc.value, HermeticViolation)
    finally:
        s.close()
    u = socket.socket(socket.AF_UNIX)
    try:
        with pytest.raises(FileNotFoundError):
            u.connect("no-such.sock")
    finally:
        u.close()
    assert GUARD.take(GUARD.current) == []




def test_a_relay_start_with_the_real_host_name_arms_nothing(monkeypatch):
    from agent_context import daemon, relay_materialize
    from agent_context import server as S
    monkeypatch.setenv("AGENT_CONTEXT_HOST", "example.invalid")
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "fake-token")
    monkeypatch.setattr(daemon, "is_remote", lambda: True)
    monkeypatch.setattr(relay_materialize, "materialize_on_start", lambda: True)
    assert S._materialize_if_remote() is True
    assert not [t for t in threading.enumerate() if t.name == "relay-update-check"]
    assert GUARD.take(GUARD.current) == []              




SYNTH_CONFTEST = """
import os
import sys

sys.path.insert(0, os.environ["TESTS_DIR"])
if not os.environ.get("SYNTH_NO_GUARD"):
    from hermetic_guard import _hermetic_guard, pytest_sessionfinish  # noqa: F401
"""

SYNTH_TESTS = """
import os
import threading
import time


def test_a_starts_a_thread_and_returns():
    def later():
        time.sleep(0.4)
        with open(os.path.join(os.environ["FAKE_REAL"], "leak.txt"), "w") as fh:
            fh.write("leaked")

    threading.Thread(target=later, name="leaky-loop", daemon=True).start()


def test_b_is_running_when_the_thread_writes():
    time.sleep(1.5)


def test_c_writes_directly():
    with open(os.path.join(os.environ["FAKE_REAL"], "direct.txt"), "w") as fh:
        fh.write("x")
"""


def _synthetic_run(tmp_path, guarded):
    tree = tmp_path / "synth"
    fake_real = tmp_path / "fake-real"
    tree.mkdir()
    fake_real.mkdir()
    (tree / "conftest.py").write_text(SYNTH_CONFTEST)
    (tree / "test_synth.py").write_text(SYNTH_TESTS)
    env = {**os.environ, "TESTS_DIR": str(TESTS_DIR), "FAKE_REAL": str(fake_real),
           "AGENT_CONTEXT_GUARD_ROOTS": str(fake_real), "PYTHONDONTWRITEBYTECODE": "1"}
    if not guarded:
        env["SYNTH_NO_GUARD"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
         str(tree)], cwd=str(tree), env=env, capture_output=True, text=True, timeout=120)
    return proc, fake_real


def test_without_the_guard_the_synthetic_leak_reaches_the_protected_directory(tmp_path):
    proc, fake_real = _synthetic_run(tmp_path, guarded=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (fake_real / "leak.txt").exists() and (fake_real / "direct.txt").exists()


def test_with_the_guard_the_leak_is_blocked_and_the_failure_names_test_path_and_thread(tmp_path):
    proc, fake_real = _synthetic_run(tmp_path, guarded=True)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert not (fake_real / "leak.txt").exists()        
    assert not (fake_real / "direct.txt").exists()
    
    
    assert "test_synth.py::test_b_is_running_when_the_thread_writes" in out
    assert "leak.txt" in out
    assert ("thread 'leaky-loop' was started by test_synth.py::test_a_starts_a_thread_and_returns"
            " and outlived that test") in out
    assert "test_synth.py::test_c_writes_directly" in out and "direct.txt" in out


def test_the_real_relay_installer_is_never_run_by_default(tmp_path):
    'A bridge that sees a new release runs agent-context-relay-install (policy). Under the suite\n    that must be a no-op, or a test would build a release under the real home and exec into it.'
    from agent_context import relay_swap
    assert relay_swap.install('"any"', tmp_path) is None
