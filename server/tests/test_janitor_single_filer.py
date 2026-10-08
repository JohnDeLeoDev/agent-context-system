"One machine files the janitor's fleet-wide observations, not every machine."
import json
import os
import time

from agent_context import fleet, janitor


def _row(root, uuid, age_secs=0, now=None):
    t = (time.time() if now is None else now) - age_secs
    p = root / "machines" / uuid / "daemon-status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"machine_id": uuid, "verdict": "healthy",
                             "updated_at": int(t)}))


def test_only_the_lowest_fresh_uuid_is_the_filer(tmp_path):
    now = time.time()
    for u in ("u-b", "u-a", "u-c"):
        _row(tmp_path, u, now=now)
    assert janitor.is_fleet_filer(tmp_path, "u-a", now=now) is True
    assert janitor.is_fleet_filer(tmp_path, "u-b", now=now) is False
    assert janitor.is_fleet_filer(tmp_path, "u-c", now=now) is False


def test_a_stale_lowest_uuid_hands_filing_to_the_next_fresh_one(tmp_path):
    now = time.time()
    _row(tmp_path, "u-a", age_secs=fleet.STALE_SECS + 60, now=now)
    _row(tmp_path, "u-b", now=now)
    _row(tmp_path, "u-c", now=now)
    assert janitor.is_fleet_filer(tmp_path, "u-a", now=now) is False
    assert janitor.is_fleet_filer(tmp_path, "u-b", now=now) is True
    assert janitor.is_fleet_filer(tmp_path, "u-c", now=now) is False


def test_with_no_readable_rows_this_machine_files(tmp_path):
    
    
    assert janitor.is_fleet_filer(tmp_path, "u-z") is True
    (tmp_path / "machines").mkdir()
    assert janitor.is_fleet_filer(tmp_path, "u-z") is True


def test_a_non_filer_skips_store_filing_and_keeps_its_due_stamps(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(janitor, "sweep_eval_gaps",
                        lambda root, store: ran.append("eval_gaps") or {"ran": True})
    monkeypatch.setattr(janitor, "watch_bootstrap_footprint",
                        lambda root, store: ran.append("footprint") or {"ran": True})
    monkeypatch.setattr(janitor, "refresh_invariant_health", lambda root: None)
    root = tmp_path / "r"
    root.mkdir()
    os.system(f"git -C {root} init -q -b main")
    now = time.time()
    _row(root, "u-a", now=now)
    _row(root, "u-b", now=now)

    out = janitor.sweep(str(root), now=now, store=object(), machine_uuid="u-b")
    assert "worktrees" in out and "litter" in out           
    assert not {"eval_gaps", "footprint"} & set(out)
    assert ran == []

    
    
    _row(root, "u-a", age_secs=fleet.STALE_SECS + 60, now=now + 60)
    out = janitor.sweep(str(root), now=now + 60, store=object(), machine_uuid="u-b")
    assert {"eval_gaps", "footprint"} <= set(out)
