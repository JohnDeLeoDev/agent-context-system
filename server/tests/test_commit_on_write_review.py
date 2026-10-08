'Regressions found by the adversarial review of commit-on-write.\n\nFixtures and helpers come from test_commit_on_write.py.'
import threading

import pytest
from test_commit_on_write import _count, _doc, _git, _remote_tip, cow, repo  

from agent_context import projects


def test_delete_project_arms_the_timer(repo, cow):  
    store, _, _ = repo
    projects.upsert_project(store, "git@example.com:o/r.git", "proj")
    cow.clock.advance(3.1)
    cow.tick()
    assert cow.status()["pending"] is False
    projects.delete_project(store, "proj")
    assert cow.status()["pending"] is True


@pytest.mark.parametrize("marker", ["rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD",
                                    "REBASE_HEAD"])
def test_unfinished_git_operation_defers_the_commit(repo, cow, marker):  
    store, root, _ = repo
    target = root / ".git" / marker
    if marker.startswith("rebase-"):
        target.mkdir()
    else:
        target.write_text(_git(root, "rev-parse", "HEAD").stdout)
    before = _count(root)
    _doc(store, "r.md")
    cow.clock.advance(3.1)
    cow.tick()
    assert _count(root) == before
    assert marker in (cow.status()["last_commit_error"] or "")


def test_detached_head_never_pushes(repo, cow):  
    store, root, mirrors = repo
    _git(root, "checkout", "-q", "--detach")
    tip = _remote_tip(mirrors["origin"])
    _doc(store, "dh.md")
    cow.clock.advance(3.1)
    cow.tick()
    assert _remote_tip(mirrors["origin"]) == tip
    heads = _git(mirrors["origin"], "branch", "--list").stdout.split()
    assert "HEAD" not in heads
    assert cow.status()["last_push_error"]


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "banana"])
def test_non_finite_debounce_falls_back_to_default(repo, monkeypatch, value):  
    store, _, _ = repo
    monkeypatch.setenv("AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS", value)
    c = store.enable_commit_on_write(start=False)
    assert c is not None and c.debounce == 3.0


def test_a_slow_signer_does_not_hold_the_store_lock(repo, cow, monkeypatch):  
    store, _root, _ = repo
    _doc(store, "slow.md")
    cow.clock.advance(3.1)
    in_commit, release = threading.Event(), threading.Event()
    real = store._commit_staged

    def slow(message, timeout=None):
        in_commit.set()
        release.wait(30)
        return real(message, timeout)

    monkeypatch.setattr(store, "_commit_staged", slow)
    t = threading.Thread(target=cow.tick_commit)
    t.start()
    try:
        assert in_commit.wait(10), "the commit never started"
        done = threading.Event()
        w = threading.Thread(target=lambda: (_doc(store, "during.md"), done.set()))
        w.start()
        assert done.wait(3), "a write blocked while the signing commit ran"
    finally:
        release.set()
        t.join(30)
    assert cow.status()["pending"] is True     


def test_dead_mirror_retry_backs_off(repo, cow, tmp_path):  
    store, _root, mirrors = repo
    mirrors["m1"].rename(tmp_path / "m1.gone")
    _doc(store, "bo.md")
    cow.clock.advance(3.1)
    cow.tick()
    calls = []
    real = store.push_all
    store.push_all = lambda: (calls.append(1), real())[1]
    for _ in range(4):                          
        cow.clock.advance(61)
        cow.tick()
    assert len(calls) < 4
