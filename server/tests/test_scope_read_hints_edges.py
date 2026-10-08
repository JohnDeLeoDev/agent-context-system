'test_scope_read_hints.py is locked, so these live beside it:\n  1. More child checkouts than the cap: the ones past it were never looked at, so a\n     second workspace sorting after position 50 was invisible and the root resolved.\n  2. A cwd that is itself inside a git checkout (an unregistered repo holding nested\n     clones) resolved to its clones\' workspace.\n  3. project="ws:" advised `workspace=""`, which is falsy and ignored downstream.\n  4. project="ws:NoSuch" advised `workspace="NoSuch"`, which is refused in turn.\n  5. A registered project whose display_name starts with "ws:" became unaddressable.'
import json
import subprocess

from agent_context import fstools as T
from agent_context import projects as P
from agent_context import server


def _ws_project(store, name, ws):
    T.upsert_project(store, f"gh:org/{name.lower()}", name, workspace=ws)
    return name


def _checkout(parent, name, store, project):
    d = parent / name
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
    e = store.project_entity(project)
    assert e and P._write_project_marker(str(d), e["uuid"], project)
    return d


def _tool(monkeypatch, store, fn, **kwargs):
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    return json.loads(fn(**kwargs))


def test_more_checkouts_than_the_cap_resolve_to_no_workspace(store, tmp_path):
    _ws_project(store, "A", "W")
    _ws_project(store, "B", "W")
    _ws_project(store, "C", "X")
    root = tmp_path / "root"
    _checkout(root, "a", store, "A")
    _checkout(root, "b", store, "B")
    _checkout(root, "c", store, "C")          

    assert P.resolve_workspace_root(store, str(root), max_checkouts=2) is None


def test_a_cwd_inside_a_git_checkout_is_not_a_workspace_root(store, tmp_path):
    _ws_project(store, "A", "W")
    _ws_project(store, "B", "W")
    outer = tmp_path / "outer-repo"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True, capture_output=True)
    _checkout(outer, "a", store, "A")
    _checkout(outer, "b", store, "B")

    assert P.resolve_workspace_root(store, str(outer)) is None
    assert T.get_session_context(store, str(outer)).get("workspace") is None


def test_an_empty_workspace_name_in_project_gets_advice_that_works(store, monkeypatch):
    _ws_project(store, "A", "W")

    out = _tool(monkeypatch, store, server.get_doc, path="x.md", project="ws:")

    err = out.get("error", "") if isinstance(out, dict) else ""
    assert err, out
    assert 'workspace=""' not in err, err
    assert "W" in err, err                    


def test_an_unknown_workspace_in_project_is_refused_as_unknown(store, monkeypatch):
    _ws_project(store, "A", "W")

    out = _tool(monkeypatch, store, server.get_doc, path="x.md", project="ws:NoSuch")

    err = out.get("error", "") if isinstance(out, dict) else ""
    assert "unknown workspace" in err, err
    assert 'workspace="NoSuch"' not in err, err


def test_a_project_whose_name_starts_with_ws_stays_addressable(store, monkeypatch):
    T.upsert_project(store, "gh:org/odd", "ws:odd")
    T.upsert_doc(store, "o.md", "odd body\n", project="ws:odd")

    out = _tool(monkeypatch, store, server.get_doc, path="o.md", project="ws:odd")

    assert isinstance(out, dict) and out.get("scope") == "project:ws:odd", out
