'get_session_context inbox surfacing + open/acknowledged filtering.'
import socket

from agent_context import fstools as T
from agent_context import projects as P


def _raw_memory(store, slug, description, project=None):
    'Write a memory BELOW the guard. upsert_memory refuses over-length descriptions\n    since build 17; the hygiene report must still find the ones written before that.'
    store.upsert("memory", slug, {"memory_type": "reference", "description": description,
                                  "origin": "agent", "load_behavior": "always"},
                 body="b", project=project)


def _set_status(store, path, status, project=None):
    "Stamp a doc entity's frontmatter `status` (upsert_doc doesn't set one)."
    e = store.get("doc", path, project)
    e["status"] = status


def test_inbox_includes_open_and_acknowledged(store):
    host = socket.gethostname()
    base = f"inbox/machines/{host}/"
    T.upsert_doc(store, base + "a.md", "body-a", title="A")
    T.upsert_doc(store, base + "b.md", "body-b", title="B")
    _set_status(store, base + "a.md", "open")
    _set_status(store, base + "b.md", "acknowledged")
    ctx = T.get_session_context(store, "/no/project")
    paths = {i["path"] for i in ctx["inbox"]}
    assert base + "a.md" in paths
    assert base + "b.md" in paths


def test_inbox_excludes_done_declined_and_archive(store):
    host = socket.gethostname()
    base = f"inbox/machines/{host}/"
    T.upsert_doc(store, base + "done.md", "x", title="Done")
    T.upsert_doc(store, base + "declined.md", "x", title="Declined")
    T.upsert_doc(store, base + "archive/old.md", "x", title="Old")
    _set_status(store, base + "done.md", "done")
    _set_status(store, base + "declined.md", "declined")
    _set_status(store, base + "archive/old.md", "open")  
    ctx = T.get_session_context(store, "/no/project")
    paths = {i["path"] for i in ctx["inbox"]}
    assert base + "done.md" not in paths
    assert base + "declined.md" not in paths
    assert not any("archive/" in p for p in paths)


def test_inbox_items_omit_bodies(store):
    host = socket.gethostname()
    base = f"inbox/machines/{host}/"
    T.upsert_doc(store, base + "m.md", "SECRET BODY", title="M")
    _set_status(store, base + "m.md", "open")
    ctx = T.get_session_context(store, "/no/project")
    item = next(i for i in ctx["inbox"] if i["path"] == base + "m.md")
    assert set(item.keys()) == {"path", "title", "status"}
    assert "body" not in item


def test_inbox_default_status_is_open(store):
    
    host = socket.gethostname()
    base = f"inbox/machines/{host}/"
    T.upsert_doc(store, base + "nostatus.md", "x", title="NoStatus")
    ctx = T.get_session_context(store, "/no/project")
    assert any(i["path"] == base + "nostatus.md" for i in ctx["inbox"])



def test_open_observations_are_compact_not_full_bodies(store):
    
    long_obs = "This is a long observation about a subtle bug. " * 20  
    T.add_audit_observation(store, long_obs, scope="universal", project=None, evidence="f.py:1",
                            severity="high")
    ctx = T.get_session_context(store, "/no/project")
    obs = ctx["open_audit_observations"]
    
    rows = obs.splitlines()
    assert len(rows) == 1
    row = rows[0]
    assert row.startswith("#1 high universal ")
    assert "evidence" not in row and "f.py:1" not in row      
    assert len(row.split(" — ", 1)[1]) <= 140
    assert len(row) < 200


def _block(store, cwd="/no/project"):
    return T.get_session_context(store, cwd)["memory_index"]["global"]


def test_lazy_memory_is_rostered_by_slug_not_dropped(store):
    T.upsert_memory(store, "always-one", "reference", "loaded every session", "b", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "lazy-one", "reference", "archival prose", "b", project=None,
                    load_behavior="lazy")
    b = _block(store)
    assert "always-one — loaded every session" in b["rows"]
    assert "lazy-one" not in b["rows"]          
    assert "lazy-one" in b["lazy"].split()      
    assert "archival prose" not in b["lazy"]
    assert T.get_memory(store, "lazy-one", project=None) is not None  


