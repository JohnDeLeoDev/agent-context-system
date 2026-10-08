'The local-corruption verdict must not blame this machine for a broken mirror.'
import subprocess
from types import SimpleNamespace

from agent_context import server
from agent_context.store import ContextStore

REMOTES = ["origin", "ls", "s1", "s2"]


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    root = str(tmp_path)

    def _git(*args, check=True, timeout=None):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                              check=check)
    return SimpleNamespace(root=root, _git=_git)


def test_git_temp_write_files_are_not_empty_objects(tmp_path):
    fake = _repo(tmp_path)
    d = tmp_path / ".git" / "objects" / "69"
    d.mkdir(parents=True)
    (d / "tmp_obj_CmC1LR").write_bytes(b"")
    assert ContextStore._empty_loose_objects(fake) == []


def test_one_mirror_refusing_does_not_blame_local_objects():
    errs = {"ls": " ! [remote rejected] main (missing necessary objects)\n"
                  "error: remote unpack failed: unpacker error"}
    assert ContextStore._corruption_suspected(errs, REMOTES) is False


def test_every_mirror_refusing_with_unpacker_error_blames_local_objects():
    errs = {r: " ! [remote rejected] main (unpacker error)" for r in REMOTES}
    assert ContextStore._corruption_suspected(errs, REMOTES) is True


def test_every_mirror_refusing_for_another_reason_does_not():
    errs = {r: " ! [rejected] main (non-fast-forward)" for r in REMOTES}
    assert ContextStore._corruption_suspected(errs, REMOTES) is False


def test_corruption_reason_quotes_the_mirrors_own_error():
    res = {"push": False, "pull": True, "push_fail_streak": 1,
           "push_error": {"ls": " ! [remote rejected] main (missing necessary objects)"},
           "local_corruption": {"count": 1, "empty_objects": ["5e/248b7a"]}}
    reason = server.sync_verdict(res)
    assert "missing necessary objects" in reason
