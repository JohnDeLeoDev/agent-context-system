'test daemon selfcheck.'
import os

import pytest

from agent_context import daemon


@pytest.fixture(autouse=True)
def _rearm():
    daemon._LOG_UNLINKED_WARNED = False
    yield
    daemon._LOG_UNLINKED_WARNED = False


_REAL_FSTAT = os.fstat        


def _as_stdout(monkeypatch, fd):
    'Make check_log_alive see `fd` where it looks for fd 1.'
    monkeypatch.setattr(daemon.os, "fstat", lambda n: _REAL_FSTAT(fd))


def test_a_live_log_is_silent(tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text("hello\n")
    fd = os.open(str(log), os.O_RDONLY)
    _as_stdout(monkeypatch, fd)
    try:
        assert daemon.check_log_alive() is None
    finally:
        os.close(fd)


def test_an_unlinked_log_is_reported_once(tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text("hello\n")
    fd = os.open(str(log), os.O_RDONLY)
    _as_stdout(monkeypatch, fd)
    notified = []
    monkeypatch.setattr(daemon, "_notify", notified.append)
    try:
        log.unlink()                     
        msg = daemon.check_log_alive()
        assert msg and "UNLINKED" in msg
        assert len(notified) == 1
        assert daemon.check_log_alive() is None   
        assert len(notified) == 1
    finally:
        os.close(fd)


def test_a_pipe_or_tty_is_never_judged(monkeypatch):
    r, w = os.pipe()
    _as_stdout(monkeypatch, r)
    try:
        assert daemon.check_log_alive() is None
    finally:
        os.close(r)
        os.close(w)


def test_gate_dump_is_bounded_per_test_not_by_the_gate_deadline(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT_GATE_PER_TEST", raising=False)
    assert daemon._gate_per_test_secs() == daemon._GATE_PER_TEST_SECS
    
    assert min(daemon._gate_per_test_secs(), max(5, 120 - 10)) == daemon._GATE_PER_TEST_SECS

    monkeypatch.setenv("AGENT_CONTEXT_GATE_PER_TEST", "45")
    assert daemon._gate_per_test_secs() == 45
    monkeypatch.setenv("AGENT_CONTEXT_GATE_PER_TEST", "1")
    assert daemon._gate_per_test_secs() == 5      
