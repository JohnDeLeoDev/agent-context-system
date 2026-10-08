'Shared helpers for the metrics collector tests (test_metrics_*.py).\n\nNot a test module. The log format under test: one JSON object per line in\n`<metrics dir>/YYYY-MM.jsonl`, keys sorted, no spaces, `prev` = sha256 (hex) of the\nprevious line\'s bytes without the newline, `"0" * 64` for the first line of a file.'
import calendar
import hashlib
import json
import time
from pathlib import Path


def epoch(iso: str) -> int:
    'epoch.'
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


def iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def log_files(mdir: Path) -> list[Path]:
    return sorted(mdir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl"))


def raw_lines(mdir: Path) -> list[str]:
    out: list[str] = []
    for f in log_files(mdir):
        out.extend(f.read_text(encoding="utf-8").splitlines())
    return out


def events(mdir: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in raw_lines(mdir)]


def of_type(mdir: Path, event_type: str) -> list[dict[str, object]]:
    return [e for e in events(mdir) if e.get("type") == event_type]


def write_chain(path: Path, rows: list[dict[str, object]]) -> None:
    'Write `rows` as a valid hash chain, the way the emit library does.'
    prev = "0" * 64
    lines: list[str] = []
    for row in rows:
        body = dict(row)
        body["prev"] = prev
        line = json.dumps(body, sort_keys=True, separators=(",", ":"))
        lines.append(line)
        prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_terse(state: Path, rows: list[tuple[str, str, str]]) -> None:
    'rows: (iso ts, event, session). Same keys as the real terse-telemetry.jsonl.'
    state.mkdir(parents=True, exist_ok=True)
    with (state / "terse-telemetry.jsonl").open("w", encoding="utf-8") as fh:
        for at, event, session in rows:
            fh.write(json.dumps({"at": at, "event": event, "session": session,
                                 "words": 10, "budget": 40, "over": False,
                                 "earned": None, "in_loop": False, "depth": 1,
                                 "transcript_bytes": 100}) + "\n")


def write_consent(state: Path, name: str, lines: list[str]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
