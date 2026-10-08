'T3.2 — machine-agnostic project marker (.agents/project-id).\n\nMarker wins over the git remote; absent → remote fallback (unchanged) + auto-heal\nwrites the marker; upsert_project writes the marker when a repo path is known.\nget_repo_root / get_git_remote / get_git_branch are monkeypatched so nothing runs\nreal git and all writes land in tmp_path.'

from agent_context import fstools as T
from agent_context import project_resolve
from agent_context import project_resolve as PR


def _patch_git(monkeypatch, repo_root, remote):
    
    monkeypatch.setattr(project_resolve, "get_repo_root", lambda cwd: repo_root)
    
    monkeypatch.setattr(PR, "get_git_remote", lambda cwd: remote)
    monkeypatch.setattr(PR, "get_git_branch", lambda cwd: "main")


def test_marker_preferred_over_remote(store, tmp_path, monkeypatch):
    a = T.upsert_project(store, "gh:org/alpha", "Alpha")
    T.upsert_project(store, "gh:org/beta", "Beta")
    repo = tmp_path / "repo"
    repo.mkdir()
    
    _patch_git(monkeypatch, str(repo), "git@github.com:org/beta.git")
    assert T._write_project_marker(str(repo), a["id"], "Alpha")
    r = T.resolve_project(store, str(repo))
    assert r["display_name"] == "Alpha"
    assert r["resolved_via"] == "marker"


def test_falls_back_to_remote_without_marker(store, tmp_path, monkeypatch):
    T.upsert_project(store, "gh:org/beta", "Beta")
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_git(monkeypatch, str(repo), "git@github.com:org/beta.git")
    r = T.resolve_project(store, str(repo))
    assert r["display_name"] == "Beta"
    assert r["resolved_via"] == "remote"


def test_auto_heal_writes_marker_on_remote_resolve(store, tmp_path, monkeypatch):
    b = T.upsert_project(store, "gh:org/beta", "Beta")
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_git(monkeypatch, str(repo), "git@github.com:org/beta.git")
    marker = repo / ".agents" / "project-id"
    assert not marker.exists()
    r = T.resolve_project(store, str(repo))
    assert r.get("marker_written") is True
    assert marker.exists()
    parsed = T._read_project_marker(str(repo))
    assert parsed["id"] == b["id"]
    
    r2 = T.resolve_project(store, str(repo))
    assert r2["resolved_via"] == "marker"
    assert "marker_written" not in r2


def test_no_heal_when_repo_root_unknown(store, tmp_path, monkeypatch):
    
    T.upsert_project(store, "gh:org/beta", "Beta")
    monkeypatch.setattr(project_resolve, "get_repo_root", lambda cwd: None)
    monkeypatch.setattr(PR, "get_git_remote", lambda cwd: "git@github.com:org/beta.git")
    monkeypatch.setattr(PR, "get_git_branch", lambda cwd: "main")
    r = T.resolve_project(store, str(tmp_path))
    assert r["display_name"] == "Beta"
    assert "marker_written" not in r


def test_upsert_project_writes_marker(store, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(project_resolve, "get_repo_root", lambda p: str(repo))
    res = T.upsert_project(store, "gh:org/gamma", "Gamma", local_path=str(repo))
    assert res.get("marker_written") is True
    parsed = T._read_project_marker(str(repo))
    assert parsed["id"] == res["id"]
    assert parsed["display_name"] == "Gamma"


def test_write_marker_is_idempotent(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert T._write_project_marker(str(repo), "uuid-1", "Name")
    p = repo / ".agents" / "project-id"
    first = p.read_text()
    assert T._write_project_marker(str(repo), "uuid-1", "Name")
    assert p.read_text() == first   


def test_write_marker_refuses_missing_root(store):
    assert T._write_project_marker(None, "id", "n") is False
    assert T._write_project_marker("/no/such/dir/here", "id", "n") is False
