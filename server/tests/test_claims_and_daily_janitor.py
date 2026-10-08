'test claims and daily janitor.'
import json
import os
import time

from agent_context import claims, fleet, janitor, paths
from agent_context import fstools as T


def _claim(session, cwd, seen, files=(), project=None, project_id=None, worktree=None):
    d = claims.claims_dir()
    d.mkdir(parents=True, exist_ok=True)
    rec = {"session": session, "cwd": cwd, "repo": cwd, "project": project,
           "project_id": project_id, "worktree": worktree, "last_seen": seen,
           "started": seen, "files": [{"path": f, "ts": int(seen)} for f in files]}
    (d / f"{session}.json").write_text(json.dumps(rec))
    return d / f"{session}.json"




def test_live_claims_are_published_and_expired_ones_are_removed(tmp_path):
    now = time.time()
    live = _claim("abcdef0123456789", "/w/proj", now - 60, files=["/w/proj/a.py"],
                  project="Demo", project_id="p1", worktree="fix-x")
    dead = _claim("dead", "/w/other", now - 3 * 3600)
    rows = claims.read_live(now=now)
    assert [r["session"] for r in rows] == ["abcdef01"]         
    assert rows[0]["project"] == "Demo" and rows[0]["worktree"] == "fix-x"
    assert rows[0]["files"] == ["/w/proj/a.py"]
    assert rows[0]["last_seen"] % claims.SEEN_BUCKET_SECS == 0
    assert live.exists() and not dead.exists()


def test_the_ref_row_carries_live_facts_and_the_tree_row_does_not(tmp_path):
    row = json.dumps({"machine_id": "laptop", "updated_at": 100})
    out = json.loads(fleet.with_live(row, sessions=[{"session": "x"}],
                                     recent_writes=[{"path": "global/docs/a.md", "ts": 1}]))
    assert out["sessions"] == [{"session": "x"}]
    assert out["recent_writes"][0]["path"] == "global/docs/a.md"
    assert json.loads(row).get("sessions") is None
    
    assert fleet.with_live(row) == json.dumps(json.loads(row), separators=(",", ":"),
                                              sort_keys=True)




def _rows(now, lap_uuid="U-LAP", lap_cwd="/Users/user/.agent-context"):
    return [
        {"machine_id": "m4", "machine_uuid": "U-M4", "updated_at": now,
         "sessions": [
             {"session": "aaaa", "cwd": "/Users/j/Dev/example-app", "project": "example-app",
              "project_id": "p-mobile", "worktree": "fix-login", "last_seen": now - 120,
              "files": ["/Users/j/Dev/example-app/.claude/worktrees/fix-login/Login.cs"]},
             {"session": "bbbb", "cwd": "/Users/j/.agent-context", "project": "agent-context",
              "project_id": None, "worktree": None, "last_seen": now - 30,
              "files": ["/Users/j/.agent-context/global/hooks/guard-git-write.py"]},
             {"session": "gone", "cwd": "/x", "project": "example-app", "project_id": "p-mobile",
              "last_seen": now - 4 * 3600, "files": []}],
         "recent_writes": [{"path": "global/docs/worktrees.md", "ts": now - 300}]},
        {"machine_id": "laptop", "machine_uuid": lap_uuid, "updated_at": now,
         "sessions": [
             {"session": "cccc", "cwd": lap_cwd, "project": "agent-context",
              "project_id": None, "last_seen": now - 10, "files": []}]},
    ]


def test_relevant_sessions_are_same_project_or_both_in_store():
    now = time.time()
    live = claims.fleet_sessions(_rows(now), now=now)
    assert {s["session"] for s in live} == {"aaaa", "bbbb", "cccc"}   
    
    got = claims.relevant(live, "/Users/user/Dev/example-app", project_id="p-mobile",
                          project_name="example-app", this_uuid="U-LAP")
    assert [s["session"] for s in got] == ["aaaa"]
    
    got = claims.relevant(live, "/Users/user/.agent-context", project_name="agent-context",
                          this_uuid="U-LAP")
    assert [s["session"] for s in got] == ["bbbb"]
    line = claims.describe(got[0], now)
    assert line.startswith("m4 · agent-context · /Users/j/.agent-context · seen just now")
    assert "editing guard-git-write.py" in line


def test_store_paths_touched_maps_hand_edits_to_entity_paths():
    now = time.time()
    touched = claims.store_paths_touched(claims.fleet_sessions(_rows(now), now=now))
    assert list(touched) == ["global/hooks/guard-git-write.py"]
    assert touched["global/hooks/guard-git-write.py"][0]["session"] == "bbbb"


