'C9b: /relay-source also ships server/uv.lock, so the installer can pin the relay to the\ndependency versions the fleet daemon runs (`uv tool install` ignores uv.lock on its own).'
import io
import os
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from agent_context.relay_source import build_relay_source

REAL_SERVER = Path(__file__).resolve().parents[1]


def _names(body: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        return tar.getnames()


def _tiny_server(root: Path, *, lock: bool) -> Path:
    server = root / "server"
    (server / "src" / "agent_context").mkdir(parents=True)
    (server / "pyproject.toml").write_text('[project]\nname = "agent-context"\n')
    (server / "src" / "agent_context" / "__init__.py").write_text("")
    if lock:
        (server / "uv.lock").write_text("version = 1\n")
    return server


def test_uv_lock_is_in_the_tarball(tmp_path: Path) -> None:
    assert "uv.lock" in _names(build_relay_source(_tiny_server(tmp_path, lock=True)))


def test_a_tree_without_a_lock_still_builds(tmp_path: Path) -> None:
    names = _names(build_relay_source(_tiny_server(tmp_path, lock=False)))
    assert "pyproject.toml" in names
    assert "uv.lock" not in names


def test_the_real_tree_ships_its_lock() -> None:
    assert "uv.lock" in _names(build_relay_source(REAL_SERVER))


def test_the_lock_bytes_are_the_files_bytes(tmp_path: Path) -> None:
    server = _tiny_server(tmp_path, lock=True)
    body = build_relay_source(server)
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        member = tar.extractfile("uv.lock")
        assert member is not None
        assert member.read() == (server / "uv.lock").read_bytes()
