'Two guards, tested here. Every case runs on a throwaway repo under tmp_path.'
import subprocess

import pytest
from fixture_signing import signing_config

from agent_context import paths
from agent_context.store import ContextStore


def _git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=check)


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "ctx"
    (root / "global" / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 *signing_config(tmp_path)):
        _git(root, "config", k, v)
    (root / "global" / "scripts" / "materialize.sh").write_text("#!/bin/sh\necho hi\n")
    (root / "global" / "note.md").write_text("a memory\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return ContextStore(root=str(root)), root


def test_truncated_tracked_file_blocks_the_whole_sync(store):
    'test truncated tracked file blocks the whole sync.'
    s, root = store
    victim = root / "global" / "scripts" / "materialize.sh"
    victim.write_text("")                       

    res = s.sync(push=False)

    assert res.get("commit_skipped"), "sync should refuse, not commit the wreckage"
    assert res["truncated"] == ["global/scripts/materialize.sh"]
    assert not res.get("committed")
    
    
    
    from agent_context import server
    assert res.get("truncated_hold") == ["global/scripts/materialize.sh"]
    assert server.sync_verdict(res) is not None, \
        "a refused sync must not be recorded as a healthy one"
    
    assert _git(root, "show", "HEAD:global/scripts/materialize.sh").stdout.strip()
    assert _git(root, "log", "--oneline").stdout.count("\n") == 1
    
    assert victim.read_text() == ""


def test_ordinary_edits_still_commit(store):
    'The guard must not fire on real work, or it gets switched off.'
    s, root = store
    
    paths.write_atomic(root / "global" / "note.md", "a memory, revised\n")
    paths.write_atomic(root / "global" / "new.md", "brand new\n")

    res = s.sync(push=False)

    assert res.get("committed") is True
    assert "commit_skipped" not in res
    assert _git(root, "show", "HEAD:global/new.md").stdout == "brand new\n"


def test_a_deletion_is_not_a_truncation(store):
    'Removing a file is legitimate; only non-empty -> 0-byte is wreckage.'
    s, root = store
    (root / "global" / "note.md").unlink()
    s._arm_commit(str(root / "global" / "note.md"))      

    res = s.sync(push=False)

    assert res.get("committed") is True
    assert "commit_skipped" not in res
    assert _git(root, "cat-file", "-e", "HEAD:global/note.md", check=False).returncode != 0


def test_a_newly_added_empty_file_is_not_wreckage(store):
    'An empty file that was never committed non-empty has no truncation to report.'
    s, root = store
    (root / "global" / "placeholder.md").write_text("")

    res = s.sync(push=False)

    assert "commit_skipped" not in res


def test_a_file_committed_empty_stays_allowed(store):
    'HEAD already holds 0 bytes, so the worktree copy is not a regression.'
    s, root = store
    (root / "global" / "empty.md").write_text("")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "an intentionally empty file")

    paths.write_atomic(root / "global" / "note.md", "changed\n")
    res = s.sync(push=False)

    assert res.get("committed") is True
    assert "commit_skipped" not in res


def test_truncation_is_detected_before_anything_is_staged(store):
    'The refusal must cost nothing to undo — no `git reset` in the recovery path.'
    s, root = store
    (root / "global" / "scripts" / "materialize.sh").write_text("")

    s.sync(push=False)

    assert not _git(root, "diff", "--cached", "--name-only").stdout.strip()


def test_failed_autostash_is_flagged_apart_from_a_conflict(monkeypatch, store):
    '`Cannot autostash` is an environment fault, not two machines disagreeing.\n\n    They need different answers — one wants a human to reconcile content, the other\n    wants somebody to find out why `git stash` will not run on that host — so the\n    result has to tell them apart.'
    s, _root = store
    real = s._git

    class R:
        returncode = 1
        stdout = ""
        stderr = "fatal: Cannot autostash\n"

    def fake(*args, **kw):
        if args and args[0] == "merge" and "--abort" not in args:
            return R()
        return real(*args, **kw)

    monkeypatch.setattr(s, "_git", fake)
    monkeypatch.setattr(s, "_remote_mains", lambda b: (["origin"], ["origin/main"]))

    res = s.sync(push=False)

    assert res.get("pull") is False
    assert res.get("autostash_failed") is True
