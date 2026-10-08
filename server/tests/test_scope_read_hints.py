'test scope read hints.'
import json
import subprocess

from agent_context import fstools as T
from agent_context import projects as P
from agent_context import server


def _ws_project(store, name="P", ws="W"):
    T.upsert_project(store, f"gh:org/{name.lower()}", name, workspace=ws)
    return name


def _tool(monkeypatch, store, fn, **kwargs):
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    return json.loads(fn(**kwargs))




def test_get_doc_miss_names_the_workspace_that_holds_it(store, monkeypatch):
    'The exact #416 call: no project, no workspace, doc only at ws:W.'
    _ws_project(store)
    T.upsert_doc(store, "board.md", "rules\n", workspace="W")

    out = _tool(monkeypatch, store, server.get_doc, path="board.md")

    assert isinstance(out, dict) and "error" in out, out
    assert "not found" in out["error"], out
    assert "ws:W" in out["error"] and 'workspace="W"' in out["error"], out


def test_get_memory_miss_names_the_workspace_that_holds_it(store, monkeypatch):
    _ws_project(store)
    T.upsert_memory(store, "ws-mem", "reference", "d", "b\n", workspace="W")

    out = _tool(monkeypatch, store, server.get_memory, slug="ws-mem")

    assert isinstance(out, dict) and 'workspace="W"' in out.get("error", ""), out


def test_get_entity_miss_names_the_scope_for_every_kind(store, monkeypatch):
    _ws_project(store)
    T.upsert_memory(store, "m", "reference", "d", "b\n", workspace="W")
    T.upsert_doc(store, "d.md", "b\n", workspace="W")
    T.upsert_skill(store, "s", "d", "b\n", workspace="W")
    T.upsert_command(store, "c", "b\n", description="d", workspace="W")
    T.upsert_script(store, "sc", "b\n", description="d", language="sh", workspace="W")
    T.upsert_hook(store, "h", "SessionStart", "b\n", description="d", workspace="W")
    T.upsert_agent_definition(store, "a", description="d", body="b\n", workspace="W")

    for kind, key in (("memory", "m"), ("doc", "d.md"), ("skill", "s"), ("command", "c"),
                      ("script", "sc"), ("hook", "h"), ("agent_definition", "a")):
        out = _tool(monkeypatch, store, server.get_entity, kind=kind, key=key)
        assert isinstance(out, dict) and 'workspace="W"' in out.get("error", ""), (kind, out)


def test_a_project_scoped_entity_is_named_with_its_project(store, monkeypatch):
    T.upsert_project(store, "gh:org/q", "Q")
    T.upsert_doc(store, "q.md", "b\n", project="Q")

    out = _tool(monkeypatch, store, server.get_doc, path="q.md")

    assert isinstance(out, dict) and 'project="Q"' in out.get("error", ""), out




def test_a_key_that_exists_nowhere_still_reads_null(store, monkeypatch):
    _ws_project(store)
    assert _tool(monkeypatch, store, server.get_doc, path="no-such.md") is None
    assert _tool(monkeypatch, store, server.get_memory, slug="no-such") is None
    assert _tool(monkeypatch, store, server.get_entity, kind="skill", key="no-such") is None




def test_a_workspace_passed_as_project_is_refused_on_every_read(store, monkeypatch):
    _ws_project(store)
    T.upsert_doc(store, "board.md", "rules\n", workspace="W")
    T.upsert_memory(store, "ws-mem", "reference", "d", "b\n", workspace="W")

    for fn, kwargs in ((server.get_doc, {"path": "board.md"}),
                       (server.get_memory, {"slug": "ws-mem"}),
                       (server.get_entity, {"kind": "doc", "key": "board.md"})):
        out = _tool(monkeypatch, store, fn, project="ws:W", **kwargs)
        assert isinstance(out, dict) and 'workspace="W"' in out.get("error", ""), (fn, out)