def test_set_memory_load_behavior_tiers_in_and_out(store):
    T.upsert_memory(store, "m", "reference", "d", "the body", project=None,
                    load_behavior="always")
    loaded = lambda: "m — d" in _block(store)["rows"]
    assert loaded()
    T.set_memory_load_behavior(store, "m", "lazy", project=None)
    assert not loaded()
    assert "m" in _block(store)["lazy"].split()
    
    assert T.get_memory(store, "m", project=None)["body"] == "the body"
    T.set_memory_load_behavior(store, "m", "always", project=None)
    assert loaded()


def test_over_budget_rows_demote_to_roster_rather_than_vanish(store):
    
    for i in range(200):
        T.upsert_memory(store, f"m-{i:03}", "project", "x" * 130, "b", project=None,
                        load_behavior="always")
    b = _block(store)
    assert "over_budget" in b
    assert len(b["rows"]) <= T._MEM_ROWS_BUDGET
    seen = {ln.split(" ")[1] for ln in b["rows"].splitlines()} | set(b["lazy"].split())
    assert seen == {f"m-{i:03}" for i in range(200)}  


def test_upsert_warns_once_the_scope_outgrows_its_budget(store):
    r = T.upsert_memory(store, "first", "project", "d", "b", project=None,
                        load_behavior="always")
    assert "warning" not in r
    for i in range(200):
        r = T.upsert_memory(store, f"bulk-{i:03}", "project", "x" * 130, "b", project=None,
                            load_behavior="always")
    assert "budget" in r["warning"] and "load_behavior='lazy'" in r["warning"]


def test_instructions_carry_body_without_db_metadata(store):
    T.upsert_instruction(store, "Rule", "the mandatory body", project=None, load_behavior="always")
    instr = T.get_session_context(store, "/no/project")["instructions"]
    row = next(i for i in instr if i["title"] == "Rule")
    assert row["body"] == "the mandatory body"      
    assert set(row.keys()) == {"title", "body"}     



def test_clean_index_costs_nothing(store):
    T.upsert_memory(store, "fine", "reference", "a short hook", "b", project=None)
    assert "index_health" not in T.get_session_context(store, "/no/project")


def test_long_description_surfaces_in_the_session(store):
    _raw_memory(store, "fat", "x" * 200)
    h = T.get_session_context(store, "/no/project")["index_health"]
    assert h["long_descriptions"] == ["fat"]
    assert "description=" in h["fix"]
    assert "x" * 200 not in str(h)   


def test_health_is_scoped_to_this_session(store, monkeypatch):
    T.upsert_project(store, "gh:org/a", "A")
    T.upsert_project(store, "gh:org/b", "B")
    _raw_memory(store, "fat", "x" * 200, project="B")
    
    monkeypatch.setattr(P, "resolve_project", lambda s, cwd: {"display_name": "A"})
    assert "index_health" not in T.get_session_context(store, "/a")
    monkeypatch.setattr(P, "resolve_project", lambda s, cwd: {"display_name": "B"})
    assert T.get_session_context(store, "/b")["index_health"]["long_descriptions"] == ["fat"]


def test_over_ceiling_reported_when_instructions_alone_blow_the_limit(store):
    
    T.upsert_instruction(store, "Huge", "x" * (T._BOOTSTRAP_CEILING + 1), project=None)
    h = T.get_session_context(store, "/no/project")["index_health"]
    assert "over_ceiling" in h
    assert "over_budget_scopes" not in h


def test_prune_candidates_need_both_stabilized_and_quiet(store):
    T.upsert_memory(store, "old-done", "project", "Phase B COMPLETE, landed on main", "b",
                    project=None, load_behavior="always")
    T.upsert_memory(store, "live-done", "project", "Phase C COMPLETE, landed on main", "b",
                    project=None, load_behavior="always")
    T.upsert_memory(store, "old-live", "project", "ongoing migration work", "b", project=None,
                    load_behavior="always")
    store.get("memory", "old-done", None)["updated_at"] = "2020-01-01T00:00:00Z"
    store.get("memory", "old-live", None)["updated_at"] = "2020-01-01T00:00:00Z"
    h = T.get_session_context(store, "/no/project")["index_health"]
    assert h["prune_candidates"] == ["old-done"]      
    
    
    assert "delete_entity" not in h["fix"]
    assert "load_behavior='lazy'" in h["fix"]


