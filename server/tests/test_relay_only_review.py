'Defects an adversarial review found in the relay-only work (test_relay_only.py holds the criteria).\n\nThe relay sighting file must never break fleet health, a hand-edited record must not flip the flag,\nand a relay-only machine with no daemon row must still show when it was last seen.'
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_context import claims, fleet, server
from agent_context import fstools as T


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    (tmp_path / "machines").mkdir()
    return tmp_path


def _record(root: Path, uuid: str, body: str) -> None:
    (root / "machines" / f"{uuid}.toml").write_text(
        f'type = "machine"\nmachine_uuid = "{uuid}"\n{body}')




@pytest.mark.parametrize("bad", ['{"u1": NaN}', '{"u1": Infinity}', '{"u1": 1e400}',
                                 '{"u1": -Infinity}'])
def test_a_non_finite_stamp_is_dropped_not_raised(bad: str) -> None:
    claims.note_relay_seen("u0", now=1_800_000_000.0)
    claims._relay_seen_path().write_text(bad)
    assert claims.relay_seen() == {}
    assert claims.note_relay_seen("u1", now=1_800_000_000.0) is True     
    assert claims.relay_seen() == {"u1": 1_800_000_000}


def test_one_bad_stamp_does_not_hide_the_good_ones() -> None:
    claims.note_relay_seen("u0", now=1_800_000_000.0)
    claims._relay_seen_path().write_text('{"u1": 1e400, "u2": 1800000000, "u3": "x", "u4": true}')
    assert claims.relay_seen() == {"u2": 1_800_000_000}


def test_an_unusable_state_dir_reads_as_nothing_seen_and_never_raises(
        store_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Path:
        raise PermissionError("state dir is read-only")
    monkeypatch.setattr(claims, "_relay_seen_path", boom)
    assert claims.relay_seen() == {}
    assert claims.note_relay_seen("u1") is False
    assert fleet.read_all(store_root) == []                     


def test_fleet_health_survives_a_broken_sighting_file(store_root: Path) -> None:
    claims.note_relay_seen("u1", now=time.time())
    claims._relay_seen_path().write_text('{"u1": NaN}')
    assert fleet.health(store_root)["problems"] == []




@pytest.mark.parametrize("body", ['relay_only = "false"\n', 'relay_only = "true"\n',
                                  "relay_only = 1\n", 'relay_only = "yes"\n'])
def test_only_a_real_true_makes_a_machine_relay_only(store_root: Path, body: str) -> None:
    _record(store_root, "u1", body)
    assert fleet._relay_only(store_root / "machines", "u1") is False


def test_a_real_true_still_does(store_root: Path) -> None:
    _record(store_root, "u1", "relay_only = true\n")
    assert fleet._relay_only(store_root / "machines", "u1") is True




def test_a_relay_only_machine_with_no_daemon_row_appears_with_its_last_seen(
        store_root: Path) -> None:
    _record(store_root, "u1", 'machine_id = "laptop"\nhostname = "Laptop.local"\nrelay_only = true\n')
    claims.note_relay_seen("u1", now=time.time())
    rows = {r["machine_uuid"]: r for r in fleet.read_all(store_root)}
    row = rows["u1"]
    assert row["machine_id"] == "laptop" and row["relay_only"] is True
    assert abs(row["relay_last_seen"] - time.time()) < 3600 + 5
    assert row["stale"] is False and row["age_secs"] >= 0
    assert fleet.problems(list(rows.values())) == []


def test_a_sighting_of_a_machine_with_no_record_adds_no_row(store_root: Path) -> None:
    claims.note_relay_seen("ghost", now=time.time())
    assert fleet.read_all(store_root) == []


def test_a_daemon_machine_that_was_seen_over_the_relay_keeps_its_own_row_only_once(
        store_root: Path) -> None:
    p = store_root / "machines" / "u1" / "daemon-status.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"machine_id": "mirror-a", "hostname": "s1", "verdict": "healthy",
                             "updated_at": int(time.time())}))
    _record(store_root, "u1", 'machine_id = "mirror-a"\n')
    claims.note_relay_seen("u1", now=time.time())
    rows = [r for r in fleet.read_all(store_root) if r["machine_uuid"] == "u1"]
    assert len(rows) == 1 and "relay_last_seen" in rows[0]




def test_a_dry_run_of_set_relay_only_reports_the_machine_file_and_writes_nothing(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_store", store)
    me = T.get_session_context(store, "/nowhere")["machine"]["machine_uuid"]
    out = json.loads(server.set_machine(relay_only=True, dry_run=True))
    assert out["dry_run"] is True
    assert [c["path"] for c in out["would_change"]] == [f"machines/{me}.toml"]
    assert "relay_only" not in next(e for e in store.entities.values() if e["type"] == "machine")
