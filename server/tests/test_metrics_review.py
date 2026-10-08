'Regression tests for the findings of the review of the metrics collector.\n\nEach test names the input that broke it: a hostile module in the gate scripts, the\nwt-finish output tee, adoption and token ids, a lost cursor, malformed input to the\nsummary, a torn log tail, planted symlinks, key-shaped values, and unsafe names.'
import fcntl
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from metrics_fixtures import epoch, events, log_files, of_type, raw_lines
from test_metrics_gates import (
    PRECOMMIT,
    WT_FINISH,
    Fixture,
    _commit_server,
    _finish,
    _git,
    _stage_server_change,
    _wt_setup,
)

from agent_context import metrics, metrics_report

NOW = float(epoch("2026-09-21T12:00:00Z"))


@pytest.fixture
def mdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "metrics"
    monkeypatch.setenv("AGENT_CONTEXT_METRICS_DIR", str(d))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return d


def _status(store: Path, uuid: str, machine: str, commit: str) -> None:
    d = store / "machines" / uuid
    d.mkdir(parents=True, exist_ok=True)
    (d / "daemon-status.json").write_text(json.dumps({"machine_id": machine, "server_commit": commit}))




def test_precommit_ignores_a_metrics_module_that_exits_at_import(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    (fx.store / "server" / "src" / "agent_context" / "metrics.py").write_text("import sys\nsys.exit(7)\n")
    _stage_server_change(fx.store, fx.env)
    rc, _out, err = fx.run([sys.executable, str(PRECOMMIT)], fx.store)
    assert rc == 0 and "gate passed." in err


def test_wt_finish_ignores_a_metrics_module_that_exits_at_import(tmp_path: Path) -> None:
    fx, wt = _wt_setup(tmp_path)
    (fx.store / "server" / "src" / "agent_context" / "metrics.py").write_text("import sys\nsys.exit(7)\n")
    _git(fx.store, fx.env, "add", "-A")
    _git(fx.store, fx.env, "commit", "-q", "-m", "hostile module")
    _git(wt, fx.env, "rebase", "-q", "main")
    _commit_server(wt, fx.env)
    rc, _out, err = _finish(fx, wt)
    assert rc == 0, err


def _slow_grandchild_python(fx: Fixture) -> None:
    py = fx.store / "server" / ".venv" / "bin" / "python"
    py.write_text("#!/bin/sh\n"
                  'case "$*" in\n'
                  "  *compileall*) exit 0 ;;\n"
                  "  *pytest*) sleep 8 2>/dev/null & echo '3 passed in 0.10s'; exit 0 ;;\n"
                  "esac\nexit 0\n")


def test_wt_finish_does_not_wait_for_a_grandchild_that_holds_the_pipe(tmp_path: Path) -> None:
    fx, wt = _wt_setup(tmp_path)
    _slow_grandchild_python(fx)
    _commit_server(wt, fx.env)
    start = time.monotonic()
    proc = subprocess.run([sys.executable, str(WT_FINISH), str(wt)], cwd=str(wt), env=dict(
        fx.env, AGENT_CONTEXT_STORE=str(fx.store)), capture_output=True, text=True,
        timeout=60, stdin=subprocess.DEVNULL)
    took = time.monotonic() - start
    assert proc.returncode == 0, proc.stderr
    assert took < 6
    end, = of_type(fx.mdir, "gate_end")
    assert end["ok"] is True and end["n"] == 3


class _ClosedStdout:
    'A sys.stdout whose reader has gone away.'

    class buffer:
        @staticmethod
        def write(_data: bytes) -> int:
            raise BrokenPipeError

        @staticmethod
        def flush() -> None:
            raise BrokenPipeError


def test_the_wt_finish_tee_keeps_judging_pytest_when_stdout_is_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("wt_finish_under_test", str(WT_FINISH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = tmp_path / "pytest.sh"
    fake.write_text("#!/bin/sh\necho '3 passed in 0.10s'\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "stdout", _ClosedStdout)
    code, tail = module.__dict__["_run_teed"]([str(fake)], str(tmp_path))
    assert code == 0
    assert "3 passed" in tail


def test_a_worktree_directory_name_is_made_a_valid_change_id(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    wt = fx.store / ".agents" / "worktrees" / ".we ird name"
    _git(fx.store, fx.env, "worktree", "add", "-q", "-b", "weird", str(wt))
    _stage_server_change(wt, fx.env)
    rc, _o, _e = fx.run([sys.executable, str(PRECOMMIT)], wt)
    assert rc == 0
    end, = of_type(fx.mdir, "gate_end")
    assert end["change"] == "we-ird-name"


def test_a_line_that_only_looks_like_a_summary_gives_no_test_counts(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    _stage_server_change(fx.store, fx.env)
    fx.run([sys.executable, str(PRECOMMIT)], fx.store, FAKE_PYTEST_RC="1",
           FAKE_PYTEST_OUT="collected everything in 5s and then crashed")
    end, = of_type(fx.mdir, "gate_end")
    assert end["ok"] is False and "n" not in end




def test_a_machine_that_flips_between_two_commits_is_recorded_every_time(mdir: Path,
                                                                       tmp_path: Path) -> None:
    store = tmp_path / "store"
    for step, commit in enumerate(["aaaaaaa1", "bbbbbbb2", "aaaaaaa1", "bbbbbbb2", "aaaaaaa1"]):
        _status(store, "u1", "box", commit)
        metrics_report.collect(store=store, now=NOW + 300 * step)
    refs = [str(a["ref"]) for a in of_type(mdir, "adopt")]
    assert refs == ["box@aaaaaaa", "box@bbbbbbb", "box@aaaaaaa", "box@bbbbbbb", "box@aaaaaaa"]


def _db(path: Path, rows: list[tuple[str, str]]) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE requests (request_id TEXT PRIMARY KEY, ts TEXT, day TEXT,"
                " session_id TEXT, root_session TEXT, input_tokens INTEGER DEFAULT 0,"
                " cache_w5 INTEGER DEFAULT 0, cache_w1h INTEGER DEFAULT 0,"
                " cache_read INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,"
                " cost_usd REAL DEFAULT 0)")
    for rid, session in rows:
        con.execute("INSERT INTO requests (request_id, ts, day, session_id, root_session,"
                    " input_tokens) VALUES (?, '2026-09-21T10:00:00Z', '2026-09-21', ?, ?, 1)",
                    (rid, session, session))
    con.commit()
    con.close()
    return path


def test_two_sessions_with_the_same_first_eight_characters_are_both_recorded(
        mdir: Path, tmp_path: Path) -> None:
    db = _db(tmp_path / "t.db", [("r1", "aaaaaaaa-1111"), ("r2", "aaaaaaaa-2222")])
    counts = metrics_report.collect(store=tmp_path / "store", token_db=db, now=NOW)
    assert counts["tokens"] == 2
    assert len(of_type(mdir, "tokens")) == 2


def test_a_lost_cursor_does_not_double_count(mdir: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    _status(store, "u1", "box", "aaaaaaa1")
    db = _db(tmp_path / "t.db", [("r1", "aaaaaaaa-1111")])
    metrics_report.collect(store=store, token_db=db, now=NOW)
    before = raw_lines(mdir)
    (mdir / "collect.cursor.json").unlink()
    metrics_report.collect(store=store, token_db=db, now=NOW + 300)
    assert raw_lines(mdir) == before


def test_a_second_collector_started_during_a_pass_does_nothing(mdir: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    _status(store, "u1", "box", "aaaaaaa1")
    mdir.mkdir(parents=True)
    with (mdir / "collect.cursor.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        counts = metrics_report.collect(store=store, now=NOW)
    assert counts == {"adopt": 0, "tokens": 0, "cooloff_end": 0}
    assert of_type(mdir, "adopt") == []


def test_a_claims_session_name_cannot_leave_the_claims_directory(tmp_path: Path) -> None:
    (tmp_path / "evil.json").write_text(json.dumps({"worktree": "stolen"}))
    claims = tmp_path / "claims"
    claims.mkdir()
    assert metrics_report._claim_change(claims, "../evil") is None




def test_malformed_terse_telemetry_lines_are_skipped(mdir: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    good ={"at": "2026-09-21T10:00:00Z", "event": "UserPromptSubmit", "session": "aaaa1111-0"}
    lines = ["5", "null", "[1]", "not json",
             json.dumps({"at": "2026-13-01T00:00:00Z", "event": "Stop", "session": "aaaa1111-0"}),
             json.dumps(good),
             json.dumps({"at": "2026-09-21T10:01:00Z", "event": "Stop", "session": "aaaa1111-0"})]
    (state / "terse-telemetry.jsonl").write_text("\n".join(lines) + "\n")
    day = metrics_report.build_summary(state=state)["sessions"]["aaaa1111"]["2026-09-21"]
    assert day["working_s"] == 60


@pytest.mark.parametrize("since", ["garbage", "5x", "2026-13-01"])
def test_a_bad_since_value_is_refused_with_a_message(mdir: Path, tmp_path: Path, since: str,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    rc = metrics_report.main(["summary", "--state", str(tmp_path), "--since", since])
    assert rc == 2
    assert "--since" in capsys.readouterr().out


def test_a_consent_line_with_an_impossible_date_is_skipped(mdir: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "test-lock-consent.log").write_text(
        "2026-13-45T00:00:00Z\tAPPROVED-BY-QUESTION\ttest-unlock\t/x\n"
        "2026-09-21T10:00:00Z\tAPPROVED-BY-QUESTION\ttest-unlock\t/x\n")
    granted = metrics_report.build_summary(state=state, since="2026-09-01")["approvals"]["granted_by_kind"]
    assert granted == {"test-unlock": 1}




def test_an_event_after_a_torn_last_line_is_written_whole(mdir: Path) -> None:
    assert metrics.emit("phase", ref="requested")
    target = log_files(mdir)[0]
    with target.open("a") as fh:
        fh.write('{"partial":')
    assert metrics.emit("phase", ref="criteria_sent") is True
    assert [e.get("ref") for e in metrics.read_events(mdir)] == ["requested", "criteria_sent"]
    result = metrics.verify(mdir)
    assert result.ok is False and result.bad_line == 2


def test_a_symlinked_log_or_head_is_never_written_through(mdir: Path, tmp_path: Path) -> None:
    mdir.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")
    month = time.strftime("%Y-%m", time.gmtime())
    (mdir / f"{month}.jsonl").symlink_to(victim)
    assert metrics.emit("phase", ref="requested") is False
    assert victim.read_text() == "keep me\n"
    (mdir / f"{month}.jsonl").unlink()
    (mdir / f"{month}.head").symlink_to(victim)
    metrics.emit("phase", ref="requested")
    assert victim.read_text() == "keep me\n"


KEY_SHAPED = ["ghp" + "A1b2C3d4" * 5, "AKIA" + "ABCDEFGH" * 2, "x" * 40]


@pytest.mark.parametrize("secret", KEY_SHAPED)
def test_key_shaped_values_are_rejected_in_change_ref_and_id(mdir: Path, secret: str) -> None:
    for field in ("change", "ref", "id"):
        assert metrics.emit("phase", **{field: secret}) is False
    assert raw_lines(mdir) == []


def test_short_shas_and_uuid_shaped_ids_are_still_accepted(mdir: Path) -> None:
    assert metrics.emit("landed", ref="abc1234", id="tok:2026-09-21:48cc5357-068b-4cad-9e29-66f754cd5732:12")


def test_an_oversize_event_is_counted_as_rejected(mdir: Path) -> None:
    big: dict[str, Any] = {k: 10 ** 15 for k in ("dur_ms", "n", "failed", "skipped", "reruns",
                                                  "in_tok", "out_tok", "cache_r", "cache_w")}
    before = metrics.rejected_count()
    assert metrics.emit("tokens", ref="a-" * 60, change="b-" * 32, id="c-" * 32,
                        session="d" * 8, cost_usd=123456.789012, **big) is False
    assert metrics.rejected_count() == before + 1
    assert raw_lines(mdir) == []


def test_a_lock_timeout_is_counted_as_rejected(mdir: Path) -> None:
    metrics.emit("phase", ref="requested")
    before = metrics.rejected_count()
    with log_files(mdir)[0].open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert metrics.emit("phase", ref="criteria_sent") is False
    assert metrics.rejected_count() == before + 1


def test_an_invalid_session_id_in_the_environment_does_not_drop_events(
        mdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc_def")
    assert metrics.emit("gate_start", ref="precommit") is True
    assert "session" not in events(mdir)[0]


def test_the_environment_is_left_alone_by_emit(mdir: Path) -> None:
    before = (os.getcwd(), dict(os.environ))
    metrics.emit("phase", ref="requested")
    assert (os.getcwd(), dict(os.environ)) == before
