'Each of these cost real time or real work before it was understood, and each was\ninvisible until someone happened to look. The tests exist so the specific failure\ncannot come back silently.'
import json
import os
import threading
import time

import pytest
from narrow import notnone

from agent_context import daemon
from agent_context import fstools as T



def test_script_description_changes_without_resending_the_body(store):
    'The whole point: a 560-line script should not have to be retyped to fix a\n    one-line description (the token cost is real and so is the drift risk).'
    T.upsert_script(store, "s", "#!/bin/sh\necho original", description="old")
    out = notnone(T.upsert_script(store, "s", None, description="new"))
    assert out["description"] == "new"
    assert out["script_body"] == "#!/bin/sh\necho original"


def test_omitting_the_body_preserves_it_verbatim(store):
    body = "line1\nline2\n\n  indented\n"
    T.upsert_script(store, "s", body, description="d")
    T.upsert_script(store, "s", None, description="d2")
    assert notnone(T.get_script(store, "s"))["script_body"] == body


def test_omitting_the_description_preserves_it(store):
    'Symmetry: neither field should be blanked by omitting the other.'
    T.upsert_script(store, "s", "a", description="keep me")
    T.upsert_script(store, "s", "b")
    assert notnone(T.get_script(store, "s"))["description"] == "keep me"


def test_doc_retitles_without_resending_and_keeps_the_title_field(store):
    'Docs carry `title`, not `description` — passing the wrong meta_key would\n    silently blank the very field being preserved.'
    T.upsert_doc(store, "d.md", "# Body\n\ncontent", title="Original")
    T.upsert_doc(store, "d.md", None, title="Renamed")
    got = notnone(T.get_doc(store, "d.md"))
    assert got["title"] == "Renamed"
    assert "content" in got["body"]
    T.upsert_doc(store, "d.md", "# Body\n\nchanged")          
    assert notnone(T.get_doc(store, "d.md"))["title"] == "Renamed"


def test_command_and_skill_take_the_same_contract(store):
    T.upsert_command(store, "c", "cmd body", description="c1")
    assert notnone(T.upsert_command(store, "c", None, description="c2"))["description"] == "c2"
    assert notnone(T.get_command(store, "c"))["body"] == "cmd body"

    T.upsert_skill(store, "k", "s1", "skill body")
    T.upsert_skill(store, "k", "s2", None)
    assert notnone(T.get_skill(store, "k"))["body"] == "skill body"


def test_hook_keeps_its_existing_behaviour(store):
    'upsert_hook already worked this way; refactoring onto the shared helper\n    must not change it.'
    T.upsert_hook(store, "h", "Stop", "#!/bin/sh\nexit 0", description="h1")
    out = notnone(T.upsert_hook(store, "h", "Stop", None, description="h2"))
    assert out["description"] == "h2"
    assert out["script_body"] == "#!/bin/sh\nexit 0"


def test_omitting_the_body_on_a_NEW_entity_is_an_error(store):
    'Never create a husk: a description with no content is worse than a refusal.'
    for out in (notnone(T.upsert_script(store, "nope", None, description="d")),
                notnone(T.upsert_command(store, "nope", None, description="d")),
                notnone(T.upsert_doc(store, "nope.md", None, title="t")),
                notnone(T.upsert_hook(store, "nope", "Stop", None, description="d"))):
        assert "error" in out and "not found" in out["error"]




def _health(monkeypatch, *, attempt_ago, success_ago, wedge=None):
    now = time.time()
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", now - attempt_ago)
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", now - success_ago)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ERROR", None)
    monkeypatch.setattr(daemon, "_read_daemon_info", lambda: {"pid": 1})
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: wedge)
    monkeypatch.setattr(daemon, "_sync_stall_secs", lambda: 900.0)
    return daemon.get_health()


