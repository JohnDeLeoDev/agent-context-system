"fleet.read_all()/problems() merging machines/<uuid>/deps.json into a relay-only host's row.\n\nA relay-only machine runs no daemon and publishes no daemon-status.json, so its `deps` block\n(uploaded via PUT /deps/<uuid>.json, see deps_upload_route.py) can only reach the fleet view\nthrough this merge. A daemon host's own daemon-status.json `deps` block always wins."
import json
import time
from pathlib import Path

from agent_context import claims, deps_report, fleet

CLEAN = {"ok": True, "missing": [], "broken": [], "below_floor": []}
BAD = {"ok": False, "missing": ["jq"], "broken": [], "below_floor": []}


def _record(root: Path, uuid: str, *, relay_only: bool, machine_id: str) -> None:
    (root / "machines" / f"{uuid}.toml").write_text(
        f'type = "machine"\nmachine_uuid = "{uuid}"\nmachine_id = "{machine_id}"\n'
        f'relay_only = {str(relay_only).lower()}\n')


def _write_deps_upload(root: Path, uuid: str, fleet_block: dict, at: float) -> None:
    p = root / "machines" / uuid / "deps.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": 1, "machine": uuid, "os": "linux", "at": at,
                             "ok": fleet_block["ok"], "tools": {}, "problems": [],
                             "unexpected": [], "fleet": fleet_block}))


def test_a_relay_only_machine_with_an_uploaded_report_gets_the_deps_block(tmp_path: Path) -> None:
    (tmp_path / "machines").mkdir()
    _record(tmp_path, "u1", relay_only=True, machine_id="laptop")
    claims.note_relay_seen("u1", now=time.time())
    _write_deps_upload(tmp_path, "u1", BAD, time.time())
    row = next(r for r in fleet.read_all(tmp_path) if r["machine_uuid"] == "u1")
    assert row["deps"] == BAD


def test_a_relay_only_machine_with_no_uploaded_report_has_no_deps_block(tmp_path: Path) -> None:
    (tmp_path / "machines").mkdir()
    _record(tmp_path, "u1", relay_only=True, machine_id="laptop")
    claims.note_relay_seen("u1", now=time.time())
    row = next(r for r in fleet.read_all(tmp_path) if r["machine_uuid"] == "u1")
    assert row.get("deps") is None


def test_a_relay_only_machines_uploaded_problem_reaches_fleet_problems(tmp_path: Path) -> None:
    (tmp_path / "machines").mkdir()
    _record(tmp_path, "u1", relay_only=True, machine_id="laptop")
    claims.note_relay_seen("u1", now=time.time())
    _write_deps_upload(tmp_path, "u1", BAD, time.time())
    lines = fleet.problems(fleet.read_all(tmp_path))
    assert len(lines) == 1
    assert lines[0].startswith("laptop: dependency problem:")
    assert "missing jq" in lines[0]


def test_a_relay_only_machines_clean_uploaded_report_raises_no_problem(tmp_path: Path) -> None:
    (tmp_path / "machines").mkdir()
    _record(tmp_path, "u1", relay_only=True, machine_id="laptop")
    claims.note_relay_seen("u1", now=time.time())
    _write_deps_upload(tmp_path, "u1", CLEAN, time.time())
    assert fleet.problems(fleet.read_all(tmp_path)) == []


def test_a_stale_upload_reads_the_same_way_a_daemons_own_stale_report_does(tmp_path: Path) -> None:
    (tmp_path / "machines").mkdir()
    _record(tmp_path, "u1", relay_only=True, machine_id="laptop")
    now = time.time()
    claims.note_relay_seen("u1", now=now)
    _write_deps_upload(tmp_path, "u1", CLEAN, now - deps_report.STALE_SECS - 1)
    row = next(r for r in fleet.read_all(tmp_path, now=now) if r["machine_uuid"] == "u1")
    assert row["deps"] == {"ok": None, "stale": True, "missing": [], "broken": [], "below_floor": []}
    lines = fleet.problems(fleet.read_all(tmp_path, now=now), now=now)
    assert len(lines) == 1 and "more than 3 days old" in lines[0]


def test_a_daemon_hosts_own_deps_block_is_never_overridden_by_a_stray_upload(tmp_path: Path) -> None:
    "Adversarial (criterion 11): a daemon host publishes its own daemon-status.json with a\n    CLEAN deps block, and a stray machines/<uuid>/deps.json (as if something had wrongly\n    uploaded there) says BAD. The daemon's own row must win."
    (tmp_path / "machines").mkdir()
    fleet.publish(tmp_path, "u1", {"verdict": "healthy", "code_current": True},
                  machine_id="s1", hostname="mirror-a", now=1_800_000_000, deps=CLEAN)
    _write_deps_upload(tmp_path, "u1", BAD, time.time())
    row = next(r for r in fleet.read_all(tmp_path, now=1_800_000_100) if r["machine_uuid"] == "u1")
    assert row["deps"] == CLEAN
    lines = fleet.problems(fleet.read_all(tmp_path, now=1_800_000_100), now=1_800_000_100)
    assert lines == []
