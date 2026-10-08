"preflight-core-health's generic_findings() reads every ~/.claude/state/health/*.json and\nreports any `ok: false`, so the daemon only has to write one."
import json

import pytest
from narrow import notnone

from agent_context import daemon


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    
    
    
    
    monkeypatch.setattr(daemon, "_UNHEALTHY_STREAK", 0)
    monkeypatch.setattr(daemon, "_UNHEALTHY_NOTIFIED_AT", 0.0)
    monkeypatch.setattr(daemon, "_notify", lambda *a, **k: None)
    return tmp_path


def _verdict(_unused=None):
    p = daemon._health_dir() / "store-sync.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_healthy_cycle_writes_an_ok_verdict(_isolated_home):
    daemon.note_sync_health(None)
    rec = notnone(_verdict(_isolated_home))
    assert rec["ok"] is True
    assert rec["component"] == "agent-context sync"
    assert rec["failures"] == []


def test_a_short_fault_is_not_yet_degraded(_isolated_home):
    'An ordinary edit-then-release window must not open every session with an alarm.'
    daemon.note_sync_health("sync deferred 1 consecutive cycles: uncommitted server/ edits")
    assert notnone(_verdict(_isolated_home))["ok"] is True


def test_a_persistent_fault_becomes_a_degraded_finding(_isolated_home):
    'Same threshold that pages: worth waking someone for is worth telling the next\n    session.'
    for _ in range(daemon._UNHEALTHY_NOTIFY_CYCLES):
        daemon.note_sync_health("sync deferred: uncommitted server/ edits (policy)")
    rec = notnone(_verdict(_isolated_home))
    assert rec["ok"] is False
    assert len(rec["failures"]) == 1
    
    
    assert "STALE" in rec["failures"][0]
    assert "policy" in rec["failures"][0]


def test_the_verdict_is_written_between_pages(_isolated_home):
    'The page is throttled to 6-hourly; a session starting in the gap would see\n    nothing if the verdict were only written when the alert fires.'
    for _ in range(daemon._UNHEALTHY_NOTIFY_CYCLES + 4):
        daemon.note_sync_health("integration failed — the merge conflicted")
    assert notnone(_verdict(_isolated_home))["ok"] is False
    assert daemon._UNHEALTHY_STREAK == daemon._UNHEALTHY_NOTIFY_CYCLES + 4


def test_recovery_clears_the_finding(_isolated_home):
    for _ in range(daemon._UNHEALTHY_NOTIFY_CYCLES):
        daemon.note_sync_health("integration failed — the merge conflicted")
    assert notnone(_verdict(_isolated_home))["ok"] is False
    daemon.note_sync_health(None)
    assert notnone(_verdict(_isolated_home))["ok"] is True


def test_an_unwritable_home_never_breaks_the_loop(monkeypatch):
    'The store must keep syncing on a machine with no ~/.claude at all — a probe\n    that can raise takes down the loop it exists to report on.'
    monkeypatch.setattr(daemon.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    daemon._write_sync_health_verdict("anything", 99)      


def test_the_reason_reads_as_a_sentence(_isolated_home):
    "Reason strings are fragments with no trailing punctuation, so concatenating\n    them produced 'cycles -- stuck again Until it clears'."
    for _ in range(daemon._UNHEALTHY_NOTIFY_CYCLES):
        daemon.note_sync_health("integration failed — the merge conflicted")
    detail = notnone(_verdict())["failures"][0]
    assert "conflicted. Until it clears" in detail


def test_the_verdict_path_is_the_patchable_one(monkeypatch, tmp_path):
    'THE REGRESSION GUARD. _write_sync_health_verdict must resolve its directory\n    through paths.health_dir and nothing else.\n\n    The first version called os.path.expanduser("~") inline, which no fixture can\n    patch. The existing note_sync_health tests then wrote their own fixture string\n    ("stuck again") into this machine\'s live DEGRADED CORE SYSTEMS input, and since\n    the deploy gate runs this suite on every machine on every release, every machine\n    re-poisoned itself. A test that isolates by setting HOME would still pass on the\n    broken version -- so this one asserts the SEAM, not the outcome.'
    sentinel = tmp_path / "sentinel-health"
    monkeypatch.setattr(daemon, "_health_dir", lambda: sentinel)
    daemon._write_sync_health_verdict("something is wrong", 99)
    assert (sentinel / "store-sync.json").exists()