def test_cycling_loop_that_cannot_push_is_alive_not_dead(monkeypatch):
    'THE misdiagnosis this fixes. Pushes had failed for 7.9h so `stalled` was\n    true, and it was read as a dead thread — but the loop was cycling every 300s\n    and was four minutes from self-redeploying when it got restarted.'
    h = _health(monkeypatch, attempt_ago=60, success_ago=28_000)
    assert h["loop_alive"] is True
    assert h["stalled"] is True                  
    assert h["verdict"] == "integration-failing"


def test_loop_that_stopped_attempting_is_reported_dead(monkeypatch):
    'test loop that stopped attempting is reported dead.'
    h = _health(monkeypatch, attempt_ago=34_000, success_ago=34_000)
    assert h["loop_alive"] is False
    assert h["verdict"] == "loop-dead"


def test_healthy_daemon_says_so(monkeypatch):
    h = _health(monkeypatch, attempt_ago=30, success_ago=30)
    assert h["loop_alive"] is True
    assert h["stalled"] is False
    assert h["verdict"] == "healthy"


def test_a_wedged_tree_is_integration_failing_not_loop_dead(monkeypatch):
    'The loop runs fine; the tree blocks commits. Restarting would not help.'
    h = _health(monkeypatch, attempt_ago=30, success_ago=30, wedge="rebase in progress")
    assert h["loop_alive"] is True
    assert h["verdict"] == "integration-failing"
    assert "rebase in progress" in h["last_sync_error"]


def test_a_freshly_started_daemon_is_starting_not_dead(monkeypatch):
    'Caught live: a daemon 20s old reported verdict=loop-dead because no cycle\n    had completed yet, so every timestamp was null. That is the exact false\n    signal this verdict exists to prevent, and any watchdog acting on it would\n    restart a perfectly healthy daemon in a loop.'
    now = time.time()
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", None)
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", None)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ERROR", None)
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"pid": 1, "started_at": now - 20})
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_sync_stall_secs", lambda: 900.0)

    h = daemon.get_health()
    assert h["verdict"] == "starting"
    assert h["loop_alive"] is True


