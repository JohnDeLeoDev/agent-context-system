"A protected root that lies inside TMPDIR protects only its own subtree.\n\nThe gate's TMPDIR is <state>/gate-tmp. A test that runs a child pytest passes its own tmp_path\nas XDG_STATE_HOME, so the child's protected roots sit inside the inherited TMPDIR. The child's\nown temp dir, also under TMPDIR, must stay usable while those inner roots stay protected."
from __future__ import annotations

import pytest

from hermetic_guard import Guard, HermeticViolation


@pytest.fixture
def layout(tmp_path, monkeypatch):
    state = tmp_path / "state"
    tmpdir = state / "gate-tmp"
    inner = tmpdir / "xdg" / "agent-context"
    inner.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    return Guard([str(state), str(inner)]), tmpdir, inner


def test_the_rest_of_tmpdir_is_usable_when_a_root_lies_inside_it(layout):
    g, tmpdir, _ = layout
    g.audit("os.mkdir", (str(tmpdir / "pytest-of-user"), 0o700, None))
    g.audit("open", (str(tmpdir / "pytest-of-user" / "f"), "w", 0))
    assert g.take_all() == []


def test_the_inner_root_itself_and_its_subtree_stay_protected(layout):
    g, _, inner = layout
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(inner / "daemon.info"), "w", 0))
    with pytest.raises(HermeticViolation):
        g.audit("os.mkdir", (str(inner / "new-dir"), 0o700, None))
    assert len(g.take_all()) == 2


def test_a_root_that_contains_tmpdir_still_leaves_tmpdir_usable(tmp_path, monkeypatch):
    state = tmp_path / "state"
    tmpdir = state / "gate-tmp"
    tmpdir.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    g = Guard([str(state)])
    g.audit("os.mkdir", (str(tmpdir / "pytest-of-user"), 0o700, None))
    with pytest.raises(HermeticViolation):
        g.audit("open", (str(state / "daemon.info"), "w", 0))
