'The metrics summary (stage 1): session time (C7), change view (C8), gates, adoption,\ntokens, determinism (C13) and read speed (C15).\n\nInputs are the metrics log plus the existing logs in the state directory:\nterse-telemetry.jsonl, test-lock-consent.log and git-write-consent.log.'
import json
import time
from pathlib import Path
from typing import Any

import pytest
from metrics_fixtures import epoch, write_chain, write_consent, write_terse
from timing_scale import calibrate, scaled_bound

from agent_context import metrics, metrics_report

DAY = "2026-09-21"


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    m, s = tmp_path / "metrics", tmp_path / "state"
    m.mkdir(exist_ok=True)
    s.mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_CONTEXT_METRICS_DIR", str(m))
    return m, s


def _emit(ts: str, event_type: str, **f: object) -> None:
    assert metrics.emit(event_type, ts=ts, **f), (event_type, f)


def _summary(state: Path, **kw: str | None) -> dict[str, Any]:
    return metrics_report.build_summary(state=state, **kw)




def test_working_idle_and_turn_latency_per_session_and_day(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    write_terse(s, [
        (f"{DAY}T10:00:00Z", "UserPromptSubmit", "aaaa1111-0000"),
        (f"{DAY}T10:02:00Z", "Stop", "aaaa1111-0000"),
        (f"{DAY}T10:10:00Z", "UserPromptSubmit", "aaaa1111-0000"),
        (f"{DAY}T10:11:00Z", "Stop", "aaaa1111-0000"),
    ])
    day = _summary(s)["sessions"]["aaaa1111"][DAY]
    assert day["working_s"] == 180
    assert day["idle_s"] == 480
    assert day["turns"] == 2
    assert day["latency_median_s"] == 90
    assert day["latency_p95_s"] == 120
    assert day["source"] == "terse-telemetry"
    assert day["requires_action_s"] is None            


def test_a_span_across_midnight_is_split_by_day(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    write_terse(s, [
        ("2026-09-20T23:50:00Z", "UserPromptSubmit", "aaaa1111-0000"),
        ("2026-09-21T00:10:00Z", "Stop", "aaaa1111-0000"),
    ])
    sess = _summary(s)["sessions"]["aaaa1111"]
    assert sess["2026-09-20"]["working_s"] == 600
    assert sess["2026-09-21"]["working_s"] == 600


def test_a_stop_without_a_prompt_and_a_double_prompt_do_not_crash(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    write_terse(s, [
        (f"{DAY}T10:00:00Z", "Stop", "aaaa1111-0000"),
        (f"{DAY}T10:01:00Z", "UserPromptSubmit", "aaaa1111-0000"),
        (f"{DAY}T10:02:00Z", "UserPromptSubmit", "aaaa1111-0000"),
        (f"{DAY}T10:03:00Z", "Stop", "aaaa1111-0000"),
    ])
    day = _summary(s)["sessions"]["aaaa1111"][DAY]
    assert day["turns"] == 1
    assert day["working_s"] == 120




def test_grants_are_counted_by_kind_and_wait_is_marked_not_measurable(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    write_consent(s, "test-lock-consent.log", [
        f"{DAY}T16:00:00Z\tUNLOCK\t/x/test-a.py",
        f"{DAY}T16:00:00Z\tAPPROVED-BY-QUESTION\ttest-unlock\t/x/test-a.py\tsession=s1\ttool_use=t1",
        f"{DAY}T17:00:00Z\tAPPROVED-BY-QUESTION\ttest-unlock\t/x/test-b.py\tsession=s1\ttool_use=t2",
    ])
    write_consent(s, "git-write-consent.log", [
        f"{DAY}T16:37:21Z\tAPPROVED-BY-QUESTION\tgit-write\t/home/x\tsession=s2\ttool_use=t3",
        f"{DAY}T16:37:23Z\t/home/x\tgit commit -q -m \"a message\"",
    ])
    appr = _summary(s)["approvals"]
    assert appr["granted_by_kind"] == {"git-write": 1, "test-unlock": 2}
    assert appr["measured_waits"] == {}
    assert "not measurable" in str(appr["wait_note"])


def test_asked_and_answered_pairs_give_a_measured_wait(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    _emit(f"{DAY}T16:30:49Z", "approval_asked", session="8fce50e0", ref="landing", src="backfill")
    _emit(f"{DAY}T16:31:04Z", "approval_answered", session="8fce50e0", ref="landing", src="backfill")
    _emit(f"{DAY}T16:33:51Z", "approval_asked", session="8fce50e0", ref="token", src="backfill")
    _emit(f"{DAY}T16:37:21Z", "approval_answered", session="8fce50e0", ref="token", src="backfill")
    waits = _summary(s)["approvals"]["measured_waits"]
    assert waits["landing"] == {"n": 1, "median_s": 15, "max_s": 15}
    assert waits["token"] == {"n": 1, "median_s": 210, "max_s": 210}




def _change_fixture(m: Path, s: Path) -> None:
    ch = "metrics-collector"
    for ts, name in [("10:00:00", "requested"), ("10:05:00", "criteria_sent"),
                     ("10:07:00", "criteria_approved"), ("10:20:00", "tests_locked"),
                     ("10:35:00", "review_start"), ("10:37:00", "review_end"),
                     ("10:40:00", "landing_asked"), ("10:41:00", "landing_approved")]:
        _emit(f"{DAY}T{ts}Z", "phase", session="bbbb2222", change=ch, ref=name)
    _emit(f"{DAY}T10:45:00Z", "landed", session="aaaa1111", change=ch, ref="abc1234", ok=True)
    _emit(f"{DAY}T10:30:00Z", "gate_start", session="aaaa1111", change=ch, ref="wt-finish")
    _emit(f"{DAY}T10:34:00Z", "gate_end", session="aaaa1111", change=ch, ref="wt-finish",
          dur_ms=240000, ok=True, n=100)
    _emit(f"{DAY}T10:01:00Z", "adopt", ref="server-host@0000000", src="baseline")
    _emit(f"{DAY}T10:01:00Z", "adopt", ref="m4@0000000", src="baseline")
    _emit(f"{DAY}T10:50:00Z", "adopt", ref="server-host@abc1234")
    _emit(f"{DAY}T11:10:00Z", "adopt", ref="m4@abc1234")
    
    write_terse(s, [
        (f"{DAY}T10:00:00Z", "UserPromptSubmit", "aaaa1111-0"), (f"{DAY}T10:05:00Z", "Stop", "aaaa1111-0"),
        (f"{DAY}T10:07:00Z", "UserPromptSubmit", "aaaa1111-0"), (f"{DAY}T10:30:00Z", "Stop", "aaaa1111-0"),
        (f"{DAY}T10:34:00Z", "UserPromptSubmit", "aaaa1111-0"), (f"{DAY}T10:35:00Z", "Stop", "aaaa1111-0"),
        (f"{DAY}T10:37:00Z", "UserPromptSubmit", "aaaa1111-0"), (f"{DAY}T10:40:00Z", "Stop", "aaaa1111-0"),
        (f"{DAY}T10:00:00Z", "UserPromptSubmit", "bbbb2222-0"), (f"{DAY}T11:10:00Z", "Stop", "bbbb2222-0"),
    ])


def test_change_time_is_split_by_cause_and_sums_to_lead_time(dirs: tuple[Path, Path]) -> None:
    m, s = dirs
    _change_fixture(m, s)
    ch = _summary(s)["changes"]["metrics-collector"]
    sec = ch["seconds"]
    assert ch["lead_s"] == 4200                       
    assert sec["user_approval"] == 180                
    assert sec["gate"] == 240
    assert sec["review"] == 120
    assert sec["adoption"] == 1500                    
    assert sec["active"] == 1920                      
    assert sec["unattributed"] == 240                 
    assert sec["other_session"] is None               
    parts = sum(int(v) for k, v in sec.items() if v is not None)
    assert parts == ch["lead_s"]
    assert round(sum(ch["share"].values()), 0) == 100


def test_a_change_with_no_events_is_absent_and_the_filter_selects_one(dirs: tuple[Path, Path]) -> None:
    m, s = dirs
    _change_fixture(m, s)
    _emit(f"{DAY}T12:00:00Z", "phase", change="other-change", ref="requested")
    _emit(f"{DAY}T12:10:00Z", "phase", change="other-change", ref="landed")
    only = _summary(s, change="other-change")
    assert list(only["changes"]) == ["other-change"]
    assert "nope" not in _summary(s)["changes"]




def test_gate_duration_failures_and_red_then_green(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    for i, (ok, ms) in enumerate([(True, 100000), (False, 90000), (True, 110000)]):
        _emit(f"{DAY}T10:0{i}:00Z", "gate_end", session="aaaa1111", change="c1",
              ref="precommit", dur_ms=ms, ok=ok, n=10)
    _emit(f"{DAY}T10:05:00Z", "gate_end", session="aaaa1111", change="c2",
          ref="precommit", dur_ms=80000, ok=True, n=10)
    g = _summary(s)["gates"]["precommit"]
    assert g["runs"] == 4
    assert g["failed"] == 1
    assert g["dur_median_s"] == 95            
    assert g["dur_p95_s"] == 110
    assert g["red_then_green"] == 1           




def test_adoption_lag_per_machine_and_a_later_commit_counts_as_adopting(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    _emit(f"{DAY}T09:00:00Z", "adopt", ref="server-host@0000000", src="baseline")
    _emit(f"{DAY}T09:00:00Z", "adopt", ref="m4@0000000", src="baseline")
    _emit(f"{DAY}T09:00:00Z", "adopt", ref="mirror-a@0000000", src="baseline")
    _emit(f"{DAY}T10:00:00Z", "landed", change="c1", ref="abc1234", ok=True)
    _emit(f"{DAY}T10:05:00Z", "adopt", ref="server-host@abc1234")
    _emit(f"{DAY}T12:00:00Z", "landed", change="c2", ref="def5678", ok=True)
    _emit(f"{DAY}T12:10:00Z", "adopt", ref="server-host@def5678")
    _emit(f"{DAY}T12:30:00Z", "adopt", ref="m4@def5678")     
    adoption = _summary(s)["adoption"]
    assert adoption["abc1234"]["machines"] == {"server-host": 300, "m4": 9000, "mirror-a": None}
    assert adoption["def5678"]["machines"] == {"server-host": 600, "m4": 1800, "mirror-a": None}


def test_a_landing_that_does_not_touch_server_expects_no_adoption(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    _emit(f"{DAY}T09:00:00Z", "adopt", ref="server-host@0000000", src="baseline")
    _emit(f"{DAY}T10:00:00Z", "landed", change="docs-only", ref="1111111", ok=False)
    assert "1111111" not in _summary(s)["adoption"]




def test_token_totals_by_session_and_change(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    _emit(f"{DAY}T10:00:00Z", "tokens", session="aaaa1111", change="c1", n=2, in_tok=30,
          out_tok=120, cache_r=3000, cache_w=120, cost_usd=2.0)
    _emit(f"{DAY}T11:00:00Z", "tokens", session="aaaa1111", change="c1", n=1, in_tok=3,
          out_tok=9, cache_r=0, cache_w=0, cost_usd=0.25)
    _emit(f"{DAY}T11:00:00Z", "tokens", session="bbbb2222", n=4, in_tok=1, out_tok=1,
          cache_r=1, cache_w=1, cost_usd=1.0)
    tok = _summary(s)["tokens"]
    assert tok["total"] == {"requests": 7, "in_tok": 34, "out_tok": 130, "cache_r": 3001,
                            "cache_w": 121, "cost_usd": 3.25}
    assert tok["by_session"]["aaaa1111"]["requests"] == 3
    assert tok["by_change"]["c1"]["cost_usd"] == 2.25
    assert "bbbb2222" in tok["by_session"]




def test_stage_two_metrics_are_listed_as_not_measured(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    nm = _summary(s)["not_measured"]
    for name in ("requires_action", "hops", "blocks", "approval_wait"):
        assert name in nm


def test_day_filter_limits_gates_and_tokens(dirs: tuple[Path, Path]) -> None:
    _m, s = dirs
    _emit("2026-09-20T10:00:00Z", "gate_end", ref="precommit", dur_ms=1000, ok=True, n=1)
    _emit(f"{DAY}T10:00:00Z", "gate_end", ref="precommit", dur_ms=2000, ok=True, n=1)
    assert _summary(s, day=DAY)["gates"]["precommit"]["runs"] == 1
    assert _summary(s)["gates"]["precommit"]["runs"] == 2




def test_same_log_gives_byte_identical_output(dirs: tuple[Path, Path]) -> None:
    m, s = dirs
    _change_fixture(m, s)
    a = metrics_report.render_text(_summary(s))
    b = metrics_report.render_text(_summary(s))
    assert a == b and "metrics-collector" in a
    ja = json.dumps(_summary(s), sort_keys=True)
    jb = json.dumps(_summary(s), sort_keys=True)
    assert ja == jb


def test_an_empty_log_gives_a_clear_report_and_exit_zero(dirs: tuple[Path, Path],
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    _m, s = dirs
    rc = metrics_report.main(["summary", "--state", str(s)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no events" in out.lower()


def test_cli_json_output_parses(dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    m, s = dirs
    _change_fixture(m, s)
    assert metrics_report.main(["summary", "--state", str(s), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["changes"]["metrics-collector"]["lead_s"] == 4200


def test_a_month_of_events_is_read_in_under_two_seconds(dirs: tuple[Path, Path]) -> None:
    m, s = dirs
    base = epoch("2026-09-01T00:00:00Z")
    rows: list[dict[str, object]] = []
    for i in range(100_000):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + i * 20))
        rows.append({"ts": ts, "type": "gate_end", "ref": "precommit", "change": f"c{i % 50}",
                     "session": "aaaa1111", "dur_ms": 1000 + i % 500, "n": 10, "ok": i % 7 != 0})
    write_chain(m / "2026-09.jsonl", rows)
    t0 = time.perf_counter()
    result = _summary(s)
    took = time.perf_counter() - t0
    assert result["gates"]["precommit"]["runs"] == 100_000
    assert took < scaled_bound(2.0, calibrate())   
