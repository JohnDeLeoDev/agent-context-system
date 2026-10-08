'The metrics emit library: schema, privacy, append-only chain, fail-safe, verify.\n\nCriteria C1 to C4 and C16 of the agent work metrics collector (stage 1). The log is one\nJSON line per event in `<metrics dir>/YYYY-MM.jsonl` with a hash chain in `prev`.'
import fcntl
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from metrics_fixtures import events, log_files, raw_lines

from agent_context import metrics

SRC = str(Path(__file__).resolve().parent.parent / "src")
ZERO = "0" * 64
UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


@pytest.fixture
def mdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "metrics"
    monkeypatch.setenv("AGENT_CONTEXT_METRICS_DIR", str(d))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return d


def _sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()




def test_emit_writes_one_whole_json_line(mdir: Path) -> None:
    ok = metrics.emit("phase", session="48cc5357-068b-4cad-9e29-66f754cd5732",
                      change="metrics-collector", ref="criteria_sent")
    assert ok is True
    files = log_files(mdir)
    assert len(files) == 1
    assert re.fullmatch(r"\d{4}-\d\d\.jsonl", files[0].name)
    lines = raw_lines(mdir)
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["type"] == "phase"
    assert row["session"] == "48cc5357"            
    assert row["change"] == "metrics-collector"
    assert row["ref"] == "criteria_sent"
    assert UTC.match(str(row["ts"]))
    assert row["prev"] == ZERO
    assert files[0].read_text().endswith("\n")


def test_lines_are_chained_by_sha256_of_the_previous_line(mdir: Path) -> None:
    for i in range(4):
        assert metrics.emit("gate_end", ref="precommit", dur_ms=i, ok=True)
    lines = raw_lines(mdir)
    assert json.loads(lines[0])["prev"] == ZERO
    for before, after in itertools.pairwise(lines):
        assert json.loads(after)["prev"] == _sha(before)


def test_ts_override_picks_the_month_file_and_each_file_restarts_the_chain(mdir: Path) -> None:
    assert metrics.emit("adopt", ts="2026-08-31T23:59:59Z", ref="ls@abc1234")
    assert metrics.emit("adopt", ts="2026-09-01T00:00:01Z", ref="ls@def5678")
    assert [f.name for f in log_files(mdir)] == ["2026-08.jsonl", "2026-09.jsonl"]
    for f in log_files(mdir):
        assert json.loads(f.read_text().splitlines()[0])["prev"] == ZERO


def test_session_defaults_to_the_environment_session_id(mdir: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "5d4631cd-370e-4899-9d4a-b754dd21c689")
    assert metrics.emit("phase", ref="requested")
    assert events(mdir)[0]["session"] == "5d4631cd"


def test_numeric_and_bool_fields_are_kept(mdir: Path) -> None:
    assert metrics.emit("gate_end", ref="wt-finish", dur_ms=4200, n=2273, failed=1,
                        skipped=2, reruns=0, ok=False, change="table-hermetic")
    row = events(mdir)[0]
    assert (row["dur_ms"], row["n"], row["failed"], row["skipped"], row["reruns"]) == (
        4200, 2273, 1, 2, 0)
    assert row["ok"] is False


@pytest.mark.parametrize("bad", [
    {"prompt": "do the thing"},                       
    {"ref": "x" * 121},                               
    {"ref": "has a space"},                           
    {"ref": "/home/user/.ssh/id_ed25519"},            
    {"change": "feature/branch"},                     
    {"change": "x" * 65},
    {"session": "not a session!"},
    {"ts": "2026-09-21T10:00:00+02:00"},              
    {"ts": "2026-09-21 10:00:00"},
    {"dur_ms": "12"},                                 
    {"dur_ms": True},                                 
    {"n": 1.5},
    {"ok": "yes"},
    {"src": "somewhere-else"},                        
])
def test_invalid_input_is_rejected_counted_and_not_written(mdir: Path,
                                                           bad: dict[str, Any]) -> None:
    before = metrics.rejected_count()
    fields: dict[str, Any] = {"ref": "criteria_sent"}
    fields.update(bad)
    assert metrics.emit("phase", **fields) is False
    assert raw_lines(mdir) == []
    assert metrics.rejected_count() == before + 1


