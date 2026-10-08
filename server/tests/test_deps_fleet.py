"Dependency verdicts in fleet health: deps_report reads deps-check.py's report, fleet.publish\ncarries it in the row, fleet.problems names the tools.\n\nUnknown is never clean. A machine with no report, an unreadable one, or one older than three\ndays publishes `deps: null`, which raises no problem and claims no health."
import json
import time

from agent_context import deps_report, fleet


def _report(tmp_path, fleet_block, at=None, name="deps.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"at": int(time.time()) if at is None else at, "fleet": fleet_block}))
    return p


CLEAN = {"ok": True, "missing": [], "broken": [], "below_floor": []}
BAD = {"ok": False, "missing": ["jq"], "broken": ["csharp-ls"], "below_floor": ["uv"]}


def test_a_current_report_is_read_as_its_fleet_block(tmp_path):
    assert deps_report.read(_report(tmp_path, BAD)) == BAD


def test_the_lists_are_sorted_so_the_row_bytes_do_not_depend_on_check_order(tmp_path):
    block = {"ok": False, "missing": ["uv", "jq"], "broken": [], "below_floor": []}
    got = deps_report.read(_report(tmp_path, block))
    assert got is not None
    assert got["missing"] == ["jq", "uv"]


def test_a_missing_report_is_unknown(tmp_path):
    assert deps_report.read(tmp_path / "nope.json") is None


def test_a_corrupt_report_is_unknown(tmp_path):
    p = tmp_path / "deps.json"
    p.write_text("{not json")
    assert deps_report.read(p) is None


def test_a_report_of_the_wrong_shape_is_unknown(tmp_path):
    assert deps_report.read(_report(tmp_path, {"ok": "yes", "missing": []})) is None
    assert deps_report.read(_report(tmp_path, ["not", "a", "dict"])) is None


def test_a_report_older_than_three_days_is_marked_stale_not_dropped(tmp_path):
    now = 2_000_000_000
    fresh = _report(tmp_path, CLEAN, at=now - deps_report.STALE_SECS, name="a.json")
    old = _report(tmp_path, BAD, at=now - deps_report.STALE_SECS - 1, name="b.json")
    assert deps_report.read(fresh, now=now) == CLEAN
    assert deps_report.read(old, now=now) == {
        "ok": None, "stale": True, "missing": [], "broken": [], "below_floor": []}


def test_a_stale_report_is_a_problem_line_that_names_the_checker():
    stale = {"ok": None, "stale": True, "missing": [], "broken": [], "below_floor": []}
    out = fleet.problems(_rows(stale), now=1_800_000_000)
    assert len(out) == 1
    assert out[0].startswith("m4: its dependency report is more than 3 days old")
    assert "deps-check.py" in out[0]


def test_a_stale_report_is_never_read_as_clean_or_as_the_old_failure(tmp_path):
    stale = {"ok": None, "stale": True, "missing": [], "broken": [], "below_floor": []}
    _publish(tmp_path, stale)
    assert _row(tmp_path)["deps"] == stale


def _publish(root, deps, **kw):
    return fleet.publish(root, "u1", {"verdict": "healthy", "code_current": True},
                         machine_id="m4", hostname="m4", now=1_800_000_000, deps=deps, **kw)


def _row(root):
    return json.loads(fleet.status_path(root, "u1").read_text())


def test_publish_carries_the_block_and_defaults_to_null(tmp_path):
    _publish(tmp_path, BAD)
    assert _row(tmp_path)["deps"] == BAD
    other = tmp_path / "other"
    _publish(other, None)
    assert _row(other)["deps"] is None


def test_an_unchanged_block_is_not_rewritten(tmp_path):
    assert _publish(tmp_path, BAD) is True
    assert _publish(tmp_path, BAD) is False
    assert _publish(tmp_path, CLEAN) is True


def _rows(deps, **extra):
    return [{"machine_id": "m4", "machine_uuid": "u1", "updated_at": 1_800_000_000,
             "age_secs": 0, "stale": False, "verdict": "healthy", "code_current": True,
             "deps": deps, **extra}]


def test_a_failing_block_is_a_problem_naming_every_tool():
    out = fleet.problems(_rows(BAD), now=1_800_000_000)
    assert len(out) == 1
    line = out[0]
    assert line.startswith("m4: dependency problem:")
    for tool in ("missing jq", "broken csharp-ls", "below floor uv"):
        assert tool in line
    assert "deps-check.py" in line


def test_a_clean_or_unknown_block_is_no_problem():
    assert fleet.problems(_rows(CLEAN), now=1_800_000_000) == []
    assert fleet.problems(_rows(None), now=1_800_000_000) == []


def test_a_failing_block_with_no_tool_names_says_nothing():
    empty = {"ok": False, "missing": [], "broken": [], "below_floor": []}
    assert fleet.problems(_rows(empty), now=1_800_000_000) == []


def test_a_relay_only_machine_with_a_deps_block_still_raises_a_dependency_problem():
    
    
    
    out = fleet.problems(_rows(BAD, relay_only=True), now=1_800_000_000)
    assert len(out) == 1
    assert out[0].startswith("m4: dependency problem:")


def test_a_relay_only_machine_with_no_deps_block_raises_no_dependency_problem():
    assert fleet.problems(_rows(None, relay_only=True), now=1_800_000_000) == []
