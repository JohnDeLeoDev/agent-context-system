'A machine that reaches the store through ls runs no daemon, so it publishes no status row. Its\nsilence is the normal state, and reporting it as "not reporting" in every session\'s bootstrap is a\nfalse alarm. `machine_admin(action="set_relay_only")` records the fact on the machine row, fleet\nhealth stops judging that machine\'s silence, and its rows carry `relay_last_seen`, the last time a\nsession from it reached ls.'
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_context import claims, fleet, identity, server
from agent_context import fstools as T


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    (tmp_path / "machines").mkdir()
    return tmp_path


def _put(root: Path, uuid: str, *, machine_id: str, verdict: str = "healthy",
         code_current: bool = True, age_secs: int = 0) -> None:
    t = time.time() - age_secs
    p = root / "machines" / uuid / "daemon-status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    since = int(time.time() - 3600) if code_current is False else None
    p.write_text(json.dumps({
        "machine_id": machine_id, "hostname": machine_id, "build": "1", "code_version": 1.0,
        "code_current": code_current, "code_stale_since": since, "code_defer_reason": None,
        "verdict": verdict, "sync_reason": None, "adoption": None, "updated_at": int(t)},
        separators=(",", ":"), sort_keys=True))


def _record(root: Path, uuid: str, **flags: bool) -> None:
    body = "".join(f"{k} = {str(bool(v)).lower()}\n" for k, v in flags.items())
    (root / "machines" / f"{uuid}.toml").write_text(
        f'type = "machine"\nmachine_uuid = "{uuid}"\n{body}')


def _me(store) -> str:
    return T.get_session_context(store, "/nowhere")["machine"]["machine_uuid"]




def test_set_relay_only_true_and_false_are_both_values(store) -> None:
    me = _me(store)
    on = T.machine_admin(store, "set_relay_only", relay_only=True)
    assert on["machine_uuid"] == me and on["relay_only"] is True
    row = next(e for e in store.entities.values() if e["type"] == "machine")
    assert row["relay_only"] is True
    off = T.machine_admin(store, "set_relay_only", relay_only=False)
    assert "error" not in off and off["relay_only"] is False


def test_set_relay_only_can_target_another_machine_by_its_fleet_id(store) -> None:
    _me(store)
    T.machine_admin(store, "set_display_name", display_name="Bench")
    r = T.machine_admin(store, "set_relay_only", relay_only=True, machine="Bench")
    assert r["relay_only"] is True and "error" not in r


def test_set_relay_only_needs_a_bool_and_writes_nothing_otherwise(store) -> None:
    _me(store)
    for bad in (None, "false", 1, "yes"):
        r = T.machine_admin(store, "set_relay_only", relay_only=bad)     
        assert "error" in r and "true or false" in r["error"], (bad, r)
    assert "relay_only" not in next(e for e in store.entities.values() if e["type"] == "machine")


def test_set_relay_only_on_an_unknown_machine_is_an_error(store) -> None:
    _me(store)
    r = T.machine_admin(store, "set_relay_only", relay_only=True, machine="nope")
    assert "machine not found" in r["error"]


def test_an_unknown_action_names_set_relay_only_among_the_valid_ones(store) -> None:
    r = T.machine_admin(store, "rename_everything")
    assert "set_relay_only" in r["error"]


