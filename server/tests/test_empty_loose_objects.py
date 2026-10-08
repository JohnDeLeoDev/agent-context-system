'ContextStore._empty_loose_objects: the cheap scan that names a corrupt object store.'
import os
import subprocess
from types import SimpleNamespace

from agent_context.store import ContextStore


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    root = str(tmp_path)

    def _git(*args, check=True, timeout=None):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                              check=check)
    return SimpleNamespace(root=root, _git=_git)


def test_clean_repo_has_no_empty_objects(tmp_path):
    fake = _repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    fake._git("add", "a.txt")
    assert ContextStore._empty_loose_objects(fake) == []


def test_zero_byte_object_is_named(tmp_path):
    fake = _repo(tmp_path)
    d = tmp_path / ".git" / "objects" / "5e"
    d.mkdir(parents=True)
    (d / "248b7a7fabbbdab7be794e60cd0412bce99861").write_bytes(b"")
    assert ContextStore._empty_loose_objects(fake) == [
        "5e/248b7a7fabbbdab7be794e60cd0412bce99861"]


def test_not_a_repo_is_empty_not_an_error(tmp_path):
    root = str(tmp_path)
    fake = SimpleNamespace(root=root, _git=lambda *a, **k: subprocess.run(
        ["git", "-C", root, *a], capture_output=True, text=True))
    os.makedirs(root, exist_ok=True)
    assert ContextStore._empty_loose_objects(fake) == []
