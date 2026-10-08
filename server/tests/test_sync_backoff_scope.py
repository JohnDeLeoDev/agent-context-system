'test sync backoff scope.'
from agent_context import daemon


def test_network_failure_still_backs_off():
    'test network failure still backs off.'
    over = daemon._BACKOFF_AFTER_CYCLES + 2
    assert daemon.sync_retry_delay(300, streak=over, network=True) > 300


def test_local_only_failure_holds_the_base_interval():
    'A dirty server/ tree never reaches a remote, however long it persists.'
    for streak in (daemon._BACKOFF_AFTER_CYCLES, 19, 200):
        assert daemon.sync_retry_delay(300, streak=streak, network=False) == 300


def test_backoff_is_still_capped_and_healthy_stays_at_base():
    assert daemon.sync_retry_delay(300, streak=0, network=True) == 300
    assert daemon.sync_retry_delay(300, streak=99, network=True) == daemon._BACKOFF_MAX_SECS


def test_network_defaults_true_so_a_raising_cycle_is_not_treated_as_free():
    'A cycle that raised may have raised AFTER the fetch. Assuming otherwise would\n    restore the request storm this exists to prevent.'
    over = daemon._BACKOFF_AFTER_CYCLES + 2
    assert daemon.sync_retry_delay(300, streak=over) > 300


def test_a_deferred_cycle_reports_itself_as_local_only(tmp_path, monkeypatch):
    'End to end through sync(): the deferral path must not claim network work.\n\n    Pinning this in sync() rather than only in the delay function is deliberate --\n    the flag is set at the one point past every pre-network guard, and a future guard\n    added after it would silently mark its own bail-out as quota-spending.'
    import subprocess

    from agent_context.store import ContextStore

    root = tmp_path / "store"
    (root / "server").mkdir(parents=True)
    subprocess.run(("git", "-C", str(root), "init", "-q", "-b", "main"), check=True)
    subprocess.run(("git", "-C", str(root), "config", "user.email", "t@t"), check=True)
    subprocess.run(("git", "-C", str(root), "config", "user.name", "t"), check=True)
    (root / "seed.txt").write_text("seed\n")
    subprocess.run(("git", "-C", str(root), "add", "-A"), check=True)
    subprocess.run(("git", "-C", str(root), "commit", "-qm", "seed"), check=True)
    
    (root / "server" / "code.py").write_text("x = 1\n")

    res = ContextStore(str(root)).sync()
    assert res.get("pull_skipped"), res
    
    
    
    assert "network" not in res, res
    assert daemon.sync_retry_delay(300, streak=19,
                                   network=bool(res.get("network", False))) == 300


def test_a_full_cycle_marks_itself_as_network(tmp_path):
    'test a full cycle marks itself as network.'
    import subprocess

    from agent_context.store import ContextStore

    root = tmp_path / "clean"
    (root / "server").mkdir(parents=True)
    subprocess.run(("git", "-C", str(root), "init", "-q", "-b", "main"), check=True)
    subprocess.run(("git", "-C", str(root), "config", "user.email", "t@t"), check=True)
    subprocess.run(("git", "-C", str(root), "config", "user.name", "t"), check=True)
    (root / "seed.txt").write_text("seed\n")
    subprocess.run(("git", "-C", str(root), "add", "-A"), check=True)
    subprocess.run(("git", "-C", str(root), "commit", "-qm", "seed"), check=True)

    res = ContextStore(str(root)).sync()
    assert res.get("network") is True, res