def test_lazy_memories_are_never_prune_candidates(store):
    T.upsert_memory(store, "archived", "project", "COMPLETE, deployed", "b", project=None,
                    load_behavior="lazy")
    store.get("memory", "archived", None)["updated_at"] = "2020-01-01T00:00:00Z"
    assert "index_health" not in T.get_session_context(store, "/no/project")


def test_routine_observations_are_counted_not_loaded(store):
    'normal/low rows never ride in the bootstrap; blocker/high do, worst first.'
    T.add_audit_observation(store, "routine thing", scope="universal", project=None, evidence="e")
    T.add_audit_observation(store, "low thing", scope="universal", project=None, evidence="e",
                            severity="low")
    T.add_audit_observation(store, "high thing", scope="universal", project=None, evidence="e",
                            severity="high")
    T.add_audit_observation(store, "blocker thing", scope="universal", project=None, evidence="e",
                            severity="blocker")
    ctx = T.get_session_context(store, "/no/project")
    rows = ctx["open_audit_observations"].splitlines()
    assert [r.split(" — ", 1)[1] for r in rows] == ["blocker thing", "high thing"]
    assert rows[0].startswith("#4 blocker universal ")
    more = ctx["open_audit_observations_more"]
    assert more["routine_in_scope"] == 2
    assert more["older_in_scope"] == 0
    assert "list_audit_observations" in more["fetch"]


def test_unmatched_git_checkout_gets_a_project_warning(store, monkeypatch):
    'test unmatched git checkout gets a project warning.'
    from agent_context import project_resolve as PR
    monkeypatch.setattr(PR, "get_repo_root", lambda cwd: "/repo")
    monkeypatch.setattr(PR, "get_git_remotes", lambda cwd: ["git@github.com:org/unknown.git"])
    monkeypatch.setattr(PR, "get_git_remote", lambda cwd: "git@github.com:org/unknown.git")
    ctx = T.get_session_context(store, "/repo/sub")
    assert ctx["project"] is None
    assert "org/unknown" in ctx["project_warning"]
    assert "register_path" in ctx["project_warning"]
    assert "register_machine_path" not in ctx["project_warning"]


def test_no_project_warning_outside_git(store, monkeypatch):
    from agent_context import project_resolve as PR
    monkeypatch.setattr(PR, "get_repo_root", lambda cwd: None)
    ctx = T.get_session_context(store, "/no/project")
    assert "project_warning" not in ctx


def test_audit_digest_is_rendered_live_and_supersedes_a_stored_snapshot(store):
    'test audit digest is rendered live and supersedes a stored snapshot.'
    host = socket.gethostname()
    short = host.split(".")[0]
    path = f"inbox/machines/{short}/audit-digest.md"
    T.upsert_doc(store, path, "STALE SNAPSHOT", title="Audit digest for X")
    T.add_audit_observation(store, "routine thing", scope="universal", project=None, evidence="e")
    ctx = T.get_session_context(store, "/no/project")
    rows = [i for i in ctx["inbox"] if i["path"] == path]
    assert len(rows) == 1 and rows[0]["generated"] is True
    assert "1 observation(s)" in rows[0]["title"]
    doc = T.get_doc(store, path)
    assert doc["generated"] is True and "STALE" not in doc["body"] and "| #1 |" in doc["body"]
    T.resolve_audit_observation(store, 1, resolution_note="done")
    ctx = T.get_session_context(store, "/no/project")
    assert not [i for i in ctx["inbox"] if i["path"] == path]   
    assert "nothing open" in T.get_doc(store, path)["body"]


def test_inbox_status_is_read_from_the_request_body(store):
    'test inbox status is read from the request body.'
    host = socket.gethostname()
    base = f"inbox/machines/{host}/"
    T.upsert_doc(store, base + "ack.md", "---\nfrom: a\nto: b\nstatus: acknowledged\n---\n\nbody", title="Ack")
    T.upsert_doc(store, base + "done.md", "---\nfrom: a\nto: b\nstatus: done\n---\n\nbody", title="Done")
    T.upsert_doc(store, base + "plain.md", "no frontmatter at all", title="Plain")
    rows = {i["path"]: i["status"] for i in T.get_session_context(store, "/no/project")["inbox"]}
    assert rows[base + "ack.md"] == "acknowledged"
    assert rows[base + "plain.md"] == "open"
    assert base + "done.md" not in rows