def test_bootstrap_names_other_sessions_and_only_them(store, monkeypatch):
    me = T.get_session_context(store, "/nowhere")["machine"]["machine_uuid"]
    
    monkeypatch.setattr(fleet, "read_all",
                        lambda root, now=None: _rows(now or time.time(), lap_uuid=me,
                                                     lap_cwd=store.root))
    ctx = T.get_session_context(store, "/nowhere/example-app")
    
    assert "active_sessions" not in ctx
    ctx = T.get_session_context(store, store.root)
    lines = ctx["active_sessions"]["sessions"]
    assert len(lines) == 1 and lines[0].startswith("m4 · agent-context")




def test_a_write_warns_when_another_machine_is_on_the_entity(store, monkeypatch):
    now = time.time()
    monkeypatch.setattr(store, "_fleet_rows", lambda max_age=60.0: _rows(now))
    from agent_context import machine
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "U-LAP")
    T.upsert_doc(store, "worktrees.md", "new body", title="Worktrees")
    w = store.pop_write_warning()
    assert w and "Another session is on this entity" in w and "m4 wrote it" in w
    assert store.pop_write_warning() is None                     
    T.upsert_doc(store, "unrelated.md", "x", title="Unrelated")
    assert store.pop_write_warning() is None
    
    store.upsert("hook", "guard-git-write", {"event_type": "PreToolUse", "language": "py"},
                 body="#!/usr/bin/env python3\n")
    w = store.pop_write_warning()
    assert w and "a live session edited it by hand: m4" in w


def test_the_receipt_carries_the_warning(store, monkeypatch):
    from agent_context import server
    monkeypatch.setattr(server, "_store", store)
    store._write_warning = "ANOTHER MACHINE IS ON THIS ENTITY: test"
    r = server._no_body({"body": "b", "warning": "budget"}, "doc")
    assert r["warning"] == "budget | ANOTHER MACHINE IS ON THIS ENTITY: test"
    assert "body_omitted" in r




def test_eval_gaps_are_filed_once(store, monkeypatch):
    monkeypatch.setattr(janitor, "_run_candidates",
                        lambda root, days, mn: {"gaps": {"block-sleep-poll": 7}, "no_data": False})
    first = janitor.sweep_eval_gaps(store.root, store)
    assert first["block-sleep-poll"].startswith("filed #")
    second = janitor.sweep_eval_gaps(store.root, store)
    assert second["block-sleep-poll"] == "already open"
    from agent_context import audit
    obs = [o for o in audit.list_audit_observations(store, status="open")
           if "[janitor] eval gap: block-sleep-poll" in o["observation"]]
    assert len(obs) == 1 and 'covers=["block-sleep-poll"]' in obs[0]["observation"]


def test_footprint_over_ceiling_and_growth_are_filed(store, monkeypatch):
    from agent_context import integrity
    calls = {"n": 0}

    def fake(_store):
        calls["n"] += 1
        g = [1000, 1000, 2000][min(calls["n"] - 1, 2)]
        return {"bootstrap_footprint": {"global": {"est_bootstrap_bytes": g}},
                "bootstrap_ceiling": 60000,
                "over_ceiling_sessions": ["project:Big"] if calls["n"] == 1 else []}
    monkeypatch.setattr(integrity, "check_integrity", fake)
    r1 = janitor.watch_bootstrap_footprint(store.root, store)
    assert r1["filed"].startswith("filed #") and r1["over"] == ["project:Big"]
    r2 = janitor.watch_bootstrap_footprint(store.root, store)      
    assert "filed" not in r2
    r3 = janitor.watch_bootstrap_footprint(store.root, store)      
    assert r3["filed"].startswith("filed #")
    stamp = json.loads((paths.state_dir() / "janitor-footprint.json").read_text())
    assert stamp["global"] == 2000


def test_daily_chores_run_once_a_day_and_only_with_a_store(tmp_path, monkeypatch):
    monkeypatch.setattr(janitor, "sweep_eval_gaps", lambda root, store: {"ran": True})
    monkeypatch.setattr(janitor, "watch_bootstrap_footprint", lambda root, store: {"ran": True})
    monkeypatch.setattr(janitor, "refresh_invariant_health", lambda root: None)
    root = tmp_path / "r"
    root.mkdir()
    os.system(f"git -C {root} init -q -b main")
    now = time.time()
    assert "eval_gaps" not in janitor.sweep(str(root), now=now)               
    out = janitor.sweep(str(root), now=now, store=object())
    assert out["eval_gaps"] == {"ran": True} and out["footprint"] == {"ran": True}
    assert "eval_gaps" not in janitor.sweep(str(root), now=now + 3600, store=object())
    assert "eval_gaps" in janitor.sweep(str(root), now=now + 86401, store=object())
