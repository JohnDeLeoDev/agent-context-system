"The deploy gate runs pytest with TMPDIR on the state dir's filesystem."
from pathlib import Path

from agent_context import daemon


def test_gate_env_points_tmpdir_at_the_state_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(daemon, "_state_dir", lambda: tmp_path)
    monkeypatch.setenv("TMPDIR", "/somewhere/else")
    env = daemon._gate_env()
    assert env["TMPDIR"] == str(tmp_path / "gate-tmp")
    assert (tmp_path / "gate-tmp").is_dir()


def test_gate_env_keeps_the_inherited_tmpdir_when_the_dir_cannot_be_made(
        monkeypatch, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(daemon, "_state_dir", lambda: blocker)
    monkeypatch.setenv("TMPDIR", "/somewhere/else")
    assert daemon._gate_env()["TMPDIR"] == "/somewhere/else"
