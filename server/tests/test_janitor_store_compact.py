'The daily janitor runs the archive sweep (context overhaul, Phase 1 item 5).\n\nstore-compact.py --apply moves spent audit observations and spent handoffs into their\narchives. It existed for months as a command somebody had to remember, so the live\ndirectories held 300 resolved observations and 60 consumed handoffs. The janitor now\nruns it once a day, on the fleet filer only: two machines moving the same files would\nrace each other through git.'
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_context import janitor

SCRIPT = Path(__file__).resolve().parents[2] / "global" / "scripts" / "store-compact.py"


def _quiet(monkeypatch):
    'Every other janitor job becomes a no-op, so only the sweep under test runs.'
    for name in ("sweep_worktrees", "sweep_branches"):
        monkeypatch.setattr(janitor, name, lambda root, t: {})
    monkeypatch.setattr(janitor, "sweep_litter", lambda root, t: [])
    monkeypatch.setattr(janitor, "sweep_fleet_refs", lambda root: [])
    monkeypatch.setattr(janitor, "refresh_invariant_health", lambda root: None)
    monkeypatch.setattr(janitor, "sweep_eval_gaps", lambda root, store: {"ran": True})
    monkeypatch.setattr(janitor, "watch_bootstrap_footprint", lambda root, store: {"ran": True})


def test_the_filer_runs_the_sweep_once_a_day(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    calls = []
    monkeypatch.setattr(janitor, "sweep_store_compact",
                        lambda root: calls.append(root) or {"observations": 0})
    monkeypatch.setattr(janitor, "is_fleet_filer", lambda root, uuid=None, now=None: True)
    now = time.time()
    out = janitor.sweep(str(tmp_path), now=now, store=object())
    assert out["store_compact"] == {"observations": 0} and calls == [str(tmp_path)]
    out = janitor.sweep(str(tmp_path), now=now + 3600, store=object())
    assert "store_compact" not in out and len(calls) == 1


def test_a_machine_that_is_not_the_filer_never_runs_it(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    calls = []
    monkeypatch.setattr(janitor, "sweep_store_compact", lambda root: calls.append(root))
    monkeypatch.setattr(janitor, "is_fleet_filer", lambda root, uuid=None, now=None: False)
    out = janitor.sweep(str(tmp_path), now=time.time(), store=object())
    assert "store_compact" not in out and calls == []


def test_a_failed_sweep_is_logged_and_the_other_jobs_still_run(tmp_path, monkeypatch):
    _quiet(monkeypatch)

    def boom(root):
        raise RuntimeError("store-compact exit 2")
    monkeypatch.setattr(janitor, "sweep_store_compact", boom)
    monkeypatch.setattr(janitor, "is_fleet_filer", lambda root, uuid=None, now=None: True)
    out = janitor.sweep(str(tmp_path), now=time.time(), store=object())
    assert out["store_compact"].startswith("failed: ") and "exit 2" in out["store_compact"]
    assert out["eval_gaps"] == {"ran": True} and out["footprint"] == {"ran": True}


def test_no_script_in_the_store_is_a_no_op(tmp_path):
    assert janitor.sweep_store_compact(str(tmp_path)) is None


def test_the_sweep_archives_in_the_store_it_is_given(tmp_path):
    root = tmp_path / "store"
    (root / "global" / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "global" / "scripts" / "store-compact.py")
    obs = root / "global" / "audit-observations"
    obs.mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (obs / "0007.json").write_text(json.dumps(
        {"id": 7, "status": "resolved", "resolved_date": old, "created_at": old}))
    res = janitor.sweep_store_compact(str(root))
    assert res is not None and res["observations"] == 1, res
    assert (root / "global" / "audit-observations-archive" / "0007.json").is_file()
    assert not (obs / "0007.json").exists()
