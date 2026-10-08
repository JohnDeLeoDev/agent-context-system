'test sync divergence.'
import subprocess

import pytest

from agent_context import server
from agent_context.store import ContextStore


def _git(cwd, *args):
    return subprocess.run(("git", "-C", str(cwd), *args),
                          capture_output=True, text=True, check=True)


@pytest.fixture
def diverged(tmp_path):
    'A store whose `origin` holds two commits HEAD does not.'
    upstream, local = tmp_path / "upstream", tmp_path / "local"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@t")
    _git(upstream, "config", "user.name", "t")
    (upstream / "seed.txt").write_text("0\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "seed")
    _git(tmp_path, "clone", "-q", str(upstream), str(local))
    _git(local, "config", "user.email", "t@t")
    _git(local, "config", "user.name", "t")
    for n in (1, 2):
        (upstream / f"{n}.txt").write_text(f"{n}\n")
        _git(upstream, "add", "-A")
        _git(upstream, "commit", "-qm", f"upstream {n}")
    _git(local, "fetch", "-q", "origin")
    return local


def test_divergence_measures_what_sync_did_not_report(diverged):
    div = ContextStore(str(diverged)).divergence()
    assert div["origin/main"] == {"ahead": 0, "behind": 2}


def test_each_mirror_is_counted_once(diverged):
    'refs/remotes/origin/HEAD shortens to plain `origin`, so filtering the SHORT\n    name for "/HEAD" leaves the symref in and reports one mirror as two.'
    _git(diverged, "remote", "set-head", "origin", "main")
    div = ContextStore(str(diverged)).divergence()
    assert set(div) == {"origin/main"}, div


def test_divergence_is_empty_when_converged(tmp_path):
    'A repo with no remotes measures {} — converged, not unknown. The distinction\n    matters because get_health reports absent-vs-empty differently.'
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    assert ContextStore(str(repo)).divergence() == {}


def test_divergence_never_raises_on_a_broken_repo(tmp_path):
    'A health probe that can throw takes down the loop it exists to observe.'
    assert ContextStore(str(tmp_path / "does-not-exist")).divergence() == {}


def test_behind_is_tolerated_for_one_cycle_then_reported():
    'One cycle behind is genuinely ambiguous — another machine can push between our\n    fetch and our measurement. Two is not.'
    div = {"origin/main": {"ahead": 3, "behind": 37}}
    first = {"pull": True, "divergence": div, "behind_streak": 1}
    assert server.sync_verdict(first) is None

    second = {**first, "behind_streak": server._BEHIND_STALL_CYCLES}
    reason = server.sync_verdict(second)
    assert reason is not None
    assert "origin/main by 37" in reason


def test_a_reported_failure_outranks_the_measurement():
    'When sync() DOES name the trouble, the named reason is the more useful message\n    — the measurement is the backstop for the shapes it does not name.'
    reason = server.sync_verdict({
        "pull": False,
        "pull_error": "CONFLICT (add/add): Merge conflict in global/audit-observations/0281.json",
        "base": "ls/main",
        "divergence": {"ls/main": {"ahead": 26, "behind": 37}},
        "behind_streak": 9,
    })
    assert "CONFLICT (add/add)" in reason


def test_fold_divergence_reports_the_gap_before_the_verdict_moves(diverged, monkeypatch):
    'test fold divergence reports the gap before the verdict moves.'
    recorded = {}
    monkeypatch.setattr("agent_context.daemon.record_divergence",
                        lambda d: recorded.update(d or {}))
    res = {}
    streak = server._fold_divergence(ContextStore(str(diverged)), res, 0)

    assert streak == 1
    assert server.sync_verdict({**res, "pull": True}) is None   
    assert res["divergence"]["origin/main"]["behind"] == 2      
    assert recorded["origin/main"]["behind"] == 2


class _StopLoop(Exception):
    'Ends _sync_loop at its first sleep, after the boot pull has run.'


def test_boot_pull_cannot_launder_a_stalled_integration(diverged, monkeypatch):
    'test boot pull cannot launder a stalled integration.'
    calls = []
    monkeypatch.setattr("agent_context.daemon.record_sync_success",
                        lambda *a, **k: calls.append("success"))
    monkeypatch.setattr("agent_context.daemon.record_sync_failure",
                        lambda e, *a, **k: calls.append(f"failure: {e}"))
    monkeypatch.setattr("agent_context.daemon.record_divergence", lambda d: None)
    monkeypatch.setattr(server.time, "sleep", lambda _: (_ for _ in ()).throw(_StopLoop()))

    store = ContextStore(str(diverged))
    
    
    
    monkeypatch.setattr(ContextStore, "sync", lambda self, **kw: {"pull": True})

    with pytest.raises(_StopLoop):
        server._sync_loop(store, interval=300, boot_pull=True)

    assert calls == ["success"], "boot pull is one cycle behind — still ambiguous"

    calls.clear()
    monkeypatch.setattr(server, "_BEHIND_STALL_CYCLES", 1)
    with pytest.raises(_StopLoop):
        server._sync_loop(store, interval=300, boot_pull=True)

    assert len(calls) == 1 and calls[0].startswith("failure:"), calls
    assert "behind 1 mirror(s)" in calls[0]


def test_fold_divergence_clears_the_streak_once_converged(diverged, monkeypatch):
    monkeypatch.setattr("agent_context.daemon.record_divergence", lambda d: None)
    store = ContextStore(str(diverged))
    assert server._fold_divergence(store, {}, 5) == 6
    _git(diverged, "merge", "-q", "origin/main")
    assert server._fold_divergence(store, {}, 6) == 0