def test_a_daemon_past_its_grace_window_with_no_cycle_is_dead(monkeypatch):
    "The other side: never let 'starting' become a permanent excuse."
    now = time.time()
    monkeypatch.setattr(daemon, "_LAST_SYNC_ATTEMPT", None)
    monkeypatch.setattr(daemon, "_LAST_SUCCESSFUL_SYNC", None)
    monkeypatch.setattr(daemon, "_LAST_SYNC_ERROR", None)
    monkeypatch.setattr(daemon, "_read_daemon_info",
                        lambda: {"pid": 1, "started_at": now - 5000})
    monkeypatch.setattr(daemon, "_working_tree_wedge", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_sync_stall_secs", lambda: 900.0)

    h = daemon.get_health()
    assert h["verdict"] == "loop-dead"
    assert h["loop_alive"] is False


def test_seconds_since_attempt_is_exposed(monkeypatch):
    h = _health(monkeypatch, attempt_ago=120, success_ago=120)
    assert 110 < h["seconds_since_attempt"] < 130




def test_daemon_info_is_scoped_per_port(monkeypatch, tmp_path):
    'test daemon info is scoped per port.'
    monkeypatch.setattr(daemon, "_state_dir", lambda: tmp_path)

    monkeypatch.setattr(daemon, "PORT", 8765)
    canonical = daemon._info_path()
    monkeypatch.setattr(daemon, "PORT", 8799)
    other = daemon._info_path()

    assert canonical.name == "daemon.info"          
    assert other != canonical
    assert "8799" in other.name


def test_a_second_instance_cannot_touch_the_canonical_record(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(daemon, "PORT", 8765)
    daemon._info_path().write_text(json.dumps({"pid": 111, "code_version": 1.0}))

    monkeypatch.setattr(daemon, "PORT", 8799)       
    monkeypatch.setattr(daemon, "_STARTED_VERSION", 2.0)
    monkeypatch.setattr(daemon, "_STARTED_AT", 0.0)
    monkeypatch.setattr(daemon, "_STARTED_FINGERPRINT", "x")
    daemon.write_daemon_info()

    monkeypatch.setattr(daemon, "PORT", 8765)
    still = json.loads(daemon._info_path().read_text())
    assert still["pid"] == 111, "a second instance overwrote the live record"












def test_sync_loop_accepts_the_boot_pull_and_does_it_first(monkeypatch):
    "The boot pull moved OFF the path to uvicorn's bind: inline it kept the\n    port closed for 2m48s, and Claude Code drops an MCP server that cannot answer\n    initialize within 30s. Verify the loop performs it rather than the caller."
    from agent_context import server as S
    calls = []

    class _Store:
        root = "/tmp/nope"

        def sync(self, **kw):
            calls.append(kw)
            raise RuntimeError("stop the loop here")

    monkeypatch.setattr(S.time, "sleep", lambda *_: (_ for _ in ()).throw(SystemExit))
    with pytest.raises(SystemExit):
        S._sync_loop(_Store(), interval=0, boot_pull=True)
    assert calls and calls[0].get("push") is False
    assert calls[0].get("message") == "daemon startup"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_get_conn_returns_before_the_boot_pull_finishes(tmp_path, monkeypatch):
    'test get conn returns before the boot pull finishes.'
    from agent_context import server as S

    started = threading.Event()
    finished = threading.Event()

    class _SlowStore:
        root = str(tmp_path)
        lock = threading.RLock()

        def sync(self, **kw):
            started.set()
            time.sleep(3.0)             
            finished.set()
            return {}

    monkeypatch.setattr(S, "_store", None)
    monkeypatch.setattr(S, "ContextStore", lambda *a, **k: _SlowStore())
    monkeypatch.setattr(S, "ensure_precommit_gate", lambda *a, **k: None)
    monkeypatch.setattr(S, "_log_integrity", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "start_watchdog", lambda *a, **k: None)
    monkeypatch.delenv("AGENT_CONTEXT_NO_SYNC", raising=False)

    t0 = time.monotonic()
    S._get_conn()
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"_get_conn blocked {elapsed:.1f}s on the boot pull"
    assert started.wait(2.0), "the boot pull never ran"
    assert not finished.is_set(), "the pull finished inline — it is still blocking"
    
    
    
    
    
    loops = [t for t in threading.enumerate() if getattr(t, "_target", None) is S._sync_loop]
    assert loops, "the boot pull thread was not found"
    monkeypatch.setattr(S.time, "sleep", lambda *_: (_ for _ in ()).throw(SystemExit))
    assert finished.wait(10.0), "the boot pull never finished"
    for t in loops:
        t.join(10.0)
    assert not any(t.is_alive() for t in loops), "the sync loop is still running after the test"
    monkeypatch.setattr(S, "_store", None)


def test_cycle_watchdog_arms_and_disarms_without_raising(tmp_path, monkeypatch):
    'A watchdog that can raise would kill the loop it exists to observe.'
    from agent_context import server as S
    monkeypatch.setattr(daemon, "_log_dir", lambda: tmp_path)
    S._arm_cycle_watchdog(30.0)
    S._disarm_cycle_watchdog()
    S._disarm_cycle_watchdog()                    


def test_cycle_watchdog_is_silent_when_nothing_hangs(tmp_path, monkeypatch):
    'Arming happens every cycle; only a TIMEOUT may write. A header written at\n    arm time would fill the log with entries for cycles that were fine.'
    from agent_context import server as S
    monkeypatch.setattr(daemon, "_log_dir", lambda: tmp_path)
    S._arm_cycle_watchdog(30.0)
    S._disarm_cycle_watchdog()
    hang_log = tmp_path / "sync-hang.log"
    assert not hang_log.exists() or hang_log.read_text() == ""
