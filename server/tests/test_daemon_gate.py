'Daemon version guard (_should_bounce) — bounce only on newer code, fail open.\n\nThese tests inject versions via monkeypatch so they never read or write the live\nruntime dir (~/Library/Logs/agent-context) or touch a real daemon.'
from agent_context import daemon


def test_should_bounce_true_when_disk_newer(monkeypatch):
    monkeypatch.setattr(daemon, "_code_version", lambda: 100.0)
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"code_version": 50.0, "pid": 1})
    assert daemon._should_bounce() is True


def test_should_not_bounce_when_disk_equal_or_older(monkeypatch):
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"code_version": 100.0, "pid": 1})
    monkeypatch.setattr(daemon, "_code_version", lambda: 100.0)
    assert daemon._should_bounce() is False
    monkeypatch.setattr(daemon, "_code_version", lambda: 90.0)
    assert daemon._should_bounce() is False


def test_should_bounce_fails_open_when_run_version_unknown(monkeypatch):
    
    monkeypatch.setattr(daemon, "_code_version", lambda: 100.0)
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: None)
    assert daemon._should_bounce() is False
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"pid": 1})  
    assert daemon._should_bounce() is False


def test_should_bounce_fails_open_when_disk_version_unknown(monkeypatch):
    monkeypatch.setattr(daemon, "_code_version", lambda: None)
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"code_version": 50.0, "pid": 1})
    assert daemon._should_bounce() is False


def test_code_version_returns_float():
    
    assert isinstance(daemon._code_version(), float)







def test_should_not_bounce_when_content_is_unchanged(monkeypatch):
    monkeypatch.setattr(daemon, "_code_version", lambda: 100.0)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "abc")
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"code_version": 50.0, "code_fingerprint": "abc", "pid": 1})
    assert daemon._should_bounce() is False


def test_should_bounce_when_content_actually_changed(monkeypatch):
    monkeypatch.setattr(daemon, "_code_version", lambda: 100.0)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "new")
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"code_version": 50.0, "code_fingerprint": "old", "pid": 1})
    assert daemon._should_bounce() is True


def test_should_bounce_keeps_mtime_behavior_without_a_recorded_fingerprint(monkeypatch):
    'A daemon started on pre-#190 code recorded no fingerprint; it must still be\n    bounceable, or a release could never reach it.'
    monkeypatch.setattr(daemon, "_code_version", lambda: 100.0)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "whatever")
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"code_version": 50.0, "pid": 1})
    assert daemon._should_bounce() is True
    
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: None)
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"code_version": 50.0, "code_fingerprint": "old", "pid": 1})
    assert daemon._should_bounce() is True


def test_fingerprint_follows_content_not_timestamps(tmp_path, monkeypatch):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(daemon, "_gate_files", lambda: [f])
    first = daemon._code_fingerprint()
    assert isinstance(first, str)

    f.touch()                                    
    assert daemon._code_fingerprint() == first

    f.write_text("x = 2\n")                      
    assert daemon._code_fingerprint() != first


def test_fingerprint_is_none_when_a_gate_file_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_gate_files", lambda: [tmp_path / "gone.py"])
    assert daemon._code_fingerprint() is None