def test_unknown_event_type_is_rejected(mdir: Path) -> None:
    assert metrics.emit("free_form_note", ref="x") is False
    assert raw_lines(mdir) == []
    assert metrics.rejected_count() == 1


def test_every_documented_type_is_accepted(mdir: Path) -> None:
    types = ["phase", "landed", "gate_start", "gate_end", "gate_cooloff_start",
             "gate_cooloff_end", "adopt", "tokens", "approval_asked",
             "approval_answered", "msg", "block"]
    for t in types:
        assert metrics.emit(t, ref="x") is True, t
    assert [e["type"] for e in events(mdir)] == types




CANARIES = ["SECRET CANARY prompt text", "/home/user/.ssh/id_ed25519",
            "git commit -m 'hello'", "sk-live token=abc def"]


def test_canary_free_text_never_reaches_the_log(mdir: Path) -> None:
    assert metrics.emit("phase", ref="requested")          
    for c in CANARIES:
        for field in ("ref", "change", "session", "src", "ts", "id"):
            metrics.emit("phase", **{field: c})
        metrics.emit(c, ref="x")
    assert len(raw_lines(mdir)) == 1
    blob = "".join(p.read_text() for p in mdir.glob("*") if p.is_file())
    for c in CANARIES:
        assert c not in blob


def test_cli_rejects_free_text_as_a_change_id(mdir: Path) -> None:
    from agent_context import metrics_report
    assert metrics_report.main(["phase", "metrics-collector", "requested"]) == 0   
    rc = metrics_report.main(["phase", "please summarize my prompt", "requested"])
    assert rc != 0
    assert len(raw_lines(mdir)) == 1




WRITER = (
    "import sys\n"
    "from agent_context import metrics\n"
    "for i in range(200):\n"
    "    metrics.emit('gate_end', ref='w' + sys.argv[1], n=i, ok=True)\n"
)


def test_twenty_writers_make_four_thousand_whole_lines_and_an_unbroken_chain(mdir: Path) -> None:
    env = dict(os.environ, PYTHONPATH=SRC, AGENT_CONTEXT_METRICS_DIR=str(mdir))
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(i)], env=env)
             for i in range(20)]
    for p in procs:
        assert p.wait(timeout=120) == 0
    lines = raw_lines(mdir)
    assert len(lines) == 4000
    rows = [json.loads(line) for line in lines]          
    for before, row in zip(lines, rows[1:], strict=False):
        assert row["prev"] == _sha(before)
    result = metrics.verify(mdir)
    assert result.ok is True
    assert result.lines == 4000


def test_emit_never_truncates_or_rewrites_earlier_lines(mdir: Path) -> None:
    metrics.emit("phase", ref="requested")
    first = raw_lines(mdir)[0]
    for _ in range(5):
        metrics.emit("phase", ref="criteria_sent")
    assert raw_lines(mdir)[0] == first
    assert len(raw_lines(mdir)) == 6




