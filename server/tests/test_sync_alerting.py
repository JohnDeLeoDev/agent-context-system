'test sync alerting.'
import os
import subprocess
import sys

import pytest

from agent_context import daemon

_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")


@pytest.fixture(autouse=True)
def _reset_streak(monkeypatch):
    monkeypatch.setattr(daemon, "_UNHEALTHY_STREAK", 0)
    monkeypatch.setattr(daemon, "_UNHEALTHY_NOTIFIED_AT", 0.0)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(daemon, "_notify", out.append)
    return out


def test_a_blip_never_pages(sent):
    'A rebooting NAS or a flaky link fails a cycle or two. Paging for that trains\n    the reader to ignore the alert, which is worse than not sending it.'
    for i in range(daemon._UNHEALTHY_NOTIFY_CYCLES - 1):
        assert daemon.note_sync_health("push refused", now=100.0 + i) is None
    assert sent == []


def test_a_persistent_failure_pages_once_with_the_reason(sent):
    for i in range(daemon._UNHEALTHY_NOTIFY_CYCLES):
        msg = daemon.note_sync_health("push refused by 4 remote(s)", now=100.0 + i)
    assert msg is not None
    assert len(sent) == 1
    assert "push refused by 4 remote(s)" in sent[0]
    assert str(daemon._UNHEALTHY_NOTIFY_CYCLES) in sent[0]


def test_it_does_not_re_page_inside_the_throttle_window(sent):
    for i in range(daemon._UNHEALTHY_NOTIFY_CYCLES):
        daemon.note_sync_health("stuck", now=100.0 + i)
    assert len(sent) == 1
    for i in range(50):                                  
        daemon.note_sync_health("stuck", now=200.0 + i)
    assert len(sent) == 1


def test_it_pages_again_once_the_window_has_passed(sent):
    for i in range(daemon._UNHEALTHY_NOTIFY_CYCLES):
        daemon.note_sync_health("stuck", now=100.0 + i)
    assert len(sent) == 1
    
    daemon.note_sync_health(
        "stuck", now=daemon._UNHEALTHY_NOTIFIED_AT + daemon._UNHEALTHY_RENOTIFY_SECS + 1)
    assert len(sent) == 2


def test_a_healthy_cycle_re_arms_the_alert(sent):
    'The condition clearing and coming back is a NEW incident, and must page\n    again — but only after it has once more proved it is not a blip.'
    for i in range(daemon._UNHEALTHY_NOTIFY_CYCLES):
        daemon.note_sync_health("stuck", now=100.0 + i)
    assert len(sent) == 1
    assert daemon.note_sync_health(None, now=500.0) is None
    assert daemon._UNHEALTHY_STREAK == 0
    daemon.note_sync_health("stuck again", now=600.0)     
    assert len(sent) == 1
    for i in range(daemon._UNHEALTHY_NOTIFY_CYCLES - 1):
        daemon.note_sync_health("stuck again", now=601.0 + i)
    assert len(sent) == 2
    assert "stuck again" in sent[1]


def _resolve_log_path_in(stdout_target):
    'Run _live_log_path() in a subprocess whose stdout is `stdout_target`, and\n    report the answer on stderr — stdout being the thing under test.'
    code = (f"import sys; sys.path.insert(0, {_SRC!r});"
            "from agent_context import daemon;"
            "sys.stderr.write(daemon._live_log_path() or 'NONE')")
    return subprocess.run([sys.executable, "-c", code], stdout=stdout_target,
                          stderr=subprocess.PIPE, text=True).stderr


def test_live_log_path_names_the_file_stdout_actually_points_at(tmp_path):
    'Resolved from fd 1, so it is right even when the supervisor chose the name —\n    which is the whole point, since the plausible-looking file was the stale one.'
    logf = tmp_path / "daemon-supervised.log"
    with open(logf, "w") as fh:
        got = _resolve_log_path_in(fh)
    assert os.path.realpath(got) == os.path.realpath(str(logf))


def test_live_log_path_is_none_when_stdout_is_not_a_file():
    'A tty or a pipe is not a log; reporting one as a path would be a lie an\n    operator would then go and try to read.'
    assert _resolve_log_path_in(subprocess.PIPE) == "NONE"
