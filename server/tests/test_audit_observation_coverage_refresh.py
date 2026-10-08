"resolve/update_audit_observation kick off `observation-coverage.py --health` right\nafter a write that can change what it reports, so sync-fault-notice.py stops citing a\nstale verdict for up to an hour (policy task 3): the janitor's hourly sweep only\nre-runs invariant-check, and the SessionStart probe (invariant-probe.py) may not run\nagain for a while."
import os

from agent_context import audit
from agent_context import fstools as T


def _seed_script(store):
    scripts = os.path.join(store.root, "global", "scripts")
    os.makedirs(scripts, exist_ok=True)
    path = os.path.join(scripts, "observation-coverage.py")
    with open(path, "w") as fh:
        fh.write("#!/usr/bin/env python3\n")
    return path


def _spy(monkeypatch):
    'Record only OUR OWN spawn (sys.executable ... --health) and pass every other\n    Popen call through: audit.subprocess IS the real module, and _audit_next_id\n    shells out to git through subprocess.run (which calls Popen internally), so a\n    blanket patch would break that call too.'
    calls = []
    real_popen = audit.subprocess.Popen

    def fake_popen(argv, **kw):
        if argv[0] == audit.sys.executable:      
            calls.append((argv, kw))

            class _P:
                def wait(self):
                    return 0
            return _P()
        return real_popen(argv, **kw)
    monkeypatch.setattr(audit.subprocess, "Popen", fake_popen)
    return calls


def _file(store):
    return T.add_audit_observation(store, "a defect worth recording", scope="universal",
                                   evidence="file.py:1 — quoted line")


def test_resolving_kicks_off_a_refresh(store, monkeypatch):
    _seed_script(store)
    oid = _file(store)["id"]
    spy = _spy(monkeypatch)
    T.resolve_audit_observation(store, oid, resolution_note="fixed")
    assert len(spy) == 1
    argv, kw = spy[0]
    assert argv[-1] == "--health"
    assert argv[-2].endswith("observation-coverage.py")
    assert kw["env"]["AGENT_CONTEXT_SERVER_TASK"] == "1"
    assert kw["env"]["AGENT_CONTEXT_STORE"] == store.root


def test_recording_a_recurrence_kicks_off_a_refresh(store, monkeypatch):
    _seed_script(store)
    oid = _file(store)["id"]
    spy = _spy(monkeypatch)
    T.update_audit_observation(store, oid, recurred=True)
    assert len(spy) == 1


def test_a_status_change_kicks_off_a_refresh(store, monkeypatch):
    _seed_script(store)
    oid = _file(store)["id"]
    spy = _spy(monkeypatch)
    T.update_audit_observation(store, oid, status="open")
    assert len(spy) == 1


def test_an_unrelated_update_does_not_kick_off_a_refresh(store, monkeypatch):
    _seed_script(store)
    oid = _file(store)["id"]
    spy = _spy(monkeypatch)
    T.update_audit_observation(store, oid, note="just a note")
    assert spy == []


def test_adding_a_new_observation_does_not_kick_off_a_refresh(store, monkeypatch):
    'A fresh observation starts at recurrences=0, so it cannot appear as a coverage\n    gap by itself; no refresh is needed on the add path.'
    _seed_script(store)
    spy = _spy(monkeypatch)
    _file(store)
    assert spy == []


def test_a_missing_script_is_a_silent_no_op(store, monkeypatch):
    oid = _file(store)["id"]
    spy = _spy(monkeypatch)
    T.resolve_audit_observation(store, oid, resolution_note="fixed")
    assert spy == []
