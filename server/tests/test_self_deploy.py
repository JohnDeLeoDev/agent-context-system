"maybe_self_redeploy decision logic.\n\nThe exec is factored behind daemon._do_exec, which every test patches with a\nrecorder — no test ever actually os.execv's. Versions, the gate, tree state, and\nnotify are all monkeypatched, so nothing touches the live store or a real daemon."
import pytest

from agent_context import daemon


@pytest.fixture
def wired(monkeypatch):
    'Patch the redeploy surface and return a dict of call recorders.'
    calls = {"exec": 0, "gate": 0, "notify": []}

    def fake_exec():
        calls["exec"] += 1

    def fake_gate(timeout=120):
        calls["gate"] += 1
        return calls["gate_result"]

    calls["gate_result"] = (True, "ok")
    monkeypatch.setattr(daemon, "_do_exec", fake_exec)
    monkeypatch.setattr(daemon, "run_gate", fake_gate)
    monkeypatch.setattr(daemon, "_notify", lambda msg: calls["notify"].append(msg))
    monkeypatch.setattr(daemon, "_server_tree_dirty", lambda: False)
    
    monkeypatch.setattr(daemon, "_ATTEMPTED_VERSIONS", {})
    monkeypatch.delenv("AGENT_CONTEXT_SELF_DEPLOY", raising=False)
    return calls


def _set_versions(monkeypatch, started, disk, started_fp="booted", disk_fp="on-disk"):
    'Wire both halves of the version identity: the mtime ORDER (started vs disk)\n    and the CONTENT (started_fp vs disk_fp). They default to different content, i.e.\n    a real release; pass the same value for both to model a re-touched tree.'
    monkeypatch.setattr(daemon, "_STARTED_VERSION", started)
    monkeypatch.setattr(daemon, "_code_version", lambda: disk)
    monkeypatch.setattr(daemon, "_STARTED_FINGERPRINT", started_fp)
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: disk_fp)



def test_newer_and_gate_pass_reexecs(wired, monkeypatch):
    _set_versions(monkeypatch, 100.0, 200.0)
    wired["gate_result"] = (True, "gate passed")
    daemon.maybe_self_redeploy()
    assert wired["exec"] == 1
    assert wired["gate"] == 1
    
    
    
    
    assert wired["notify"] == []



def test_newer_and_gate_fail_no_exec(wired, monkeypatch):
    _set_versions(monkeypatch, 100.0, 200.0)
    wired["gate_result"] = (False, "pytest failed")
    daemon.maybe_self_redeploy()
    assert wired["exec"] == 0
    assert wired["gate"] == 1
    assert "on-disk" in daemon._ATTEMPTED_VERSIONS   
    assert any("gate failed" in m for m in wired["notify"])



def test_not_newer_is_noop(wired, monkeypatch):
    _set_versions(monkeypatch, 200.0, 200.0)   
    daemon.maybe_self_redeploy()
    _set_versions(monkeypatch, 300.0, 200.0)   
    daemon.maybe_self_redeploy()
    assert wired["exec"] == 0
    assert wired["gate"] == 0



def test_same_version_not_reattempted(wired, monkeypatch):
    _set_versions(monkeypatch, 100.0, 200.0)
    wired["gate_result"] = (False, "boom")
    daemon.maybe_self_redeploy()
    daemon.maybe_self_redeploy()
    daemon.maybe_self_redeploy()
    assert wired["gate"] == 1   
    assert wired["exec"] == 0



