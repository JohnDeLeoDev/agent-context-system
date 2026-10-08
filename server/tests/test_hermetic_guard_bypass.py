'The guard also refuses the write routes the first version missed: a symlink into a protected\nroot, a dir_fd-relative path, and symlink, link, truncate and chmod calls.'
from __future__ import annotations

import os

import pytest
from hermetic_guard import Guard, HermeticViolation


@pytest.fixture
def guard(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    return Guard([str(real)]), real


def test_open_through_a_symlink_into_a_protected_root_is_refused(guard, tmp_path):
    g, real = guard
    link = tmp_path / "alias"
    link.symlink_to(real)
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(link / "c"), "w", 0))
    assert len(g.take_all()) == 1


def test_dir_fd_relative_open_is_refused(guard):
    g, real = guard
    fd = os.open(real, os.O_RDONLY)
    try:
        with pytest.raises(HermeticViolation):
            g.audit("os.remove", ("victim", fd))
        with pytest.raises(HermeticViolation):
            g.audit("os.mkdir", ("sub", 0o777, fd))
    finally:
        os.close(fd)
    assert len(g.take_all()) == 2


@pytest.mark.parametrize("event,args", [
    ("os.symlink", ("x", "{root}/l", None)),
    ("os.link", ("x", "{root}/l", None, None)),
    ("os.link", ("{root}/x", "elsewhere", None, None)),
    ("os.truncate", ("{root}/f", 0)),
    ("os.chmod", ("{root}/f", 0o600, None)),
    ("os.chown", ("{root}/f", 1, 1, None)),
])
def test_other_write_routes_are_refused(guard, event, args):
    g, real = guard
    args = tuple(a.format(root=real) if isinstance(a, str) else a for a in args)
    with pytest.raises(HermeticViolation):
        g.audit(event, args)
    assert g.take_all()


def test_paths_outside_the_roots_pass(guard, tmp_path):
    g, _ = guard
    g.audit("os.truncate", (str(tmp_path / "other"), 0))
    g.audit("os.chmod", (str(tmp_path / "other"), 0o600, None))
    assert g.take_all() == []


def test_default_roots_creates_nothing(tmp_path, monkeypatch):
    from hermetic_guard import default_roots
    monkeypatch.delenv("AGENT_CONTEXT_GUARD_ROOTS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    roots = default_roots()
    assert str(tmp_path / "xdg" / "agent-context") in roots
    assert not (tmp_path / "xdg").exists() and not (tmp_path / ".local").exists()
