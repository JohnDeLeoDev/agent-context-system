'One-off import of the hand-written ledgers into the log, marked src=backfill (C19),\nand the replay of one real day (C14).'
from pathlib import Path

import pytest
from metrics_fixtures import events, of_type, raw_lines, write_consent

from agent_context import metrics, metrics_report

TIMING = """\
# Timing ledger, session 8fce50e0 (all times UTC, 2026-09-21)

Backfilled from git commit times. Not recorded at the time: gate start and end.

## unbound-log (session audit lines, fcac96fcf)
- criteria approved: through the orchestrator, before this ledger; tests locked 16:24:06 and 16:26:24
- commit ca3370c0c 16:28:48; landing asked 16:30:49, approved 16:31:04 (15 s); landed 16:33:00
## unknown-caller (46739ea9a)
- criteria asked 16:51:29, approved 16:52:29 (60 s); tests locked 16:53:58 and 16:57:32
- commit 30d3999e3 16:59:42; landing asked 17:01:48, approved 17:03:32 (104 s); landed 17:05:34
## table-hermetic (in progress)
- commit 8d9cd824e 18:13:39
"""

TSV = (
    "2026-09-21T15:01:31.936Z\t2026-09-21T15:03:01.258Z\tDesign\tWhich design should the report recommend?\n"
    "2026-09-21T15:27:15.587Z\t2026-09-21T15:27:20.256Z\tApproval\tUnlock the locked test /home/user/.agent-context/x/test_a.py?\n"
    "2026-09-21T16:05:20.840Z\t2026-09-21T16:05:24.337Z\tApproval\tAllow agent writes under /etc/nginx/sites-enabled for the next 15 minutes?\n"
    "2026-09-21T16:30:49.137Z\t2026-09-21T16:31:04.317Z\tLand logging\tLand branch unbound-log (ca3370c0c) onto main now?\n"
    "2026-09-21T16:33:51.321Z\t2026-09-21T16:37:21.569Z\tApproval\tAllow one git commit, push or merge in /home/user/.agent-context within 15 minutes?\n"
    "table-hermetic: commit ca6c14c2a 2026-09-21T14:19:06-04:00; landing asked 2026-09-21T18:21:36Z\n"
)

METRICS_LEDGER = """\
# Metrics ledger, change: full-coverage (worktree .agents/worktrees/full-coverage)
Recorded from 2026-09-21T18:20:49Z.

## Approvals raised in this session
| Kind | Asked | Answered |
|---|---|---|
| criteria approval | not recorded | Approve, not recorded |
| test-unlock test-test-lock-worktree-records | not recorded | Approve, by 14:32:47Z (edit applied) |
| test-unlock test-lspd | not recorded | Approve, by 15:00:41Z |
| hook fix block-coauthor-trailer | not recorded | Yes, not recorded |
| 2026-09-21T18:22:35Z | lsp-only re-run PASS 10/0 after socket-path fix; review START |
| 2026-09-21T18:24:51Z | review END: 1 confirmed (allowlist stale) |
"""


@pytest.fixture
def mdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "metrics"
    monkeypatch.setenv("AGENT_CONTEXT_METRICS_DIR", str(d))
    return d


@pytest.fixture
def ledgers(tmp_path: Path) -> list[Path]:
    a, b, c = (tmp_path / "timing-ledger.md", tmp_path / "approvals-backfill.tsv",
               tmp_path / "metrics-ledger.md")
    a.write_text(TIMING)
    b.write_text(TSV)
    c.write_text(METRICS_LEDGER)
    return [a, b, c]


def test_every_backfilled_line_is_marked_and_the_log_verifies(mdir: Path, ledgers: list[Path]) -> None:
    metrics_report.backfill(ledgers)
    rows = events(mdir)
    assert rows and all(r["src"] == "backfill" for r in rows)
    assert metrics.verify(mdir).ok is True


def test_timing_ledger_becomes_phase_events_per_change(mdir: Path, ledgers: list[Path]) -> None:
    metrics_report.backfill(ledgers[:1])
    uc = [(e["ts"], e["ref"]) for e in of_type(mdir, "phase") if e["change"] == "unknown-caller"]
    assert ("2026-09-21T16:51:29Z", "criteria_sent") in uc
    assert ("2026-09-21T16:52:29Z", "criteria_approved") in uc
    assert ("2026-09-21T16:53:58Z", "tests_locked") in uc
    assert ("2026-09-21T16:57:32Z", "tests_locked") in uc
    assert ("2026-09-21T16:59:42Z", "committed") in uc
    assert ("2026-09-21T17:01:48Z", "landing_asked") in uc
    assert ("2026-09-21T17:03:32Z", "landing_approved") in uc
    assert ("2026-09-21T17:05:34Z", "landed") in uc
    assert {e["session"] for e in of_type(mdir, "phase")} == {"8fce50e0"}