def test_disabled_via_env_is_noop(wired, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_SELF_DEPLOY", "0")
    _set_versions(monkeypatch, 100.0, 200.0)
    daemon.maybe_self_redeploy()
    assert wired["exec"] == 0
    assert wired["gate"] == 0



def test_dirty_tree_defers_and_is_retryable(wired, monkeypatch):
    _set_versions(monkeypatch, 100.0, 200.0)
    monkeypatch.setattr(daemon, "_server_tree_dirty", lambda: True)
    daemon.maybe_self_redeploy()
    assert wired["exec"] == 0
    assert wired["gate"] == 0
    assert "on-disk" not in daemon._ATTEMPTED_VERSIONS   



def test_unknown_version_is_noop(wired, monkeypatch):
    _set_versions(monkeypatch, None, 200.0)
    daemon.maybe_self_redeploy()
    _set_versions(monkeypatch, 100.0, None)
    daemon.maybe_self_redeploy()
    assert wired["exec"] == 0
    assert wired["gate"] == 0





def test_version_key_covers_the_test_suite():
    gate, src = daemon._gate_files(), daemon._source_files()
    assert set(src) <= set(gate)
    in_tests = [f for f in gate if f.parent.name == "tests"]
    assert in_tests, "gate files must include the suite run_gate executes"
    assert any(f.name == "test_self_deploy.py" for f in in_tests)





def test_gate_failure_is_retried_after_cooldown(wired, monkeypatch):
    _set_versions(monkeypatch, 100.0, 200.0)
    wired["gate_result"] = (False, "boom")
    daemon.maybe_self_redeploy()
    daemon.maybe_self_redeploy()
    assert wired["gate"] == 1                      

    monkeypatch.setattr(daemon, "_GATE_RETRY_SECS", 0.0)   
    wired["gate_result"] = (True, "fixed")
    daemon.maybe_self_redeploy()
    assert wired["gate"] == 2                      
    assert wired["exec"] == 1                      







def test_retouched_tree_adopts_the_mtime_and_does_not_redeploy(wired, monkeypatch):
    adopted = {}
    monkeypatch.setattr(daemon, "_update_daemon_info",
                        lambda **f: adopted.update(f))
    _set_versions(monkeypatch, 100.0, 200.0, started_fp="same", disk_fp="same")

    daemon.maybe_self_redeploy()

    assert wired["gate"] == 0        
    assert wired["exec"] == 0
    assert wired["notify"] == []     
    
    
    assert daemon._STARTED_VERSION == 200.0
    assert adopted == {"code_version": 200.0}




def test_repeated_retouch_never_alerts(wired, monkeypatch):
    monkeypatch.setattr(daemon, "_update_daemon_info", lambda **f: None)
    for mtime in (200.0, 300.0, 400.0, 500.0):
        _set_versions(monkeypatch, 100.0, mtime, started_fp="same", disk_fp="same")
        daemon.maybe_self_redeploy()
    assert wired["gate"] == 0
    assert wired["exec"] == 0
    assert wired["notify"] == []





def test_failing_build_on_a_retouched_tree_alerts_once(wired, monkeypatch):
    wired["gate_result"] = (False, "boom")
    for mtime in (200.0, 300.0, 400.0):
        _set_versions(monkeypatch, 100.0, mtime, started_fp="booted", disk_fp="bad-build")
        daemon.maybe_self_redeploy()
    assert wired["gate"] == 1
    assert len(wired["notify"]) == 1
    assert wired["exec"] == 0




def test_unreadable_content_falls_back_to_the_mtime(wired, monkeypatch):
    _set_versions(monkeypatch, 100.0, 200.0, started_fp="booted", disk_fp=None)
    daemon.maybe_self_redeploy()
    assert wired["gate"] == 1
    assert wired["exec"] == 1
    assert 200.0 in daemon._ATTEMPTED_VERSIONS   






_DUMP = ("""Timeout (0:00:20)!
Thread 0x00007000abcdef (most recent call first):
  File "/store/server/tests/test_wedge_heal.py", line 88 in test_the_one_that_hangs
  File "/venv/_pytest/python.py", line 1793 in pytest_pyfunc_call
  File "/venv/_pytest/main.py", line 330 in wrap_session
  File "<frozen runpy>", line 198 in _run_module_as_main
"""
         
         
         + ("." * 72 + " [ 31%]\n") * 20)


def test_timeout_detail_keeps_the_hung_test_not_pytest_s_outer_frames():
    d = daemon._timeout_detail(_DUMP)
    assert "test_the_one_that_hangs" in d          
    assert d.startswith("Timeout (0:00:20)!")      
    
    assert "test_the_one_that_hangs" not in daemon._gate_detail(_DUMP, 1200)


def test_timeout_detail_is_capped_and_survives_a_dump_it_cannot_anchor():
    assert len(daemon._timeout_detail("x" * 5000)) == 1200
    assert daemon._timeout_detail("no dump here") == "no dump here"
    
    assert daemon._timeout_detail("noise\nThread 0x1 (most recent call first):\n  File a"
                                  ).startswith("Thread 0x1")


def test_gate_detail_keeps_the_summary_and_the_failing_test_ids():
    out = ("....\n" + "=" * 20 + " short test summary info " + "=" * 20 + "\n"
           "ERROR tests/test_x.py::test_a - subprocess.CalledProcessError: git commit\n"
           "2 passed, 1 error in 0.5s\n")
    d = daemon._gate_detail(out)
    assert "short test summary info" in d
    assert "test_x.py::test_a" in d and "CalledProcessError" in d
    assert daemon._gate_detail("no summary here") == "no summary here"
    assert len(daemon._gate_detail("x" * 5000)) == 700













class _FakeStore:
    def __init__(self):
        import threading
        self.lock = threading.RLock()


def _lock_is_free(store):
    'True when nobody holds it. Uses a non-blocking acquire from THIS thread —\n    an RLock is re-entrant, so a plain acquire() would succeed even if the leak\n    happened on this same thread and would prove nothing.'
    import threading
    out = {}

    def probe():
        got = store.lock.acquire(blocking=False)
        out["free"] = got
        if got:
            store.lock.release()

    t = threading.Thread(target=probe)
    t.start()
    t.join(5)
    return out.get("free", False)


def test_a_failed_exec_releases_the_store_lock(wired, monkeypatch):
    'test a failed exec releases the store lock.'
    _set_versions(monkeypatch, started=100.0, disk=200.0)

    def boom():
        raise FileNotFoundError(2, "No such file or directory", "/gone/python")

    monkeypatch.setattr(daemon, "_do_exec", boom)
    store = _FakeStore()
    assert _lock_is_free(store)

    with pytest.raises(FileNotFoundError):
        daemon.maybe_self_redeploy(store)

    assert _lock_is_free(store), "a failed exec leaked the store lock — every later sync would block forever"


def test_a_dirty_tree_check_that_raises_INSIDE_the_lock_also_releases(wired, monkeypatch):
    'Same hazard, the other way out of the locked region.\n\n    _server_tree_dirty is called TWICE — once before the lock is taken and once\n    inside it — so a stub that always raises would blow up on the first call and\n    never reach the locked region, proving nothing. This one succeeds first and\n    raises second, which is the call that actually sits under the lock.'
    _set_versions(monkeypatch, started=100.0, disk=200.0)
    calls = {"n": 0}

    def dirty():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("cannot stat server/")
        return False

    monkeypatch.setattr(daemon, "_server_tree_dirty", dirty)
    store = _FakeStore()

    with pytest.raises(OSError):
        daemon.maybe_self_redeploy(store)

    assert calls["n"] >= 2, "the raising call must be the one inside the lock"
    assert _lock_is_free(store)


def test_the_ordinary_dirty_tree_path_still_releases(wired, monkeypatch):
    'The one branch that already released — it must keep doing so.'
    _set_versions(monkeypatch, started=100.0, disk=200.0)
    monkeypatch.setattr(daemon, "_server_tree_dirty", lambda: True)
    store = _FakeStore()
    daemon.maybe_self_redeploy(store)
    assert _lock_is_free(store)
    assert wired["exec"] == 0





def test_retouched_tree_records_the_server_commit_it_now_runs(wired, monkeypatch):
    adopted = {}
    monkeypatch.setattr(daemon, "_update_daemon_info", lambda **f: adopted.update(f))
    monkeypatch.setattr(daemon, "_server_commit", lambda: "abc1234")
    monkeypatch.setattr(daemon, "_STARTED_SERVER_COMMIT", "0000000")
    _set_versions(monkeypatch, 100.0, 200.0, started_fp="same", disk_fp="same")

    daemon.maybe_self_redeploy()

    assert wired["exec"] == 0
    assert adopted == {"code_version": 200.0, "server_commit": "abc1234"}
    assert daemon._STARTED_SERVER_COMMIT == "abc1234"
