'Terse-telemetry rows with no session identity are left out of the sessions list and counted.'
import json
from pathlib import Path
from typing import Any

import pytest
from metrics_fixtures import write_terse

from agent_context import metrics_report

DAY = "2026-09-21"
FIELDS = {"words": 10, "budget": 40, "over": False, "earned": None, "in_loop": False,
          "depth": 1, "transcript_bytes": 100}


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    m, s = tmp_path / "metrics", tmp_path / "state"
    m.mkdir(exist_ok=True)
    s.mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_CONTEXT_METRICS_DIR", str(m))
    return s


def _summary(state: Path) -> dict[str, Any]:
    return metrics_report.build_summary(state=state)


def _write_raw(state: Path, rows: list[dict[str, object]]) -> None:
    with (state / "terse-telemetry.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps({**FIELDS, **row}) + "\n")


REAL = [
    (f"{DAY}T10:00:00Z", "UserPromptSubmit", "aaaa1111-0000"),
    (f"{DAY}T10:02:00Z", "Stop", "aaaa1111-0000"),
    (f"{DAY}T10:10:00Z", "UserPromptSubmit", "aaaa1111-0000"),
    (f"{DAY}T10:11:00Z", "Stop", "aaaa1111-0000"),
]
BLANK = [
    (f"{DAY}T03:41:06Z", "UserPromptSubmit", ""),
    (f"{DAY}T18:55:16Z", "Stop", ""),
]
REAL_DAY = {"working_s": 180, "idle_s": 480, "turns": 2, "latency_median_s": 90,
            "latency_p95_s": 120, "requires_action_s": None, "source": "terse-telemetry"}




def test_empty_session_rows_form_no_session_and_real_sessions_are_unchanged(state: Path) -> None:
    write_terse(state, [*BLANK, *REAL])
    sessions = _summary(state)["sessions"]
    assert "" not in sessions
    assert sessions == {"aaaa1111": {DAY: REAL_DAY}}




def test_unattributed_rows_are_counted(state: Path) -> None:
    write_terse(state, [*BLANK, *REAL])
    summary = _summary(state)
    assert summary["terse_unattributed_rows"] == 2
    assert "2" in next(line for line in metrics_report.render_text(summary).splitlines()
                       if "unattributed" in line)


def test_a_log_of_only_empty_session_rows_has_no_sessions(state: Path) -> None:
    write_terse(state, BLANK)
    summary = _summary(state)
    assert summary["sessions"] == {}
    assert summary["terse_unattributed_rows"] == 2




def test_a_log_without_empty_rows_is_unchanged_and_counts_zero(state: Path) -> None:
    write_terse(state, REAL)
    summary = _summary(state)
    assert summary["terse_unattributed_rows"] == 0
    assert summary["sessions"] == {"aaaa1111": {DAY: REAL_DAY}}
    assert summary["empty"] is False
    assert "unattributed" not in metrics_report.render_text(summary)


def test_a_missing_terse_file_counts_zero(state: Path) -> None:
    summary = _summary(state)
    assert summary["terse_unattributed_rows"] == 0
    assert summary["sessions"] == {}




def test_whitespace_missing_and_null_sessions_are_skipped_and_counted(state: Path) -> None:
    _write_raw(state, [
        {"at": f"{DAY}T09:00:00Z", "event": "UserPromptSubmit", "session": "   "},
        {"at": f"{DAY}T09:01:00Z", "event": "Stop", "session": "   "},
        {"at": f"{DAY}T09:02:00Z", "event": "UserPromptSubmit"},
        {"at": f"{DAY}T09:03:00Z", "event": "Stop", "session": None},
        {"at": f"{DAY}T10:00:00Z", "event": "UserPromptSubmit", "session": "aaaa1111-0000"},
        {"at": f"{DAY}T10:02:00Z", "event": "Stop", "session": "aaaa1111-0000"},
    ])
    summary = _summary(state)
    assert list(summary["sessions"]) == ["aaaa1111"]
    assert summary["terse_unattributed_rows"] == 4


def test_rows_that_are_not_events_at_all_are_not_counted(state: Path) -> None:
    (state / "terse-telemetry.jsonl").write_text(
        "not json\n[1, 2]\n" + json.dumps({"session": "", "words": 1}) + "\n",
        encoding="utf-8")
    assert _summary(state)["terse_unattributed_rows"] == 0
