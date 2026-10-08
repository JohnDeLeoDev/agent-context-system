'test machine row ownership.'
import json
import subprocess

import pytest
from fixture_signing import signing_config_beside

from agent_context import audit, machine, paths, write_ledger
from agent_context.store import ContextStore


def _git(cwd, *args, check=True):
    return subprocess.run(("git", "-C", str(cwd), *args),
                          capture_output=True, text=True, check=check)


def _identify(root):
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for k, v in signing_config_beside(root):
        _git(root, "config", k, v)


def _daemon_write(p, text):
    'Write as the daemon does (paths.write_atomic), so the write ledger records it: only\n    a daemon write is committed (policy).'
    for parent in p.parents:
        if (parent / ".git").exists():
            write_ledger.register(str(parent))
            break
    paths.write_atomic(p, text)


def _row(root, uuid, verdict):
    p = root / "machines" / uuid / "daemon-status.json"
    _daemon_write(p, json.dumps({"machine_uuid": uuid, "verdict": verdict}) + "\n")
    return p


def _fleet(tmp_path):
    up, a, b = tmp_path / "up", tmp_path / "a", tmp_path / "b"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(up))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _identify(seed)
    (seed / "global").mkdir()
    (seed / "global" / ".keep").write_text("")
    _row(seed, "uuid-a", "healthy")
    _row(seed, "uuid-m4", "healthy")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "remote", "add", "origin", str(up))
    _git(seed, "push", "-q", "origin", "main")
    for r in (a, b):
        _git(tmp_path, "clone", "-q", str(up), str(r))
        _identify(r)
    return a, b


def test_sync_never_commits_another_machines_row(tmp_path, monkeypatch):
    a, _ = _fleet(tmp_path)
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "uuid-a")
    head_m4 = _git(a, "show", "HEAD:machines/uuid-m4/daemon-status.json").stdout
    _row(a, "uuid-m4", "stale-code")        
    _row(a, "uuid-a", "integration-failing")  

    res = ContextStore(str(a)).sync(message="daemon startup", push=False)

    assert res.get("committed")
    assert _git(a, "show", "HEAD:machines/uuid-m4/daemon-status.json").stdout == head_m4
    assert (a / "machines/uuid-m4/daemon-status.json").read_text() == head_m4
    assert "integration-failing" in _git(
        a, "show", "HEAD:machines/uuid-a/daemon-status.json").stdout


def test_conflict_on_own_row_keeps_ours(tmp_path, monkeypatch):
    a, b = _fleet(tmp_path)
    
    _row(b, "uuid-a", "stale-code")
    _git(b, "add", "-A")
    _git(b, "commit", "-qm", "sweep a's row")
    _git(b, "push", "-q", "origin", "main")

    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "uuid-a")
    _row(a, "uuid-a", "healthy-now")
    res = ContextStore(str(a)).sync(push=False)

    assert res["pull"], res
    assert res["pull_via"] == "merge+own-row"
    assert "healthy-now" in (a / "machines/uuid-a/daemon-status.json").read_text()
    assert _git(a, "ls-files", "-u").stdout == ""
    assert _git(a, "merge-base", "--is-ancestor", "origin/main", "HEAD",
                check=False).returncode == 0


def test_own_row_resolver_leaves_other_conflicts_for_a_human(tmp_path, monkeypatch):
    a, b = _fleet(tmp_path)
    for r, text in ((b, "theirs\n"), (a, "ours\n")):
        (r / "global" / "doc.md").write_text(text)
        _row(r, "uuid-a", f"{text.strip()}-row")
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "edit")
    _git(b, "push", "-q", "origin", "main")

    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "uuid-a")
    res = ContextStore(str(a)).sync(push=False)

    assert not res["pull"], res
    assert not (a / ".git" / "MERGE_HEAD").exists()   


def _deps(root, uuid, ok):
    p = root / "machines" / uuid / "deps.json"
    _daemon_write(p, json.dumps({"at": 1, "fleet": {"ok": ok, "missing": [], "broken": [],
                                                     "below_floor": []}}) + "\n")
    return p


def test_uploaded_foreign_deps_json_is_staged_and_committed(tmp_path, monkeypatch):
    
    
    a, _ = _fleet(tmp_path)
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "uuid-a")
    _deps(a, "uuid-m4", ok=False)   

    res = ContextStore(str(a)).sync(message="deps upload", push=False)

    assert res.get("committed")
    committed = _git(a, "show", "HEAD:machines/uuid-m4/deps.json").stdout
    assert json.loads(committed)["fleet"]["ok"] is False
    assert _git(a, "status", "--porcelain").stdout.strip() == ""


def test_other_foreign_drift_still_discarded_alongside_an_uploaded_deps_json(tmp_path, monkeypatch):
    a, _ = _fleet(tmp_path)
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "uuid-a")
    head_m4_status = _git(a, "show", "HEAD:machines/uuid-m4/daemon-status.json").stdout
    _row(a, "uuid-m4", "stale-code")      
    _deps(a, "uuid-m4", ok=False)         

    res = ContextStore(str(a)).sync(message="mixed", push=False)

    assert res.get("committed")
    assert _git(a, "show", "HEAD:machines/uuid-m4/daemon-status.json").stdout == head_m4_status
    assert (a / "machines/uuid-m4/daemon-status.json").read_text() == head_m4_status
    assert json.loads(
        _git(a, "show", "HEAD:machines/uuid-m4/deps.json").stdout)["fleet"]["ok"] is False


def test_uploaded_foreign_token_usage_is_committed_and_survives_the_drift_rule(tmp_path, monkeypatch):
    
    
    a, _ = _fleet(tmp_path)
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "uuid-a")
    tu = a / "machines" / "uuid-m4" / "token-usage" / "2026-09.json"
    _daemon_write(tu, '{"usage":{"columns":["day"],"rows":[["2026-09-01"]]}}')
    first = ContextStore(str(a)).sync(message="token upload", push=False)
    assert first.get("committed")
    assert "2026-09-01" in _git(a, "show", "HEAD:machines/uuid-m4/token-usage/2026-09.json").stdout

    _daemon_write(tu, '{"usage":{"columns":["day"],"rows":[["2026-09-02"]]}}')   
    second = ContextStore(str(a)).sync(message="token upload", push=False)
    assert second.get("committed")
    assert "2026-09-02" in _git(a, "show", "HEAD:machines/uuid-m4/token-usage/2026-09.json").stdout
    assert _git(a, "status", "--porcelain").stdout.strip() == ""


def _obs(oid, text):
    return {"id": oid, "observation": text, "status": "resolved"}


def test_archive_refuses_to_overwrite_a_different_record(tmp_path):
    store = ContextStore(str(tmp_path))
    audit._audit_write(store, _obs(2, "active duplicate"))
    audit._audit_write(store, _obs(2, "july record"), archive=True)

    with pytest.raises(ValueError):
        audit.archive_audit_observation(store, 2)

    kept = json.loads((tmp_path / "global/audit-observations-archive/0002.json").read_text())
    assert kept["observation"] == "july record"
    assert (tmp_path / "global/audit-observations/0002.json").exists()


def test_archive_of_an_identical_record_still_succeeds(tmp_path):
    store = ContextStore(str(tmp_path))
    audit._audit_write(store, _obs(3, "same"))
    audit._audit_write(store, _obs(3, "same"), archive=True)

    assert audit.archive_audit_observation(store, 3)["id"] == 3
    assert not (tmp_path / "global/audit-observations/0003.json").exists()