def test_an_in_progress_change_keeps_only_what_was_recorded(mdir: Path, ledgers: list[Path]) -> None:
    metrics_report.backfill(ledgers[:1])
    th = [e["ref"] for e in of_type(mdir, "phase") if e["change"] == "table-hermetic"]
    assert th == ["committed"]


def test_tsv_becomes_asked_and_answered_events_with_a_kind(mdir: Path, ledgers: list[Path]) -> None:
    metrics_report.backfill(ledgers[1:2])
    asked = of_type(mdir, "approval_asked")
    answered = of_type(mdir, "approval_answered")
    assert len(asked) == len(answered) == 5                 
    kinds = sorted(str(a["ref"]) for a in asked)
    assert kinds == ["landing", "other", "sudo", "token", "unlock"]
    landing = next(a for a in asked if a["ref"] == "landing")
    assert landing["ts"] == "2026-09-21T16:30:49Z"
    ans = next(a for a in answered if a["ref"] == "landing")
    assert ans["ts"] == "2026-09-21T16:31:04Z"


def test_question_text_and_paths_never_reach_the_log(mdir: Path, ledgers: list[Path]) -> None:
    metrics_report.backfill(ledgers)
    blob = "\n".join(raw_lines(mdir))
    assert blob                                           
    for leak in ("nginx", "sites-enabled", "/home/user", "Which design", "unbound-log (", "test_a.py"):
        assert leak not in blob


def test_metrics_ledger_gives_review_phases_and_bounded_answers(mdir: Path, ledgers: list[Path]) -> None:
    metrics_report.backfill(ledgers[2:])
    phases = [(e["ts"], e["ref"], e["change"]) for e in of_type(mdir, "phase")]
    assert ("2026-09-21T18:22:35Z", "review_start", "full-coverage") in phases
    assert ("2026-09-21T18:24:51Z", "review_end", "full-coverage") in phases
    answered = [(e["ts"], e["ref"]) for e in of_type(mdir, "approval_answered")]
    assert ("2026-09-21T14:32:47Z", "unlock") in answered
    assert ("2026-09-21T15:00:41Z", "unlock") in answered
    assert len(answered) == 2                                
    assert of_type(mdir, "approval_asked") == []             


def test_running_the_backfill_twice_adds_nothing(mdir: Path, ledgers: list[Path]) -> None:
    first = metrics_report.backfill(ledgers)
    n = len(raw_lines(mdir))
    assert n > 10
    second = metrics_report.backfill(ledgers)
    assert len(raw_lines(mdir)) == n
    assert sum(second.values()) == 0
    assert sum(first.values()) == n


def test_a_missing_or_unknown_file_is_skipped_not_fatal(mdir: Path, tmp_path: Path) -> None:
    other = tmp_path / "notes.txt"
    other.write_text("hello\n")
    valid_tsv = tmp_path / "approvals-backfill.tsv"
    valid_tsv.write_text(TSV)
    counts = metrics_report.backfill([tmp_path / "absent.md", other, valid_tsv])
    assert sum(counts.values()) == 10                     
    assert len(raw_lines(mdir)) == 10




def test_replay_finds_the_same_approvals_as_the_source_files(mdir: Path, ledgers: list[Path],
                                                            tmp_path: Path) -> None:
    metrics_report.backfill(ledgers[1:2])
    state = tmp_path / "state"
    write_consent(state, "test-lock-consent.log", [
        "2026-09-21T16:45:14Z\tAPPROVED-BY-QUESTION\ttest-unlock\t/x/a.py\tsession=s\ttool_use=t1",
        "2026-09-21T16:45:14Z\tAPPROVED-BY-QUESTION\ttest-unlock\t/x/b.py\tsession=s\ttool_use=t2",
    ])
    summary = metrics_report.build_summary(state=state)
    waits = summary["approvals"]["measured_waits"]
    assert sum(int(v["n"]) for v in waits.values()) == 5     
    assert sum(summary["approvals"]["granted_by_kind"].values()) == 2   
