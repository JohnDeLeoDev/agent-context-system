'resolve_project matching + upsert_project clobber-guard + delete_project.'
from pathlib import Path

import pytest

from agent_context import fstools as T
from agent_context import project_resolve as PR
from agent_context.store import stable_uuid



def test_resolve_project_exact_match(store, monkeypatch):
    T.upsert_project(store, "github.com:org/repo", "Repo")
    
    monkeypatch.setattr(PR, "get_git_remote", lambda cwd: "git@github.com:org/repo.git")
    monkeypatch.setattr(PR, "get_git_branch", lambda cwd: "main")
    r = T.resolve_project(store, "/some/where")
    assert "error" not in r
    assert r["display_name"] == "Repo"
    assert r["canonical_remote"] == "github.com:org/repo"
    assert r["branch"] == "main"


def test_resolve_project_suffix_match(store, monkeypatch):
    
    T.upsert_project(store, "github-work:org/repo", "WorkRepo")
    monkeypatch.setattr(PR, "get_git_remote", lambda cwd: "git@github.com:org/repo.git")
    monkeypatch.setattr(PR, "get_git_branch", lambda cwd: "dev")
    r = T.resolve_project(store, "/some/where")
    assert "error" not in r
    assert r["display_name"] == "WorkRepo"


def test_resolve_project_matches_a_pushurl_only_leg(store, monkeypatch):
    'test resolve project matches a pushurl only leg.'
    T.upsert_project(store, "example.invalid:/volume1/GitServer/Personal/example.invalid", "site")
    monkeypatch.setattr(PR, "get_git_remotes", lambda cwd: [
        "user@example.invalid:/home/user/git-server/personal/example.invalid",  
        "ssh://user@example.invalid:/volume1/GitServer/Personal/example.invalid",  
        "ssh://user@example.invalid:/volume1/GitServer/Personal/example.invalid",  
    ])
    monkeypatch.setattr(PR, "get_git_branch", lambda cwd: "main")
    r = T.resolve_project(store, "/some/where")
    assert "error" not in r
    assert r["display_name"] == "site"
    assert r["resolved_via"] == "remote"


def test_resolve_project_prefers_fetch_url_over_push(store, monkeypatch):
    'Fetch-first ordering: when both legs name a real project, the primary wins.'
    T.upsert_project(store, "hostA:org/primary", "Primary")
    T.upsert_project(store, "hostB:org/secondary", "Secondary")
    monkeypatch.setattr(PR, "get_git_remotes", lambda cwd: ["hostA:org/primary", "hostB:org/secondary"])
    monkeypatch.setattr(PR, "get_git_branch", lambda cwd: "main")
    assert T.resolve_project(store, "/some/where")["display_name"] == "Primary"


def test_resolve_project_no_match(store, monkeypatch):
    monkeypatch.setattr(PR, "get_git_remote", lambda cwd: "git@github.com:other/thing.git")
    r = T.resolve_project(store, "/nope")
    assert "error" in r



def test_upsert_project_same_remote_is_idempotent(store):
    a = T.upsert_project(store, "gh:org/repo", "P")
    b = T.upsert_project(store, "gh:org/repo", "P")
    assert a["id"] == b["id"]
    assert "error" not in b


def test_upsert_project_clobber_guard_refuses(store):
    T.upsert_project(store, "gh:org/repo", "P")
    r = T.upsert_project(store, "gh:org/OTHER", "P")
    assert "error" in r
    assert r["existing_canonical_remote"] == "gh:org/repo"
    assert r["requested_canonical_remote"] == "gh:org/OTHER"
    
    on_disk = T.list_projects(store)
    assert any(p["canonical_remote"] == "gh:org/repo" for p in on_disk)


def test_upsert_project_overwrite_pops_stale_entity(store):
    first = T.upsert_project(store, "gh:org/repo", "P")
    old_uuid = first["id"]
    assert old_uuid in store.entities
    second = T.upsert_project(store, "gh:org/OTHER", "P", overwrite=True)
    assert "error" not in second
    assert second["canonical_remote"] == "gh:org/OTHER"
    
    assert old_uuid not in store.entities
    assert stable_uuid("project", "global", "gh:org/OTHER") in store.entities