def test_unwritable_directory_returns_false_and_raises_nothing(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setenv("AGENT_CONTEXT_METRICS_DIR", str(tmp_path / "writable"))
    assert metrics.emit("phase", ref="requested") is True          
    monkeypatch.setenv("AGENT_CONTEXT_METRICS_DIR", str(blocker / "sub"))
    assert metrics.emit("phase", ref="requested") is False


def test_a_write_error_is_swallowed(mdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert metrics.emit("phase", ref="requested") is True          

    def boom(*_a: object, **_k: object) -> int:
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "write", boom)
    assert metrics.emit("phase", ref="requested") is False


def test_a_lock_held_by_another_process_makes_emit_give_up_fast(mdir: Path) -> None:
    metrics.emit("phase", ref="requested")
    target = log_files(mdir)[0]
    holder = subprocess.Popen(
        [sys.executable, "-c",
         ("import fcntl, sys, time\n"
          "fh = open(sys.argv[1], 'a')\n"
          "fcntl.flock(fh, fcntl.LOCK_EX)\n"
          "print('locked', flush=True)\n"
          "time.sleep(3)\n"), str(target)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        start = time.perf_counter()
        ok = metrics.emit("phase", ref="criteria_sent")
        took = time.perf_counter() - start
    finally:
        holder.kill()
        holder.wait()
    assert ok is False
    assert took < 0.6                                   
    assert len(raw_lines(mdir)) == 1                    


def test_added_time_per_emit_is_under_20ms_at_p95(mdir: Path) -> None:
    samples: list[float] = []
    for _ in range(200):
        t0 = time.perf_counter()
        wrote = metrics.emit("gate_end", ref="precommit", dur_ms=1200, n=10, ok=True)
        samples.append(time.perf_counter() - t0)
        assert wrote is True
    samples.sort()
    assert samples[int(len(samples) * 0.95) - 1] < 0.020


def test_a_typical_line_is_small_enough_for_the_size_budget(mdir: Path) -> None:
    metrics.emit("gate_end", session="48cc5357-068b", change="metrics-collector",
                 ref="wt-finish", dur_ms=241000, n=2273, failed=0, skipped=2, ok=True)
    assert len(raw_lines(mdir)[0]) < 260




def _five(mdir: Path) -> Path:
    for i in range(5):
        metrics.emit("gate_end", ref="precommit", n=i, ok=True)
    return log_files(mdir)[0]


def test_verify_passes_on_an_untouched_log(mdir: Path) -> None:
    _five(mdir)
    result = metrics.verify(mdir)
    assert result.ok is True and result.lines == 5 and result.bad_line is None


def test_verify_finds_an_edited_middle_line(mdir: Path) -> None:
    f = _five(mdir)
    lines = f.read_text().splitlines()
    lines[1] = lines[1].replace('"n":1', '"n":9')
    f.write_text("\n".join(lines) + "\n")
    result = metrics.verify(mdir)
    assert result.ok is False
    assert result.bad_file == f.name
    assert result.bad_line == 3        


def test_verify_finds_a_deleted_line(mdir: Path) -> None:
    f = _five(mdir)
    lines = f.read_text().splitlines()
    del lines[1]
    f.write_text("\n".join(lines) + "\n")
    result = metrics.verify(mdir)
    assert result.ok is False
    assert result.bad_line == 2


def test_verify_finds_an_edited_last_line(mdir: Path) -> None:
    f = _five(mdir)
    lines = f.read_text().splitlines()
    lines[-1] = lines[-1].replace('"n":4', '"n":8')
    f.write_text("\n".join(lines) + "\n")
    result = metrics.verify(mdir)
    assert result.ok is False
    assert result.bad_line == 5


def test_verify_finds_a_truncated_tail(mdir: Path) -> None:
    f = _five(mdir)
    lines = f.read_text().splitlines()
    f.write_text("\n".join(lines[:-1]) + "\n")
    result = metrics.verify(mdir)
    assert result.ok is False
    assert result.reason == "tail"


def test_verify_on_an_empty_directory_is_ok(mdir: Path) -> None:
    mdir.mkdir(parents=True)
    result = metrics.verify(mdir)
    assert result.ok is True and result.lines == 0


def test_log_files_are_mode_0640(mdir: Path) -> None:
    metrics.emit("phase", ref="requested")
    assert (log_files(mdir)[0].stat().st_mode & 0o777) == 0o640


def test_flock_is_used_on_the_log_file(mdir: Path) -> None:
    'A second process taking the same lock has to wait: proves emit locks the file\n    it appends to, which is what keeps lines whole under concurrency.'
    metrics.emit("phase", ref="requested")
    target = log_files(mdir)[0]
    with target.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        assert metrics.emit("phase", ref="criteria_sent") is False
    assert metrics.emit("phase", ref="criteria_sent") is True
