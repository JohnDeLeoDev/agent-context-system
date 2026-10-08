'Every writer into the synced tree goes through paths.write_atomic, and it cleans up.\n\nThese tests drive the REAL callers, not the helper alone: the point is that each\nsite now routes through it, and a later "quick local tmp+replace" at any of them\nre-fails here.'
import json
import os

import pytest

from agent_context import audit as A
from agent_context import fleet as F
from agent_context import paths as P
from agent_context import usage as U


class Boom(Exception):
    pass


def _explode(*a, **k):
    raise Boom("mid-write")


def _litter(d):
    return sorted(n for n in os.listdir(d) if ".tmp" in n or ".part" in n)


def test_write_atomic_leaves_no_litter_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    P.write_atomic(target, "first")
    monkeypatch.setattr(P.os, "replace", _explode)
    with pytest.raises(Boom):
        P.write_atomic(target, "second")
    assert target.read_text() == "first"
    assert _litter(tmp_path) == []


def test_audit_write_leaves_no_litter_on_failure(store, monkeypatch):
    rec = {"id": 7, "observation": "x", "status": "open"}
    A._audit_write(store, rec)
    d = A._audit_dir(store, False)
    monkeypatch.setattr(P.os, "replace", _explode)
    with pytest.raises(Boom):
        A._audit_write(store, {**rec, "observation": "y"})
    assert _litter(d) == []
    
    with open(A._audit_file(store, 7, False), encoding="utf-8") as fh:
        assert json.load(fh)["observation"] == "x"


def test_audit_write_fails_before_touching_disk_on_bad_json(store):
    'Serializing first means an unserializable record never creates a file.'
    d = A._audit_dir(store, False)
    os.makedirs(d, exist_ok=True)
    before = sorted(os.listdir(d))
    with pytest.raises(TypeError):
        A._audit_write(store, {"id": 8, "when": object()})
    assert sorted(os.listdir(d)) == before


def test_fleet_publish_leaves_no_litter_on_failure(tmp_path, monkeypatch):
    base = tmp_path / "store"
    (base / "machines").mkdir(parents=True)
    
    
    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(P.os, "replace", disk_full)
    ok = F.publish(base, "u1", {"verdict": "healthy", "code_current": True},
                   machine_id="m", hostname="h")
    assert ok is False, "publish never raises, and the caller still sees the failure"
    for dirpath, _dirs, files in os.walk(base):
        assert not [f for f in files if ".tmp" in f or ".part" in f], dirpath


def test_usage_publish_leaves_no_litter_on_failure(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(U, "_last_publish", 0.0)
    monkeypatch.setattr(U, "_data", {"since": 1.0, "e": {"k": {"n": 1}}})
    monkeypatch.setattr(P.os, "replace", _explode)
    U.publish_snapshot(str(root), force=True)
    for dirpath, _dirs, files in os.walk(root):
        assert not [f for f in files if ".tmp" in f or ".part" in f], dirpath


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_write_atomic_keeps_the_targets_mode(tmp_path):
    'test write atomic keeps the targets mode.'
    target = tmp_path / "run.sh"
    P.write_atomic(target, "#!/bin/sh\n")
    os.chmod(target, 0o755)
    P.write_atomic(target, "#!/bin/sh\necho hi\n")
    assert os.stat(target).st_mode & 0o7777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_write_atomic_new_file_does_not_inherit_mkstemp_0600(tmp_path):
    target = tmp_path / "new.md"
    P.write_atomic(target, "x")
    assert os.stat(target).st_mode & 0o7777 == 0o644