def test_delete_project(store):
    T.upsert_project(store, "gh:org/repo", "P")
    r = T.delete_project(store, "P")
    assert r == {"deleted": "P"}
    assert T.list_projects(store) == []


def test_delete_project_missing(store):
    assert T.delete_project(store, "Ghost") == {"deleted": None}


def test_register_machine_path_honours_explicit_project(store, monkeypatch, tmp_path):
    'test register machine path honours explicit project.'
    T.upsert_project(store, "example.invalid:/volume1/GitServer/Personal/example.invalid", "site")
    repo = tmp_path / "checkout"
    repo.mkdir()
    monkeypatch.setattr(PR, "get_git_remotes", lambda cwd: ["user@ls:/elsewhere/example.invalid"])
    monkeypatch.setattr(PR, "get_git_remote", lambda cwd: "user@ls:/elsewhere/example.invalid")
    monkeypatch.setattr(PR, "get_git_branch", lambda cwd: "main")
    monkeypatch.setattr(PR, "get_repo_root", lambda cwd: str(repo))
    assert "error" in T.resolve_project(store, str(repo))
    assert "error" in T.register_machine_path(store, str(repo))            
    assert "error" in T.register_machine_path(store, str(repo), "nope")    
    r = T.register_machine_path(store, str(repo), "site")
    assert "error" not in r and r["display_name"] == "site" and r["marker_written"]
    assert (repo / ".agents" / "project-id").is_file()
    assert T.resolve_project(store, str(repo))["resolved_via"] == "marker"



def test_upsert_rejects_the_string_None_as_a_project(store):
    'A JSON tool call has no Python None, so `project=None` in a doc reads as the\n    STRING "None" — which used to file the entity into projects/None/ where the\n    bootstrap\'s inbox resolver never looks.'
    with pytest.raises(ValueError) as ei:
        T.upsert_doc(store, "inbox/machines/server-host/req.md", "body", project="None")
    msg = str(ei.value)
    assert "None" in msg and "OMIT the project argument" in msg
    assert not (Path(store.root) / "projects" / "None").exists()


def test_upsert_rejects_any_unregistered_project(store):
    with pytest.raises(ValueError) as ei:
        T.upsert_memory(store, "slug", "project", "d", "b", project="NoSuchProject")
    assert "no registered project" in str(ei.value)
    assert not (Path(store.root) / "projects" / "NoSuchProject").exists()


def test_upsert_allows_a_registered_project(store):
    T.upsert_project(store, "github.com:org/repo", "Repo")
    r = T.upsert_doc(store, "notes.md", "body", project="Repo")
    assert "error" not in r
    assert (Path(store.root) / "projects" / "Repo" / "docs" / "notes.md").is_file()


def test_upsert_still_allows_an_existing_but_deregistered_project_dir(store):
    'Entities left behind by a de-registered project must stay editable, or the\n    guard would turn them read-only with no repair path.'
    T.upsert_project(store, "github.com:org/repo", "Repo")
    T.upsert_doc(store, "notes.md", "one", project="Repo")
    T.delete_project(store, "Repo")
    assert store.project_entity("Repo") is None
    r = T.upsert_doc(store, "notes.md", "two", project="Repo")
    assert "error" not in r


def test_list_entities_project_keeps_one_workspace_when_asked(store):
    'list_projects(workspace) folded into list_entities(kind="project") (policy).'
    T.upsert_project(store, "git@github.com:acme/a.git", "A", workspace="acme")
    T.upsert_project(store, "git@github.com:other/b.git", "B", workspace="other")
    T.upsert_project(store, "git@github.com:solo/c.git", "C")
    everything = T.list_entities(store, "project")
    acme = T.list_entities(store, "project", workspace="acme")
    assert sorted(p["display_name"] for p in everything) == ["A", "B", "C"]
    assert [p["display_name"] for p in acme] == ["A"]
