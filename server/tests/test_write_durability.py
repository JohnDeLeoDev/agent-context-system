'The one durability switch in the store, and the line it must never cross.\n\nThese tests exist so the switch cannot quietly grow past that: fsync off must still\nmean atomic, and the default must still mean durable.'
import os

import pytest

from agent_context import store as S


@pytest.fixture
def sync_calls(monkeypatch):
    'Record every os.fsync the store makes, without preventing it.'
    calls = []
    real = os.fsync
    monkeypatch.setattr(S.os, "fsync", lambda fd: (calls.append(fd), real(fd))[0])
    return calls


def test_the_default_is_durable(monkeypatch):
    'No env var set at all must mean fsync ON. The failure this guards is a\n    default that flips the wrong way and takes the durability with it silently.'
    monkeypatch.delenv("AGENT_CONTEXT_FSYNC", raising=False)
    assert S._fsync_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " 0 "])
def test_the_falsy_set_turns_it_off(monkeypatch, value):
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", value)
    assert S._fsync_enabled() is False


@pytest.mark.parametrize("value", ["1", "yes", "on", "", "anything"])
def test_everything_else_stays_durable(monkeypatch, value):
    'Fail SAFE on a value nobody recognizes. A typo in the variable must leave the\n    store durable, not silently unflushed.'
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", value)
    assert S._fsync_enabled() is True


def test_the_flag_is_read_per_call_not_frozen_at_import(monkeypatch):
    'The daemon re-execs onto new code without a fresh interpreter for every\n    module, and tests monkeypatch the environment per test. A cached value would\n    make both of those lie.'
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", "0")
    assert S._fsync_enabled() is False
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", "1")
    assert S._fsync_enabled() is True


def test_fsync_runs_when_durability_is_on(tmp_path, monkeypatch, sync_calls):
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", "1")
    S.ContextStore._write_atomic(str(tmp_path / "a.md"), "hello")
    assert sync_calls, "durable mode must flush before the rename"


def test_fsync_is_skipped_when_durability_is_off(tmp_path, monkeypatch, sync_calls):
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", "0")
    S.ContextStore._write_atomic(str(tmp_path / "a.md"), "hello")
    assert not sync_calls, "the whole point is that the flush does not happen"


def test_the_write_is_still_correct_with_the_flush_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", "0")
    p = tmp_path / "a.md"
    S.ContextStore._write_atomic(str(p), "first")
    S.ContextStore._write_atomic(str(p), "second")
    assert p.read_text() == "second"


def test_atomicity_does_not_depend_on_the_flush(tmp_path, monkeypatch):
    'test atomicity does not depend on the flush.'
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", "0")
    p = tmp_path / "a.md"
    S.ContextStore._write_atomic(str(p), "the good content")

    class Boom(Exception):
        pass

    def explode(*a, **k):
        raise Boom

    monkeypatch.setattr(S.os, "fdopen", explode)
    with pytest.raises(Boom):
        S.ContextStore._write_atomic(str(p), "the write that dies")

    assert p.read_text() == "the good content"


def test_a_failed_write_leaves_no_litter(tmp_path, monkeypatch):
    "The temp file lands in the target's own directory, so anything left behind is\n    picked up by the autocommit's `git add -A` and rides to four remotes."
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", "0")
    d = tmp_path / "entities"
    d.mkdir()

    def explode(*a, **k):
        raise RuntimeError("mid-write")

    monkeypatch.setattr(S.os, "fdopen", explode)
    with pytest.raises(RuntimeError):
        S.ContextStore._write_atomic(str(d / "a.md"), "x")

    assert list(d.iterdir()) == []
