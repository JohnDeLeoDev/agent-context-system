'`since` is protected inside flush() by a min(), so no local writer can move the\nwindow forward. That protection stopped at the publish boundary, which is where it\nmattered most: one moment with the local file missing resets `since` to now, and\nthe next publish copied that fresh start over a store snapshot that remembered\nweeks of counting. Every machine in the fleet reported a ~0.25-day window as a\nresult, so `enough_evidence` was permanently false and cold_always_loaded could\nnever fire.'
import json
import time

from agent_context import usage


def _read(p):
    return json.loads(p.read_text())


def test_publish_keeps_the_earlier_window_start(tmp_path, monkeypatch):
    store = tmp_path / "store"
    uid = "test-machine"
    snap = usage.snapshot_path(store, uid)
    snap.parent.mkdir(parents=True)

    old_since = time.time() - 30 * 86400          
    snap.write_text(json.dumps({"since": old_since, "e": {}}))

    
    monkeypatch.setattr(usage, "_data", {"since": time.time(), "e": {"k": {"r": 1, "h": 0, "t": 0}}})
    monkeypatch.setattr(usage, "_last_publish", 0.0)

    assert usage.publish_snapshot(store, uid, force=True) is True
    assert _read(snap)["since"] == old_since, "a reset local window overwrote the store's history"


def test_publish_still_advances_counts_while_holding_the_window(tmp_path, monkeypatch):
    store = tmp_path / "store"
    uid = "test-machine"
    snap = usage.snapshot_path(store, uid)
    snap.parent.mkdir(parents=True)
    old_since = time.time() - 10 * 86400
    snap.write_text(json.dumps({"since": old_since, "e": {"k": {"r": 1, "h": 0, "t": 0}}}))

    monkeypatch.setattr(usage, "_data", {"since": time.time(), "e": {"k": {"r": 7, "h": 2, "t": 5}}})
    monkeypatch.setattr(usage, "_last_publish", 0.0)
    assert usage.publish_snapshot(store, uid, force=True) is True

    out = _read(snap)
    assert out["since"] == old_since
    assert out["e"]["k"]["r"] == 7, "counts should be this machine's current totals"


def test_publish_uses_local_since_when_it_is_the_earlier_one(tmp_path, monkeypatch):
    "The guard is a min(), not 'always prefer the store' — a machine that has been\n    counting longer than its last published snapshot must still widen the window."
    store = tmp_path / "store"
    uid = "test-machine"
    snap = usage.snapshot_path(store, uid)
    snap.parent.mkdir(parents=True)
    snap.write_text(json.dumps({"since": time.time() - 86400, "e": {}}))

    local_since = time.time() - 40 * 86400
    monkeypatch.setattr(usage, "_data", {"since": local_since, "e": {"k": {"r": 1, "h": 0, "t": 0}}})
    monkeypatch.setattr(usage, "_last_publish", 0.0)
    assert usage.publish_snapshot(store, uid, force=True) is True
    assert _read(snap)["since"] == local_since


def test_publish_survives_a_corrupt_prior_snapshot(tmp_path, monkeypatch):
    "Fail-silent is the module's contract: a garbage snapshot must not stop a\n    publish, it just cannot contribute a window start."
    store = tmp_path / "store"
    uid = "test-machine"
    snap = usage.snapshot_path(store, uid)
    snap.parent.mkdir(parents=True)
    snap.write_text("{not json at all")

    local_since = time.time() - 5 * 86400
    monkeypatch.setattr(usage, "_data", {"since": local_since, "e": {}})
    monkeypatch.setattr(usage, "_last_publish", 0.0)
    assert usage.publish_snapshot(store, uid, force=True) is True
    assert _read(snap)["since"] == local_since
