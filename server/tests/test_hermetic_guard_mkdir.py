"mkdir of a directory that already exists changes nothing, so the guard lets it pass.\n\nThe ls daemon's gate ran the suite and the guard recorded `created /home/user/.local/state/\nagent-context` before the first test: code that runs at import or collection time calls\n`Path.mkdir(parents=True, exist_ok=True)` on the real state dir, which already exists. The\naudit event fires before the OS reports EEXIST, so the guard failed test_adoption at setup and\nls stayed on old code. A mkdir that would create something is still refused."
from __future__ import annotations

import pytest

from hermetic_guard import Guard, HermeticViolation


@pytest.fixture
def guarded(tmp_path):
    root = tmp_path / "guarded"
    root.mkdir()
    (root / "sub").mkdir()
    return Guard([str(root)]), root


def test_mkdir_of_the_existing_root_is_allowed(guarded):
    g, root = guarded
    g.audit("os.mkdir", (str(root), 0o777, None))
    assert g.take_all() == []


def test_mkdir_of_an_existing_subdirectory_is_allowed(guarded):
    g, root = guarded
    g.audit("os.mkdir", (str(root / "sub"), 0o777, None))
    assert g.take_all() == []


def test_mkdir_that_would_create_something_is_still_refused(guarded):
    g, root = guarded
    with pytest.raises(HermeticViolation):
        g.audit("os.mkdir", (str(root / "new"), 0o777, None))
    assert len(g.take_all()) == 1


def test_mkdir_over_an_existing_file_is_still_refused(guarded):
    'EEXIST either way, but only a directory is a known no-op.'
    g, root = guarded
    (root / "f").write_text("x")
    with pytest.raises(HermeticViolation):
        g.audit("os.mkdir", (str(root / "f"), 0o777, None))


def test_a_missing_root_is_still_refused(tmp_path):
    g = Guard([str(tmp_path / "absent")])
    with pytest.raises(HermeticViolation):
        g.audit("os.mkdir", (str(tmp_path / "absent"), 0o777, None))


def test_only_mkdir_gets_the_exemption(guarded):
    g, root = guarded
    with pytest.raises(HermeticViolation):
        g.audit("os.rmdir", (str(root / "sub"), None))
    with pytest.raises(HermeticViolation):
        g.audit("os.rename", (str(root / "sub"), str(root / "sub2"), None, None))
