'Detecting a relay-only machine that was never marked (policy task 2).'
import json
import time
from pathlib import Path

import pytest

from agent_context import claims, fleet


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    (tmp_path / "machines").mkdir()
    return tmp_path


def _put(root: Path, uuid: str, *, machine_id: str, age_secs: int = 0) -> None:
    t = time.time() - age_secs
    p = root / "machines" / uuid / "daemon-status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "machine_id": machine_id, "hostname": machine_id, "build": "1", "code_version": 1.0,
        "code_current": True, "code_stale_since": None, "code_defer_reason": None,
        "verdict": "healthy", "sync_reason": None, "adoption": None, "updated_at": int(t)},
        separators=(",", ":"), sort_keys=True))


def test_an_unmarked_relay_only_machine_is_named_and_given_the_fix(store_root: Path) -> None:
    _put(store_root, "u1", machine_id="mirror-a", age_secs=20 * 3600)
    claims.note_relay_seen("u1", now=time.time())
    line = fleet.health(store_root)["problems"][0]
    assert line.startswith("mirror-a:")
    assert "relay" in line
    assert "set_machine(machine='mirror-a', relay_only=True)" in line
    assert "not reporting" not in line


def test_a_stale_machine_the_relay_has_not_seen_since_keeps_the_generic_line(
        store_root: Path) -> None:
    _put(store_root, "u1", machine_id="mirror-a", age_secs=20 * 3600)
    claims.note_relay_seen("u1", now=time.time() - 25 * 3600)
    line = fleet.health(store_root)["problems"][0]
    assert "not reporting for 20h" in line


def test_a_machine_with_no_relay_sighting_at_all_keeps_the_generic_line(
        store_root: Path) -> None:
    _put(store_root, "u1", machine_id="mirror-a", age_secs=20 * 3600)
    line = fleet.health(store_root)["problems"][0]
    assert "not reporting for 20h" in line