def test_search_docs_rows_carry_scope_and_the_fetch_argument(store):
    _ws_project(store)
    T.upsert_project(store, "gh:org/q", "Q")
    T.upsert_doc(store, "ws-board.md", "zebracorn rules\n", title="Zebracorn WS", workspace="W")
    T.upsert_doc(store, "q-board.md", "zebracorn rules\n", title="Zebracorn Q", project="Q")
    T.upsert_doc(store, "g-board.md", "zebracorn rules\n", title="Zebracorn G")

    rows = {r["path"]: r for r in T.search_docs(store, "zebracorn")}

    ws, q, g = rows["ws-board.md"], rows["q-board.md"], rows["g-board.md"]
    assert ws.get("scope") == "ws:W" and ws.get("workspace") == "W", ws
    assert q.get("scope") == "project:Q" and q.get("project") == "Q", q
    assert g.get("scope") == "global", g
    assert "workspace" not in g and "project" not in g, g


def test_search_memories_rows_carry_scope_and_the_fetch_argument(store):
    _ws_project(store)
    T.upsert_memory(store, "zebracorn-ws", "reference", "zebracorn note", "b\n", workspace="W")

    row = next(r for r in T.search_memories(store, "zebracorn") if r["slug"] == "zebracorn-ws")

    assert row.get("scope") == "ws:W" and row.get("workspace") == "W", row


def test_search_all_rows_name_the_fetch_argument(store):
    _ws_project(store)
    T.upsert_doc(store, "ws-board.md", "zebracorn rules\n", title="Zebracorn WS", workspace="W")

    row = next(r for r in T.search_all(store, "zebracorn") if r["name"] == "ws-board.md")

    assert row.get("workspace") == "W", row




def test_a_project_read_still_falls_back_to_its_workspace(store, monkeypatch):
    p = _ws_project(store)
    T.upsert_doc(store, "board.md", "rules\n", workspace="W")

    out = _tool(monkeypatch, store, server.get_doc, path="board.md", project=p)

    assert out and out.get("scope") == "ws:W", out


def test_a_workspace_read_still_reads_one_scope(store, monkeypatch):
    _ws_project(store)
    T.upsert_doc(store, "board.md", "rules\n", workspace="W")

    out = _tool(monkeypatch, store, server.get_doc, path="board.md", workspace="W")

    assert out and out.get("scope") == "ws:W", out


def test_the_python_readers_still_return_none_on_a_miss(store):
    _ws_project(store)
    T.upsert_doc(store, "board.md", "rules\n", workspace="W")
    T.upsert_memory(store, "ws-mem", "reference", "d", "b\n", workspace="W")

    assert T.get_doc(store, "board.md") is None
    assert T.get_memory(store, "ws-mem") is None
    assert T.get_entity(store, "doc", "board.md") is None




def _checkout(parent, name, store, project):
    d = parent / name
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
    e = store.project_entity(project)
    assert e and P._write_project_marker(str(d), e["uuid"], project)
    return d


def test_a_workspace_root_loads_its_workspace_at_bootstrap(store, tmp_path):
    for name in ("Api", "Web"):
        _ws_project(store, name, ws="W")
    T.upsert_instruction(store, "W Rules", "workspace body\n", workspace="W")
    T.upsert_memory(store, "w-only-memory", "reference", "a workspace memory", "b\n",
                    workspace="W")
    root = tmp_path / "Developer" / "W"
    _checkout(root, "api", store, "Api")
    _checkout(root, "web", store, "Web")

    ctx = T.get_session_context(store, str(root))

    assert ctx["project"] is None, ctx["project"]
    assert ctx.get("workspace") == "W", {k: ctx.get(k) for k in ("project", "workspace")}
    assert "W Rules" in [i.get("title") for i in ctx["instructions"]], ctx["instructions"]
    assert "w-only-memory" in json.dumps(ctx["memory_index"]), ctx["memory_index"]


def test_a_directory_holding_two_workspaces_resolves_to_neither(store, tmp_path):
    _ws_project(store, "Api", ws="W")
    _ws_project(store, "Other", ws="X")
    T.upsert_instruction(store, "W Rules", "workspace body\n", workspace="W")
    root = tmp_path / "Developer"
    _checkout(root, "api", store, "Api")
    _checkout(root, "other", store, "Other")

    ctx = T.get_session_context(store, str(root))

    assert ctx.get("workspace") is None, ctx.get("workspace")
    assert "W Rules" not in [i.get("title") for i in ctx["instructions"]]


def test_a_plain_directory_resolves_to_no_workspace(store, tmp_path):
    _ws_project(store, "Api", ws="W")
    (tmp_path / "empty").mkdir()

    ctx = T.get_session_context(store, str(tmp_path / "empty"))

    assert ctx.get("workspace") is None and ctx["project"] is None