def test_the_store_checkout_itself_gets_no_project_warning(store, monkeypatch):
    'test the store checkout itself gets no project warning.'
    import os

    from agent_context import project_resolve as PR
    monkeypatch.setattr(PR, "get_repo_root", lambda cwd: store.root)
    monkeypatch.setattr(PR, "get_git_remotes",
                        lambda cwd: ["git@github.com:org/agent-context.git"])
    monkeypatch.setattr(PR, "get_git_remote", lambda cwd: "git@github.com:org/agent-context.git")
    ctx = T.get_session_context(store, os.path.join(store.root, "server"))
    assert ctx["project"] is None
    assert "project_warning" not in ctx


def test_bootstrap_reports_this_machines_own_daemon_when_it_is_not_syncing(store, monkeypatch):
    'The fleet rows are what OTHER machines published, and a machine whose sync is\n    broken cannot publish. Its own sessions must hear it from the daemon directly.'
    from agent_context import daemon
    monkeypatch.setattr(daemon, "get_health", lambda: {
        "verdict": "sync-broken", "last_sync_error": "integration failed (12 behind)",
        "log_hint": "journalctl --user -u agent-context-daemon -n 200"})
    ctx = T.get_session_context(store, "/no/project")
    dh = ctx["daemon_health"]
    assert dh["verdict"] == "sync-broken"
    assert "12 behind" in dh["reason"]
    assert "journalctl" in dh["log_hint"]
    assert "NOT syncing" in dh["note"]


def test_bootstrap_says_nothing_about_a_healthy_daemon(store, monkeypatch):
    from agent_context import daemon
    monkeypatch.setattr(daemon, "get_health", lambda: {"verdict": "healthy"})
    ctx = T.get_session_context(store, "/no/project")
    assert "daemon_health" not in ctx
    monkeypatch.setattr(daemon, "get_health", lambda: {"verdict": "starting"})
    assert "daemon_health" not in T.get_session_context(store, "/no/project")



def test_rendered_bootstrap_carries_instruction_bodies_verbatim(store):
    body = '# Rules\n\n- one "quoted"\n- two\n'
    T.upsert_instruction(store, "Rule", body, project=None, load_behavior="always")
    md = T.render_session_context(T.get_session_context(store, "/no/project"))
    assert "## Instruction: Rule\n\n" + body in md
    assert "\\n" not in md and '\\"' not in md      
    assert not md.lstrip().startswith("{")


def test_rendered_bootstrap_keeps_index_rows_and_lazy_roster(store):
    T.upsert_memory(store, "always-one", "reference", "loaded every session", "b", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "lazy-one", "reference", "archival prose", "b", project=None,
                    load_behavior="lazy")
    md = T.render_session_context(T.get_session_context(store, "/no/project"))
    assert "## Memory index" in md and "### global" in md
    row = next(ln for ln in md.splitlines() if ln.startswith("r always-one "))
    assert row.endswith(" loaded every session")
    lazy = next(ln for ln in md.splitlines() if ln.startswith("lazy: "))
    assert "lazy-one" in lazy.split()
    assert "archival prose" not in md


def test_rendered_bootstrap_never_drops_an_unknown_key(store):
    ctx = T.get_session_context(store, "/no/project")
    ctx["brand_new_signal"] = {"verdict": "bad", "count": 3}
    md = T.render_session_context(ctx)
    assert "## brand_new_signal" in md
    assert "- verdict: bad" in md and "- count: 3" in md


def test_rendered_bootstrap_shows_daemon_health_and_observations(store, monkeypatch):
    from agent_context import daemon
    monkeypatch.setattr(daemon, "get_health", lambda: {"verdict": "stale-code",
                                                       "log_hint": "journalctl x"})
    T.add_audit_observation(store, "obs-summary-here", scope="universal", project=None,
                            evidence="e", severity="high")
    md = T.render_session_context(T.get_session_context(store, "/no/project"))
    assert "## Daemon health\nverdict: stale-code\n" in md
    assert "log: journalctl x" in md
    assert "## Open audit observations" in md and "obs-summary-here" in md


def test_session_context_tool_returns_plain_text_not_structured_content():
    import asyncio

    from agent_context import server
    tool = asyncio.run(server.mcp.list_tools())
    spec = next(t for t in tool if t.name == "get_session_context")
    assert spec.outputSchema is None        
