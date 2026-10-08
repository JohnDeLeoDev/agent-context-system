'The metrics collector: adoption (C10), token import (C11), cooling-off end, idempotency (C12).\n\nRuns against a throwaway store, token database and claims directory. It must not reach\nthe network.'
import json
import socket
import sqlite3
from pathlib import Path

import pytest
from metrics_fixtures import epoch, events, of_type, raw_lines

from agent_context import metrics, metrics_report

REQUESTS_DDL = """
CREATE TABLE requests (
  request_id TEXT PRIMARY KEY, ts TEXT NOT NULL, day TEXT NOT NULL, session_id TEXT,
  root_session TEXT, project TEXT, git_branch TEXT, model TEXT, effort TEXT,
  sidechain INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_w5 INTEGER NOT NULL DEFAULT 0, cache_w1h INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0, cost_input_usd REAL NOT NULL DEFAULT 0)
"""
NOW = float(epoch("2026-09-21T12:00:00Z"))


@pytest.fixture
def mdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "metrics"
    monkeypatch.setenv("AGENT_CONTEXT_METRICS_DIR", str(d))
    return d


def _status(store: Path, uuid: str, machine: str, commit: str) -> None:
    d = store / "machines" / uuid
    d.mkdir(parents=True, exist_ok=True)
    (d / "daemon-status.json").write_text(json.dumps(
        {"machine_id": machine, "machine_uuid": uuid, "server_commit": commit,
         "updated_at": 1790013600, "code_current": True, "verdict": "healthy"}))


@pytest.fixture
def store(tmp_path: Path) -> Path:
    s = tmp_path / "store"
    _status(s, "5b0f8d11", "server-host", "ab3584575")
    _status(s, "b19eb343", "mirror-b", "385ffdc61")
    return s


