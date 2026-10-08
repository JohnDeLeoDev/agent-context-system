'This is the function behind `code_current` and the `stale-code` verdict, and it was\nadded because get_health measured SYNC only: a machine whose deploy gate had been\nfailing for two days reported `healthy` while running 19-hour-old code, twice, and\nboth times a human found it by reading version numbers by hand.'
from agent_context import daemon


def test_not_newer_is_current(monkeypatch):
    monkeypatch.setattr(daemon, "_code_version", lambda: 100.0)
    assert daemon.code_currency({"code_version": 100.0}) is True
    assert daemon.code_currency({"code_version": 120.0}) is True   


def test_newer_on_disk_with_different_bytes_is_stale(monkeypatch):
    'A real release: newer mtime AND different content.'
    monkeypatch.setattr(daemon, "_code_version", lambda: 200.0)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "bbbb")
    assert daemon.code_currency({"code_version": 100.0, "code_fingerprint": "aaaa"}) is False


def test_newer_mtime_over_identical_bytes_is_NOT_stale(monkeypatch):
    'test newer mtime over identical bytes is NOT stale.'
    monkeypatch.setattr(daemon, "_code_version", lambda: 200.0)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "same")
    assert daemon.code_currency({"code_version": 100.0, "code_fingerprint": "same"}) is True


def test_a_daemon_that_recorded_no_fingerprint_falls_back_to_mtime(monkeypatch):
    'Pre-fingerprint daemons (older builds) have no code_fingerprint in\n    daemon.info. They keep the old mtime-only behavior rather than being judged\n    against a comparison they cannot make.'
    monkeypatch.setattr(daemon, "_code_version", lambda: 200.0)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "bbbb")
    assert daemon.code_currency({"code_version": 100.0}) is False


def test_unreadable_fingerprint_on_disk_does_not_claim_current(monkeypatch):
    '_code_fingerprint returning None means we could not read the tree. That is\n    not evidence of sameness, so the newer mtime stands and it reports stale.'
    monkeypatch.setattr(daemon, "_code_version", lambda: 200.0)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: None)
    assert daemon.code_currency({"code_version": 100.0, "code_fingerprint": "aaaa"}) is False


def test_undecidable_is_None_not_False(monkeypatch):
    'None and False mean different things downstream: False drives the stale-code\n    verdict and the fleet-view problem line, None must drive neither. Guessing here\n    would page a human about a machine we know nothing about.'
    monkeypatch.setattr(daemon, "_code_version", lambda: None)
    assert daemon.code_currency({"code_version": 100.0}) is None
    monkeypatch.setattr(daemon, "_code_version", lambda: 100.0)
    assert daemon.code_currency({}) is None


def test_get_health_reports_it_and_the_verdict_follows(monkeypatch):
    "The wiring, not just the helper: a stale daemon must actually surface as\n    verdict 'stale-code' rather than 'healthy'."
    import time
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: "")
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"pid": 1, "code_version": 100.0, "started_at": 1.0,
                                 "code_fingerprint": "aaaa"})
    monkeypatch.setattr(daemon, "_code_version", lambda: 200.0)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "bbbb")
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", time.time())
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", time.time())
    monkeypatch.setattr(daemon, "_LAST_SYNC_ERROR", None)
    h = daemon.get_health()
    assert h["code_current"] is False
    assert h["verdict"] == "stale-code"
    assert h["stalled"] is False          
