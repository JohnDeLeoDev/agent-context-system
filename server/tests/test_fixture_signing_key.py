'The throwaway signing key must be usable under a home that carries a default ACL.'
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from fixture_signing import signing_config, throwaway_key


def _signed_commit(repo: Path, key_dir: Path) -> subprocess.CompletedProcess:
    'A fixture repo that signs with the throwaway key in `key_dir`.'
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    pairs = (("user.email", "t@t"), ("user.name", "T"), *signing_config(key_dir))
    for key, value in pairs:
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    (repo / "a").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "a"], check=True)
    return subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"],
                          capture_output=True, text=True)


def test_the_key_is_readable_by_its_owner_alone(tmp_path: Path) -> None:
    key = throwaway_key(tmp_path)
    mode = stat.S_IMODE(key.stat().st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, oct(mode)
    assert mode & stat.S_IRUSR


def test_a_signed_commit_works_with_the_generated_key(tmp_path: Path) -> None:
    done = _signed_commit(tmp_path / "repo", tmp_path)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(shutil.which("setfacl") is None, reason="host has no ACL tools")
def test_a_default_acl_on_the_key_directory_does_not_widen_the_key(tmp_path: Path) -> None:
    'test a default acl on the key directory does not widen the key.'
    key_dir = tmp_path / "acl"
    key_dir.mkdir()
    subprocess.run(["setfacl", "-d", "-m", "g::r-x", str(key_dir)], check=True)
    key = throwaway_key(key_dir)
    mode = stat.S_IMODE(key.stat().st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, oct(mode)
    done = _signed_commit(tmp_path / "acl-repo", key_dir)
    assert done.returncode == 0, done.stderr


def test_the_key_is_reused_and_never_regenerated(tmp_path: Path) -> None:
    first = throwaway_key(tmp_path)
    content = first.read_bytes()
    assert throwaway_key(tmp_path) == first
    assert first.read_bytes() == content
