'paths.write_atomic takes bytes and an explicit mode, so oauth and relay_materialize\nno longer hand-roll tmp+os.replace (invariant one-atomic-writer).'
import os
import stat
from pathlib import Path

import pytest

from agent_context import paths


def test_writes_bytes_with_the_explicit_mode(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "f"
    paths.write_atomic(target, b"\xe2\x9c\x93 bytes", 0o600)
    assert target.read_bytes() == b"\xe2\x9c\x93 bytes"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_explicit_mode_overrides_the_existing_targets_mode(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_text("old")
    target.chmod(0o755)
    paths.write_atomic(target, "new", 0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_without_a_mode_the_existing_targets_mode_is_kept(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_text("old")
    target.chmod(0o755)
    paths.write_atomic(target, "new")
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_a_failed_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "f"

    def boom(*a: object, **k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        paths.write_atomic(target, b"x", 0o600)
    assert [q for q in tmp_path.iterdir() if q.name.startswith(".ac-tmp-")] == []