def _db(path: Path, rows: list[tuple[str, str, str, str, int, int, int, int, int, float]]) -> Path:
    'rows: (request_id, ts, day, root_session, in, w5, w1h, read, out, cost).'
    con = sqlite3.connect(path)
    con.execute(REQUESTS_DDL)
    for rid, ts, day, root, i, w5, w1h, rd, out, cost in rows:
        con.execute(
            "INSERT INTO requests (request_id, ts, day, session_id, root_session, input_tokens,"
            " cache_w5, cache_w1h, cache_read, output_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, ts, day, root, root, i, w5, w1h, rd, out, cost))
    con.commit()
    con.close()
    return path




def test_first_run_records_a_baseline_for_each_machine(mdir: Path, store: Path) -> None:
    metrics_report.collect(store=store, now=NOW)
    adopts = of_type(mdir, "adopt")
    assert sorted(str(a["ref"]) for a in adopts) == ["server-host@ab35845", "mirror-b@385ffdc"]
    assert all(a["src"] == "baseline" for a in adopts)


def test_a_changed_server_commit_emits_one_adopt_line(mdir: Path, store: Path) -> None:
    metrics_report.collect(store=store, now=NOW)
    _status(store, "5b0f8d11", "server-host", "68c779359")
    counts = metrics_report.collect(store=store, now=NOW + 300)
    fresh = [a for a in of_type(mdir, "adopt") if "src" not in a]
    assert [a["ref"] for a in fresh] == ["server-host@68c7793"]
    assert counts["adopt"] == 1
    assert fresh[0]["ts"] == "2026-09-21T12:05:00Z"


def test_an_unchanged_commit_emits_nothing(mdir: Path, store: Path) -> None:
    metrics_report.collect(store=store, now=NOW)
    n = len(raw_lines(mdir))
    assert n == 2                                       
    metrics_report.collect(store=store, now=NOW + 300)
    assert len(raw_lines(mdir)) == n


def test_a_machine_without_a_status_file_or_commit_is_skipped(mdir: Path, store: Path) -> None:
    (store / "machines" / "nostatus").mkdir()
    bad = store / "machines" / "badjson"
    bad.mkdir()
    (bad / "daemon-status.json").write_text("{not json")
    _status(store, "nocommit", "rp", "")
    metrics_report.collect(store=store, now=NOW)
    assert sorted(str(a["ref"]) for a in of_type(mdir, "adopt")) == [
        "server-host@ab35845", "mirror-b@385ffdc"]




def test_second_run_adds_no_lines(mdir: Path, store: Path, tmp_path: Path) -> None:
    db = _db(tmp_path / "t.db", [("r1", "2026-09-21T10:00:00Z", "2026-09-21", "aaaaaaaa-1", 5, 10, 0, 100, 50, 1.5)])
    metrics_report.collect(store=store, token_db=db, now=NOW)
    before = (mdir.glob("*.jsonl").__next__().read_bytes(), len(raw_lines(mdir)))
    metrics_report.collect(store=store, token_db=db, now=NOW + 300)
    assert (mdir.glob("*.jsonl").__next__().read_bytes(), len(raw_lines(mdir))) == before


def test_a_crash_between_emit_and_cursor_save_creates_no_duplicates(
        mdir: Path, store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    metrics_report.collect(store=store, now=NOW)
    _status(store, "5b0f8d11", "server-host", "68c779359")

    real_save = metrics_report._save_cursor
    calls = {"n": 0}

    def crash(path: Path, data: dict[str, object]) -> None:
        calls["n"] += 1
        raise RuntimeError("killed after emit, before the cursor was written")

    monkeypatch.setattr(metrics_report, "_save_cursor", crash)
    with pytest.raises(RuntimeError):
        metrics_report.collect(store=store, now=NOW + 300)
    assert calls["n"] >= 1
    monkeypatch.setattr(metrics_report, "_save_cursor", real_save)
    metrics_report.collect(store=store, now=NOW + 600)
    fresh = [a for a in of_type(mdir, "adopt") if "src" not in a]
    assert [a["ref"] for a in fresh] == ["server-host@68c7793"]     


def test_the_collector_makes_no_network_call(mdir: Path, store: Path, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    def no_net(*_a: object, **_k: object) -> None:
        raise AssertionError("the collector opened a socket")
    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    db = _db(tmp_path / "t.db", [("r1", "2026-09-21T10:00:00Z", "2026-09-21", "aaaaaaaa-1", 1, 2, 3, 4, 5, 0.5)])
    counts = metrics_report.collect(store=store, token_db=db, now=NOW)
    assert counts["adopt"] == 2 and counts["tokens"] == 1   




ROWS = [
    ("r1", "2026-09-20T09:00:00Z", "2026-09-20", "aaaaaaaa-1111", 10, 100, 20, 1000, 50, 1.25),
    ("r2", "2026-09-20T09:05:00Z", "2026-09-20", "aaaaaaaa-1111", 20, 0, 0, 2000, 70, 0.75),
    ("r3", "2026-09-21T10:00:00Z", "2026-09-21", "aaaaaaaa-1111", 5, 10, 0, 500, 30, 0.5),
    ("r4", "2026-09-21T10:30:00Z", "2026-09-21", "bbbbbbbb-2222", 7, 0, 5, 700, 40, 2.0),
]


def test_token_history_is_imported_once_per_day_and_session(mdir: Path, store: Path,
                                                            tmp_path: Path) -> None:
    db = _db(tmp_path / "t.db", ROWS)
    counts = metrics_report.collect(store=store, token_db=db, now=NOW)
    toks = of_type(mdir, "tokens")
    assert counts["tokens"] == 3
    by_key = {(str(t["session"]), str(t["ts"])[:10]): t for t in toks}
    past = by_key[("aaaaaaaa", "2026-09-20")]
    assert past["ts"] == "2026-09-20T23:59:59Z"           
    assert past["src"] == "baseline"
    assert (past["n"], past["in_tok"], past["out_tok"]) == (2, 30, 120)
    assert (past["cache_r"], past["cache_w"]) == (3000, 120)
    assert past["cost_usd"] == 2.0
    today = by_key[("aaaaaaaa", "2026-09-21")]
    assert today["ts"] == "2026-09-21T12:00:00Z"          


def test_later_requests_emit_only_the_delta(mdir: Path, store: Path, tmp_path: Path) -> None:
    db = _db(tmp_path / "t.db", ROWS)
    metrics_report.collect(store=store, token_db=db, now=NOW)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO requests (request_id, ts, day, session_id, root_session, input_tokens,"
                " output_tokens, cost_usd) VALUES ('r5','2026-09-21T11:00:00Z','2026-09-21',"
                "'aaaaaaaa-1111','aaaaaaaa-1111',3,9,0.25)")
    con.commit()
    con.close()
    counts = metrics_report.collect(store=store, token_db=db, now=NOW + 300)
    assert counts["tokens"] == 1
    last = of_type(mdir, "tokens")[-1]
    assert (last["n"], last["in_tok"], last["out_tok"], last["cost_usd"]) == (1, 3, 9, 0.25)


def test_token_events_sum_to_the_database_totals(mdir: Path, store: Path, tmp_path: Path) -> None:
    db = _db(tmp_path / "t.db", ROWS)
    metrics_report.collect(store=store, token_db=db, now=NOW)
    toks = of_type(mdir, "tokens")
    assert sum(int(str(t["n"])) for t in toks) == len(ROWS)
    assert round(sum(float(str(t["cost_usd"])) for t in toks), 6) == round(
        sum(r[9] for r in ROWS), 6)


def test_the_change_label_comes_from_the_claims_file(mdir: Path, store: Path,
                                                     tmp_path: Path) -> None:
    claims = tmp_path / "claims"
    claims.mkdir()
    (claims / "bbbbbbbb-2222.json").write_text(json.dumps(
        {"session": "bbbbbbbb-2222", "worktree": "table-hermetic"}))
    db = _db(tmp_path / "t.db", ROWS)
    metrics_report.collect(store=store, token_db=db, claims_dir=claims, now=NOW)
    labelled = {str(t["session"]): t.get("change") for t in of_type(mdir, "tokens")
                if str(t["ts"]).startswith("2026-09-21")}
    assert labelled["bbbbbbbb"] == "table-hermetic"
    assert labelled["aaaaaaaa"] is None


def test_a_missing_token_database_is_not_an_error(mdir: Path, store: Path, tmp_path: Path) -> None:
    counts = metrics_report.collect(store=store, token_db=tmp_path / "absent.db", now=NOW)
    assert counts["tokens"] == 0




def test_cooloff_end_is_emitted_once_after_the_window_expires(mdir: Path, store: Path) -> None:
    start = "2026-09-21T09:00:00Z"
    metrics.emit("gate_cooloff_start", ts=start, ref="daemon", dur_ms=3600000)
    metrics_report.collect(store=store, now=float(epoch("2026-09-21T09:30:00Z")))
    assert of_type(mdir, "gate_cooloff_end") == []          
    metrics_report.collect(store=store, now=float(epoch("2026-09-21T11:00:00Z")))
    ends = of_type(mdir, "gate_cooloff_end")
    assert len(ends) == 1
    assert ends[0]["ts"] == "2026-09-21T10:00:00Z"          
    assert ends[0]["n"] == epoch(start)                     
    metrics_report.collect(store=store, now=float(epoch("2026-09-21T13:00:00Z")))
    assert len(of_type(mdir, "gate_cooloff_end")) == 1


def test_events_written_by_the_collector_verify(mdir: Path, store: Path, tmp_path: Path) -> None:
    db = _db(tmp_path / "t.db", ROWS)
    metrics_report.collect(store=store, token_db=db, now=NOW)
    assert metrics.verify(mdir).ok is True
    assert len(events(mdir)) >= 5
