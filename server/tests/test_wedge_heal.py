'Every case runs on a throwaway git repo under tmp_path; the live store is never touched.'
import os
import subprocess
import time

import pytest
from fixture_signing import signing_config

from agent_context import daemon


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    
    
    
    
    
    
    for k, v in signing_config(tmp_path):
        _git(root, "config", k, v)
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "one")
    return root


def _age(path, secs):
    old = time.time() - secs
    os.utime(path, (old, old))


def test_stale_lock_with_no_live_git_is_removed(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    _age(lock, 1200)
    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 0)
    acts = daemon.heal_working_tree(str(repo), lock_age_secs=600)
    assert not lock.exists()
    assert any("index.lock" in a for a in acts)
    assert daemon._working_tree_wedge(str(repo), lock_age_secs=600) == ""


def _bare(tmp_path, name):
    p = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(p)], check=True)
    return p


def test_stale_ref_lock_is_removed_and_unblocks_fetch(repo, tmp_path, monkeypatch):
    'test stale ref lock is removed and unblocks fetch.'
    up = _bare(tmp_path, "up.git")
    _git(repo, "remote", "add", "up", str(up))
    _git(repo, "push", "-q", "up", "main")

    
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(up), str(other)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 *signing_config(tmp_path)):
        _git(other, "config", k, v)
    (other / "b.txt").write_text("two\n")
    _git(other, "add", "b.txt")
    _git(other, "commit", "-q", "-m", "two")
    _git(other, "push", "-q", "origin", "main")

    lock = repo / ".git" / "refs" / "remotes" / "up" / "main.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    _age(lock, 1200)

    blocked = subprocess.run(["git", "-C", str(repo), "fetch", "up"],
                             capture_output=True, text=True)
    assert blocked.returncode != 0, "the lock must actually break fetch, or this proves nothing"
    
    
    
    
    
    
    
    assert "refs/remotes/up/main" in blocked.stderr + blocked.stdout

    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 0)
    acts = daemon.heal_working_tree(str(repo), lock_age_secs=600)
    assert any("main.lock" in a for a in acts)
    assert not lock.exists()

    healed = subprocess.run(["git", "-C", str(repo), "fetch", "up"],
                            capture_output=True, text=True)
    assert healed.returncode == 0


def test_fresh_ref_lock_is_left_alone(repo, monkeypatch):
    'Same age rule as index.lock: a young lock may still have a live owner.'
    lock = repo / ".git" / "refs" / "remotes" / "up" / "main.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 0)
    assert daemon.heal_working_tree(str(repo), lock_age_secs=600) == []
    assert lock.exists()


def test_stale_ref_lock_with_a_live_git_is_left_alone(repo, monkeypatch):
    'A git that has run at least as long as the lock could be its owner.'
    lock = repo / ".git" / "refs" / "remotes" / "up" / "main.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    _age(lock, 1200)
    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 3000)
    assert daemon.heal_working_tree(str(repo), lock_age_secs=600) == []
    assert lock.exists()
    assert "main.lock" in daemon._working_tree_wedge(str(repo), lock_age_secs=600)


def test_fresh_lock_is_left_alone(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 0)
    assert daemon.heal_working_tree(str(repo), lock_age_secs=600) == []
    assert lock.exists()


def test_lock_held_by_long_running_git_is_left_alone(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    _age(lock, 1200)
    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 5000)  
    assert daemon.heal_working_tree(str(repo), lock_age_secs=600) == []
    assert lock.exists()


def test_empty_cherry_pick_is_quit(repo, monkeypatch):
    
    
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("two\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "two")
    side = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "main")
    (repo / "b.txt").write_text("two\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "two again")
    r = subprocess.run(["git", "-C", str(repo), "cherry-pick", side], capture_output=True, text=True)
    assert r.returncode != 0
    assert (repo / ".git" / "CHERRY_PICK_HEAD").exists()
    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 0)
    acts = daemon.heal_working_tree(str(repo))
    assert any("cherry-pick" in a for a in acts)
    assert not (repo / ".git" / "CHERRY_PICK_HEAD").exists()
    assert daemon._working_tree_wedge(str(repo)) == ""


def test_conflicted_cherry_pick_is_left_for_a_human(repo, monkeypatch):
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "a.txt").write_text("side\n")
    _git(repo, "commit", "-q", "-am", "side edit")
    side = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("main\n")
    _git(repo, "commit", "-q", "-am", "main edit")
    r = subprocess.run(["git", "-C", str(repo), "cherry-pick", side], capture_output=True, text=True)
    assert r.returncode != 0
    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 0)
    assert daemon.heal_working_tree(str(repo)) == []
    assert "cherry-pick" in daemon._working_tree_wedge(str(repo))


def test_interrupted_rebase_is_aborted(repo, monkeypatch):
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "a.txt").write_text("side\n")
    _git(repo, "commit", "-q", "-am", "side edit")
    _git(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("main\n")
    _git(repo, "commit", "-q", "-am", "main edit")
    _git(repo, "checkout", "-q", "side")
    r = subprocess.run(["git", "-C", str(repo), "rebase", "main"], capture_output=True, text=True)
    assert r.returncode != 0  
    assert "rebase" in daemon._working_tree_wedge(str(repo))
    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 0)
    acts = daemon.heal_working_tree(str(repo))
    assert any("rebase" in a for a in acts)
    assert daemon._working_tree_wedge(str(repo)) == ""
    assert _git(repo, "log", "--oneline").stdout.count("\n") == 2  


def test_no_git_dir_is_a_noop(tmp_path):
    assert daemon.heal_working_tree(str(tmp_path)) == []


def test_live_git_secs_parses_etime(monkeypatch):
    class R:  
        stdout = "   05:12 git fetch --all\n1-02:03:04 /usr/bin/git push origin\n   00:01 python3 x\n"
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: R())
    assert daemon._live_git_secs("/x") == 86400 + 2 * 3600 + 3 * 60 + 4



def test_rebase_husk_with_only_an_autostash_is_removed_and_named(tmp_path, monkeypatch):
    'test rebase husk with only an autostash is removed and named.'
    import os
    import subprocess

    from agent_context import daemon
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    husk = repo / ".git" / "rebase-merge"
    husk.mkdir()
    (husk / "autostash").write_text("0123456789abcdef0123456789abcdef01234567\n")
    monkeypatch.setattr(daemon, "_live_git_secs", lambda root: 0)
    actions = daemon.heal_working_tree(str(repo))
    assert not husk.exists()
    assert any("0123456789abcdef" in a and "stash apply" in a for a in actions)
