"The guard leaves the process's TMPDIR alone, because that is the sandbox.\n\nThe exemption must not open a hole: paths are compared after realpath, so `..` and a symlink\nfrom inside TMPDIR to the protected root are still refused, and a TMPDIR that is a protected\nroot, or contains one, exempts nothing."
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hermetic_guard import Guard, HermeticViolation

TESTS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TESTS_DIR.parent


@pytest.fixture
def layout(tmp_path, monkeypatch):
    state = tmp_path / "guarded"
    gate_tmp = state / "gate-tmp"
    gate_tmp.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(gate_tmp))
    return Guard([str(state)]), state, gate_tmp


def test_a_path_under_tmpdir_is_allowed_even_inside_a_protected_root(layout):
    g, _, gate_tmp = layout
    g.audit("os.mkdir", (str(gate_tmp / "pytest-of-user"), 0o700, None))
    g.audit("open", (str(gate_tmp / "pytest-of-user" / "f"), "w", 0))
    g.audit("os.rename", (str(gate_tmp / "a"), str(gate_tmp / "b"), None, None))
    assert g.take_all() == []


def test_the_rest_of_the_protected_root_is_still_refused(layout):
    g, state, _ = layout
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(state / "daemon.info"), "w", 0))
    with pytest.raises(HermeticViolation):
        g.audit("os.mkdir", (str(state / "gate-tmp-sibling"), 0o700, None))
    assert len(g.take_all()) == 2


def test_dotdot_out_of_tmpdir_into_the_root_is_refused(layout):
    g, state, gate_tmp = layout
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(gate_tmp / ".." / "daemon.info"), "w", 0))
    assert len(g.take_all()) == 1


def test_a_symlink_from_tmpdir_into_the_root_is_refused(layout):
    g, state, gate_tmp = layout
    (state / "real-dir").mkdir()
    (gate_tmp / "alias").symlink_to(state / "real-dir")
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(gate_tmp / "alias" / "f"), "w", 0))
    assert len(g.take_all()) == 1


def test_a_rename_from_tmpdir_into_the_root_is_refused(layout):
    g, state, gate_tmp = layout
    with pytest.raises(HermeticViolation):
        g.audit("os.rename", (str(gate_tmp / "a"), str(state / "daemon.info"), None, None))
    assert len(g.take_all()) == 1


def test_tmpdir_set_to_the_root_itself_exempts_nothing(tmp_path, monkeypatch):
    state = tmp_path / "guarded"
    state.mkdir()
    monkeypatch.setenv("TMPDIR", str(state))
    g = Guard([str(state)])
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(state / "daemon.info"), "w", 0))


def test_tmpdir_set_to_a_parent_of_the_root_exempts_nothing(tmp_path, monkeypatch):
    state = tmp_path / "home" / "guarded"
    state.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    g = Guard([str(state)])
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(state / "daemon.info"), "w", 0))


def test_tmpdir_unset_or_relative_exempts_nothing(tmp_path, monkeypatch):
    state = tmp_path / "guarded"
    state.mkdir()
    g = Guard([str(state)])
    monkeypatch.delenv("TMPDIR", raising=False)
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(state / "x"), "w", 0))
    monkeypatch.setenv("TMPDIR", "guarded")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(state / "x"), "w", 0))


MINI_CONFTEST = """
import os, sys
sys.path.insert(0, os.environ["TESTS_DIR"])
from hermetic_guard import (  # noqa: F401
    _hermetic_basetemp, _hermetic_guard, _no_external_network, pytest_sessionfinish)
"""

MINI_OK = """
def test_tmp_path_works(tmp_path):
    (tmp_path / "f").write_text("x")
    assert (tmp_path / "f").read_text() == "x"
"""

MINI_LEAK = """
import os

def test_write_to_the_state_dir_is_still_refused():
    with open(os.path.join(os.environ["FAKE_STATE"], "leak.txt"), "w") as fh:
        fh.write("x")
"""


def _gate_run(tmp_path, monkeypatch, files):
    "The daemon's own gate command (daemon._run_pytest) and environment (daemon._gate_env)\n    against a small copy of a suite, with the state dir pointed at a scratch directory that the\n    guard protects."
    from agent_context import daemon

    state = tmp_path / "gate-state"
    state.mkdir()
    suite = tmp_path / "mini-suite"
    suite.mkdir()
    (suite / "conftest.py").write_text(MINI_CONFTEST)
    for name, body in files.items():
        (suite / name).write_text(body)
    monkeypatch.setattr(daemon, "_state_dir", lambda: state)
    monkeypatch.setattr(daemon, "_save_gate_output", lambda *a, **k: None)
    monkeypatch.setenv("AGENT_CONTEXT_GUARD_ROOTS", str(state))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("TESTS_DIR", str(TESTS_DIR))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [str(SERVER_DIR / "src")] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
    result = daemon._run_pytest(sys.executable, suite, suite, 120, 120, 1)
    return result, state


def test_the_daemon_gate_passes_a_suite_that_uses_tmp_path(tmp_path, monkeypatch):
    (kind, detail, ids), state = _gate_run(tmp_path, monkeypatch, {"test_ok.py": MINI_OK})
    assert kind == "pass", detail
    assert (state / "gate-tmp").is_dir()          


def test_the_daemon_gate_still_fails_a_suite_that_writes_the_state_dir(tmp_path, monkeypatch):
    (kind, detail, ids), state = _gate_run(
        tmp_path, monkeypatch, {"test_ok.py": MINI_OK, "test_leak.py": MINI_LEAK})
    assert kind == "failed", detail
    assert not (state / "leak.txt").exists()
    assert any("test_write_to_the_state_dir_is_still_refused" in i for i in ids), ids
    assert not any("test_tmp_path_works" in i for i in ids), ids


def test_removing_a_symlink_inside_tmpdir_is_allowed_even_when_it_points_into_the_root(layout):
    'pytest deletes old temp dirs, and a test may leave a symlink to the real store in one.\n    remove and rmdir act on the link, not on its target.'
    g, state, gate_tmp = layout
    (state / "real-dir").mkdir()
    (gate_tmp / "alias").symlink_to(state / "real-dir")
    g.audit("os.remove", (str(gate_tmp / "alias"), None))
    g.audit("os.rename", (str(gate_tmp / "alias"), str(gate_tmp / "moved"), None, None))
    assert g.take_all() == []


def test_a_symlink_in_the_root_is_still_removed_only_as_a_path_in_the_root(layout):
    g, state, gate_tmp = layout
    (state / "elsewhere").mkdir()
    (state / "link").symlink_to(gate_tmp)
    with pytest.raises(HermeticViolation):
        g.audit("os.remove", (str(state / "link"), None))


def test_a_test_that_changes_tmpdir_can_still_use_its_own_tmp_path(tmp_path, monkeypatch):
    "The gate's basetemp is under the protected state dir. Changing TMPDIR mid-test (the\n    gate's own tests do) must not turn the test's tmp_path into a protected path."
    monkeypatch.setenv("TMPDIR", "/somewhere/else")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")
    (tmp_path / "d" / "f").unlink()
    (tmp_path / "d").rmdir()


def test_the_temp_directory_itself_can_be_opened_for_an_unnamed_file(layout):
    g, _, gate_tmp = layout
    g.audit("open", (str(gate_tmp), "w", 0))          
    assert g.take_all() == []
