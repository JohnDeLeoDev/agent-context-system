'1. Rows exist but every one is stale (a fleet-wide publish failure, a store before its\n   first sync): the first version returned True on every machine, which is the\n   duplicate filing the rule exists to end. One machine still has to be chosen, and\n   every machine must choose the same one from the same rows.\n2. The uuid in machines/<uuid>/ and get_machine_uuid() agree today because one call\n   writes both, but a macOS uuid is upper case and a Linux one lower case, so a compare\n   that is case-sensitive stops filing the day either side changes spelling.'
import json
import time

from agent_context import fleet, janitor


def _row(root, uuid, age_secs=0, now=None):
    t = (time.time() if now is None else now) - age_secs
    p = root / "machines" / uuid / "daemon-status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"machine_id": uuid, "verdict": "healthy",
                             "updated_at": int(t)}))


def test_when_every_row_is_stale_only_the_most_recent_machine_files(tmp_path):
    now = time.time()
    _row(tmp_path, "u-a", age_secs=fleet.STALE_SECS + 7200, now=now)
    _row(tmp_path, "u-b", age_secs=fleet.STALE_SECS + 60, now=now)
    _row(tmp_path, "u-c", age_secs=fleet.STALE_SECS + 3600, now=now)
    filers = [u for u in ("u-a", "u-b", "u-c")
              if janitor.is_fleet_filer(tmp_path, u, now=now)]
    assert filers == ["u-b"]


def test_uuid_case_does_not_decide_who_files(tmp_path):
    now = time.time()
    _row(tmp_path, "ABC-1", now=now)
    _row(tmp_path, "abd-2", now=now)
    assert janitor.is_fleet_filer(tmp_path, "abc-1", now=now) is True
    assert janitor.is_fleet_filer(tmp_path, "ABD-2", now=now) is False