def test_the_flag_is_settable_through_the_mcp_tool(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_store", store)
    _me(store)
    out = json.loads(server.set_machine(relay_only=True))
    assert out["relay_only"] is True


def test_setting_relay_only_leaves_the_sleeps_flag_alone(store) -> None:
    _me(store)
    T.machine_admin(store, "set_sleeps", sleeps=True)
    T.machine_admin(store, "set_relay_only", relay_only=True)
    row = next(e for e in store.entities.values() if e["type"] == "machine")
    assert row["sleeps"] is True and row["relay_only"] is True




def test_a_relay_only_machine_that_stopped_publishing_is_not_a_finding(store_root: Path) -> None:
    _put(store_root, "u1", machine_id="laptop", age_secs=13 * 3600)
    _record(store_root, "u1", relay_only=True)
    h = fleet.health(store_root)
    assert h["problems"] == [] and h["converged"] is True


def test_a_relay_only_machine_is_not_judged_on_stale_code_or_a_bad_verdict(
        store_root: Path) -> None:
    _put(store_root, "u1", machine_id="m4", code_current=False)
    _put(store_root, "u2", machine_id="pc", verdict="integration-failing")
    _record(store_root, "u1", relay_only=True)
    _record(store_root, "u2", relay_only=True)
    assert fleet.health(store_root)["problems"] == []


def test_a_machine_with_a_daemon_that_goes_quiet_is_still_a_finding(store_root: Path) -> None:
    _put(store_root, "u1", machine_id="mirror-a", age_secs=20 * 3600)
    _put(store_root, "u2", machine_id="laptop", age_secs=20 * 3600)
    _record(store_root, "u2", relay_only=True)
    lines = fleet.health(store_root)["problems"]
    assert len(lines) == 1 and lines[0].startswith("mirror-a:")


def test_clearing_the_flag_restores_the_finding(store_root: Path) -> None:
    _put(store_root, "u1", machine_id="laptop", age_secs=20 * 3600)
    _record(store_root, "u1", relay_only=False)
    assert fleet.health(store_root)["problems"] != []


def test_a_machine_that_is_both_sleeping_and_relay_only_is_not_a_finding(
        store_root: Path) -> None:
    _put(store_root, "u1", machine_id="pc", age_secs=9 * 86400)
    _record(store_root, "u1", relay_only=True, sleeps=True)
    assert fleet.health(store_root)["problems"] == []


def test_a_sleeping_machine_that_is_not_relay_only_keeps_its_week_long_warning(
        store_root: Path) -> None:
    _put(store_root, "u1", machine_id="pc", age_secs=8 * 86400)
    _record(store_root, "u1", sleeps=True)
    assert "sleeps" in fleet.health(store_root)["problems"][0]


def test_the_row_says_the_machine_is_relay_only(store_root: Path) -> None:
    _put(store_root, "u1", machine_id="laptop")
    _put(store_root, "u2", machine_id="mirror-a")
    _record(store_root, "u1", relay_only=True)
    rows = {r["machine_id"]: r for r in fleet.read_all(store_root)}
    assert rows["laptop"]["relay_only"] is True
    assert rows["mirror-a"].get("relay_only") is not True


def test_relay_only_machines_are_never_probed_over_ssh(
        store_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[str] = []
    monkeypatch.setattr(fleet, "_ssh_reachable", lambda host: probed.append(host) or True)
    _put(store_root, "u1", machine_id="laptop", age_secs=100 * 60)
    _put(store_root, "u2", machine_id="mirror-a", age_secs=100 * 60)
    _record(store_root, "u1", relay_only=True)
    fleet.health(store_root)
    assert probed == ["mirror-a"]


def test_the_session_bootstrap_drops_the_relay_only_machines_lines(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(store.root)
    (root / "machines").mkdir(exist_ok=True)
    _put(root, "u1", machine_id="laptop", age_secs=13 * 3600)
    _put(root, "u2", machine_id="rp", age_secs=13 * 3600)
    _record(root, "u1", relay_only=True)
    monkeypatch.setattr(fleet, "_ssh_reachable", lambda host: False)
    ctx = T.get_session_context(store, "/nowhere")
    lines = ctx.get("fleet_health", [])
    assert any(line.startswith("rp:") for line in lines)
    assert not any(line.startswith("laptop:") for line in lines)




def test_a_relay_session_is_remembered_beyond_the_claims_expiry() -> None:
    t0 = 1_800_000_000.0
    assert claims.note_relay_seen("u1", now=t0) is True
    assert claims.relay_seen()["u1"] == int(t0 // 3600 * 3600)          
    assert claims.relay_seen()["u1"] <= t0


def test_the_last_seen_time_is_rewritten_at_most_once_an_hour() -> None:
    t0 = 1_800_000_000.0
    claims.note_relay_seen("u1", now=t0)
    assert claims.note_relay_seen("u1", now=t0 + 600) is False
    assert claims.note_relay_seen("u1", now=t0 + 3700) is True
    assert claims.relay_seen()["u1"] > int(t0 // 3600 * 3600)


def test_each_machine_has_its_own_last_seen_time() -> None:
    claims.note_relay_seen("u1", now=1_800_000_000.0)
    claims.note_relay_seen("u2", now=1_800_010_000.0)
    seen = claims.relay_seen()
    assert seen["u1"] != seen["u2"]


def test_a_corrupt_state_file_reads_as_nothing_seen(tmp_path: Path) -> None:
    claims.note_relay_seen("u1", now=1_800_000_000.0)
    for p in claims.claims_dir().parent.glob("relay-seen*"):
        p.write_text("{not json")
    assert claims.relay_seen() == {}
    assert claims.note_relay_seen("u1", now=1_800_000_000.0) is True    


def test_the_fleet_row_carries_relay_last_seen(store_root: Path) -> None:
    _put(store_root, "u1", machine_id="laptop", age_secs=13 * 3600)
    _record(store_root, "u1", relay_only=True)
    claims.note_relay_seen("u1", now=time.time())
    row = next(r for r in fleet.read_all(store_root) if r["machine_id"] == "laptop")
    assert abs(row["relay_last_seen"] - time.time()) < 3600 + 5


def test_a_machine_never_seen_over_the_relay_has_no_last_seen(store_root: Path) -> None:
    _put(store_root, "u1", machine_id="laptop")
    _record(store_root, "u1", relay_only=True)
    row = next(r for r in fleet.read_all(store_root) if r["machine_id"] == "laptop")
    assert row.get("relay_last_seen") is None


def test_a_remote_bootstrap_records_that_the_machine_was_seen(store) -> None:
    uuid = "B5B75545-B2E5-59A1-B327-47062408E8A2"
    root = Path(store.root)
    (root / "machines").mkdir(exist_ok=True)
    (root / "machines" / f"{uuid}.toml").write_text(
        f'type = "machine"\nmachine_uuid = "{uuid}"\nmachine_id = "laptop"\n'
        'hostname = "Laptop.local"\nplatform = "darwin"\nhome_dir = "/Users/user"\n')
    store.reload()
    who = identity.Identity(uuid, "laptop", "darwin", "/Users/user", session_key="k1")
    token = identity.bind(who)
    try:
        T.get_session_context(store, "/Users/user/x")
    finally:
        identity.reset(token)
    assert uuid in claims.relay_seen()


def test_a_local_bootstrap_records_no_relay_sighting(store) -> None:
    T.get_session_context(store, "/nowhere")
    assert claims.relay_seen() == {}

