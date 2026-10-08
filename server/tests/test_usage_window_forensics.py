"The window resets. Nobody knows why, and after the fact the only evidence is a\nfile that looks entirely normal — which is why four months of counting can\nvanish without anything to look at. publish_snapshot already stops a local reset\nfrom destroying the fleet's history; this is the other half, the part that lets\nsomeone eventually say what did it.\n\nThe distinction these tests protect is the diagnostic one: a file that was\nUNLINKED (parent directory intact, siblings still present) versus a directory\nthat was WIPED OR MOVED out from under the process — a cleaner, a migration, an\nOS-level Application Support sweep. Those have different causes and the record\nhas to tell them apart."
import json
import os

from agent_context import usage


def _forensics_path(home):
    
    
    
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    return os.path.join(base, "agent-context", "usage-window-forensics.jsonl")


def _rows(home):
    p = _forensics_path(home)
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p, encoding="utf-8").read().splitlines() if x.strip()]


def _reset(monkeypatch, tmp_path, target):
    'Point usage at `target` under a fake HOME, with its cache cleared.'
    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", home, 1) if p.startswith("~") else p)
    
    
    
    
    
    
    monkeypatch.delenv("AGENT_CONTEXT_USAGE_FILE", raising=False)
    monkeypatch.setattr(usage, "_state_dir", lambda: target.parent)
    monkeypatch.setattr(usage, "_data", None)
    return home


def test_an_absent_file_is_recorded_with_its_parent_intact(monkeypatch, tmp_path):
    'The file alone went: parent exists and still holds its siblings.'
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    (d / "other.json").write_text("{}")
    home = _reset(monkeypatch, tmp_path, d / "usage.json")

    usage._load()

    rows = _rows(home)
    assert len(rows) == 1, "a new window started and left no record"
    r = rows[0]
    assert r["reason"] == "absent"
    assert r["parent"]["exists"] is True
    assert "other.json" in r["parent"]["entries"], \
        "siblings are what distinguish an unlinked file from a wiped directory"
    assert r["pid"] == os.getpid()


def test_a_missing_directory_is_distinguishable_from_a_missing_file(monkeypatch, tmp_path):
    'The whole directory went — a different cause, and it must read differently.'
    home = _reset(monkeypatch, tmp_path, tmp_path / "gone" / "usage.json")

    usage._load()

    r = _rows(home)[0]
    assert r["reason"] == "absent"
    assert r["parent"]["exists"] is False, \
        "a wiped parent must not look like an unlinked file"
    assert r["parent"].get("error")


def test_an_unreadable_file_is_recorded_but_is_NOT_a_reset(monkeypatch, tmp_path):
    "Corrupt-but-present keeps counting from the file's mtime.\n\n    Recorded under its own reason so it is never mistaken for the reset this\n    observation is chasing: the window does not restart here."
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    f = d / "usage.json"
    f.write_text("{ this is not json")
    home = _reset(monkeypatch, tmp_path, f)

    data = usage._load()

    r = _rows(home)[0]
    assert r["reason"] == "unparseable"
    assert data["since"] == os.stat(f).st_mtime, \
        "an unparseable file must keep counting from its own mtime"


def test_a_healthy_file_records_nothing(monkeypatch, tmp_path):
    'No new window, no forensic row — this must not log on every start.'
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    f = d / "usage.json"
    f.write_text(json.dumps({"since": 1000.0, "e": {}}))
    home = _reset(monkeypatch, tmp_path, f)

    usage._load()

    assert _rows(home) == []


def test_a_redirected_usage_file_records_nothing(monkeypatch, tmp_path):
    'AGENT_CONTEXT_USAGE_FILE means "this is not the fleet\'s real file".\n\n    Without this gate a single `pytest -q` wrote 32 rows into the real forensic\n    log, every one a pytest tmpdir starting its perfectly normal first window.\n    The one row that will eventually matter has to be findable among them.'
    home = _reset(monkeypatch, tmp_path, tmp_path / "state" / "usage.json")
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE",
                       str(tmp_path / "redirected" / "usage.json"))

    usage._load()

    assert _rows(home) == [], "a redirected file must not reach the real log"


def test_forensics_never_raises(monkeypatch, tmp_path):
    'Instrumentation must never be why a session cannot count a read.'
    home = _reset(monkeypatch, tmp_path, tmp_path / "state" / "usage.json")
    monkeypatch.setattr(usage.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

    data = usage._load()          

    assert data["e"] == {}
    assert _rows(home) == []
