'test claims every harness.'
import json
import time

from agent_context import claims, server
from agent_context import fstools as T


def _hook_claim(session, cwd, pids, now=None):
    d = claims.claims_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.json").write_text(json.dumps(
        {"session": session, "cwd": cwd, "project": "agent-context",
         "last_seen": now or time.time(), "pids": pids, "files": []}))




def test_server_claim_is_made_renewed_and_retired_by_a_hook_claim(tmp_path):
    now = time.time()
    rec = claims.ensure_server_claim(777, cwd="/w/proj/.claude/worktrees/fix-a", project="Demo",
                                     project_id="p1", now=now)
    assert rec["session"] == "pid777" and rec["pids"] == [777] and rec["worktree"] == "fix-a"
    live = claims.read_live(now=now)
    assert [s["session"] for s in live] == ["pid777"] and live[0]["project"] == "Demo"
    
    rec = claims.ensure_server_claim(777, file="/w/store/global/docs/a.md", now=now + 60)
    assert rec["cwd"] == "/w/proj/.claude/worktrees/fix-a"
    assert [f["path"] for f in rec["files"]] == ["/w/store/global/docs/a.md"]
    assert claims.caller(claims.read_live(now=now + 60), 777)["session"] == "pid777"
    
    _hook_claim("abcdefgh-1", "/w/proj", [100, 777], now=now + 90)
    assert claims.ensure_server_claim(777, file="/x", now=now + 90) is None
    assert [s["session"] for s in claims.read_live(now=now + 90)] == ["abcdefgh"]
    assert not (claims.claims_dir() / "server-777.json").exists()
    assert claims.ensure_server_claim(None) is None


def test_server_claim_is_retired_for_a_relay_pid_via_its_own_ancestry(monkeypatch):
    'test server claim is retired for a relay pid via its own ancestry.'
    now = time.time()
    _hook_claim("4d8401ac-1", "/w/proj", [634201, 700], now=now)
    monkeypatch.setattr(claims, "_ancestors",
                        lambda pid, **k: [634201, 700] if pid == 634343 else [])
    assert claims.ensure_server_claim(634343, file="/x", now=now) is None
    assert not (claims.claims_dir() / "server-634343.json").exists()
    assert [s["session"] for s in claims.read_live(now=now)] == ["4d8401ac"]


def test_bootstrap_claims_for_an_unclaimed_caller_and_does_not_list_it(store, monkeypatch):
    from agent_context import fleet
    monkeypatch.setattr(fleet, "read_all", lambda root, now=None: [])
    monkeypatch.setattr(store, "_maybe_fetch_for_guard", lambda: None)
    ctx = T.get_session_context(store, store.root, caller_pid=4242)
    assert "active_sessions" not in ctx
    live = claims.read_live()
    assert [s["session"] for s in live] == ["pid4242"]
    assert live[0]["cwd"] == store.root and live[0]["project"] is None




def test_a_second_session_on_this_machine_is_warned_about(store, monkeypatch):
    _hook_claim("aaaaaaaa-1", "/w/one", [100, 51598])
    _hook_claim("bbbbbbbb-2", "/w/two", [200, 51700])
    monkeypatch.setattr(store, "_fleet_rows", lambda max_age=60.0: [])
    current = {"pid": 51598}
    store.caller_pid_fn = lambda: current["pid"]
    T.upsert_doc(store, "shared.md", "from a", title="Shared")
    assert store.pop_write_warning() is None
    current["pid"] = 51700
    T.upsert_doc(store, "shared.md", "from b", title="Shared")
    w = store.pop_write_warning()
    assert w and "Another session is on this entity" in w
    assert "session aaaaaaaa on this machine (/w/one) wrote it 1 min ago" in w
    
    T.upsert_doc(store, "shared.md", "from b again", title="Shared")
    w = store.pop_write_warning()
    assert w and "aaaaaaaa" in w and "bbbbbbbb" not in w
    
    rw = store.recent_writes()
    assert [(r["path"], r.get("session")) for r in rw][-3:] == [
        ("global/docs/shared.md", "aaaaaaaa"), ("global/docs/shared.md", "bbbbbbbb"),
        ("global/docs/shared.md", "bbbbbbbb")]




def test_recreating_an_entity_another_session_deleted_is_warned(store, monkeypatch):
    _hook_claim("aaaaaaaa-1", "/w/one", [100, 51598])
    _hook_claim("bbbbbbbb-2", "/w/two", [200, 51700])
    monkeypatch.setattr(store, "_fleet_rows", lambda max_age=60.0: [])
    current = {"pid": 51598}
    store.caller_pid_fn = lambda: current["pid"]
    T.upsert_memory(store, "recreate-me", "reference", "d", "body one", project=None)
    store.pop_write_warning()
    T.delete_entity(store, "memory", "recreate-me")
    rw = store.recent_writes()
    assert rw[-1]["path"] == "global/memory/recreate-me.md" and rw[-1]["op"] == "delete"
    current["pid"] = 51700
    T.upsert_memory(store, "recreate-me", "reference", "d", "body two", project=None)
    w = store.pop_write_warning()
    assert w and "re-created" in w and "deleted it" in w


def test_a_session_recreating_its_own_delete_is_not_warned(store, monkeypatch):
    _hook_claim("aaaaaaaa-1", "/w/one", [100, 51598])
    monkeypatch.setattr(store, "_fleet_rows", lambda max_age=60.0: [])
    store.caller_pid_fn = lambda: 51598
    T.upsert_memory(store, "own-delete", "reference", "d", "body one", project=None)
    store.pop_write_warning()
    T.delete_entity(store, "memory", "own-delete")
    T.upsert_memory(store, "own-delete", "reference", "d", "body two", project=None)
    w = store.pop_write_warning()
    assert w is None or "re-created" not in w


def test_only_the_recreation_is_warned_not_later_writes(store, monkeypatch):
    _hook_claim("aaaaaaaa-1", "/w/one", [100, 51598])
    _hook_claim("bbbbbbbb-2", "/w/two", [200, 51700])
    monkeypatch.setattr(store, "_fleet_rows", lambda max_age=60.0: [])
    current = {"pid": 51598}
    store.caller_pid_fn = lambda: current["pid"]
    T.upsert_memory(store, "recreate-again", "reference", "d", "body one", project=None)
    store.pop_write_warning()
    T.delete_entity(store, "memory", "recreate-again")
    current["pid"] = 51700
    T.upsert_memory(store, "recreate-again", "reference", "d", "body two", project=None)
    w = store.pop_write_warning()
    assert w and "re-created" in w
    T.upsert_memory(store, "recreate-again", "reference", "d", "body three", project=None)
    w2 = store.pop_write_warning()
    assert w2 is None or "re-created" not in w2


def test_another_machine_deleting_it_is_warned_on_recreate(store, monkeypatch):
    from agent_context import machine
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "U-LAP")
    now = time.time()
    rows = [{"machine_id": "m4", "machine_uuid": "U-M4", "updated_at": now,
             "sessions": [],
             "recent_writes": [{"path": "global/memory/remote-deleted.md",
                                 "ts": now - 120, "op": "delete"}]}]
    monkeypatch.setattr(store, "_fleet_rows", lambda max_age=60.0: rows)
    store.caller_pid_fn = lambda: None
    T.upsert_memory(store, "remote-deleted", "reference", "d", "body", project=None)
    w = store.pop_write_warning()
    assert w and "re-created" in w and "deleted it" in w


def test_a_relay_pids_own_write_is_never_warned_as_another_session(store, monkeypatch):
    'test a relay pids own write is never warned as another session.'
    _hook_claim("4d8401ac-1", "/w/proj", [634201, 700], now=time.time())
    monkeypatch.setattr(store, "_fleet_rows", lambda max_age=60.0: [])
    monkeypatch.setattr(claims, "_ancestors",
                        lambda pid, **k: [634201, 700] if pid == 634343 else [])
    store.caller_pid_fn = lambda: 634201
    T.upsert_doc(store, "relay.md", "from the hook pid", title="Relay")
    assert store.pop_write_warning() is None
    store.caller_pid_fn = lambda: 634343
    T.upsert_doc(store, "relay.md", "from the relay pid", title="Relay")
    assert store.pop_write_warning() is None
    assert not (claims.claims_dir() / "server-634343.json").exists()
    rw = store.recent_writes()
    assert [r.get("session") for r in rw][-2:] == ["4d8401ac", "4d8401ac"]


def test_a_hookless_writer_gets_a_server_claim_with_the_file(store, monkeypatch):
    monkeypatch.setattr(store, "_fleet_rows", lambda max_age=60.0: [])
    store.caller_pid_fn = lambda: 9001
    T.upsert_doc(store, "notes.md", "x", title="Notes")
    live = claims.read_live()
    assert [s["session"] for s in live] == ["pid9001"]
    assert live[0]["files"][-1].endswith("global/docs/notes.md")
    assert store.recent_writes()[-1]["session"] == "pid9001"


def test_stateless_clients_do_not_pay_a_lookup_per_request(monkeypatch):
    import asyncio
    calls = []
    monkeypatch.setattr(claims, "peer_pid", lambda port: calls.append(port) or 55)
    server._PEER_CACHE.clear()

    async def app(scope, receive, send):
        assert server.caller_pid() == 55

    wrapped = server._counting_app(app)
    scope = {"type": "http", "method": "POST", "client": ("127.0.0.1", 60000), "headers": []}

    async def run():
        for _ in range(3):
            await asyncio.create_task(wrapped(scope, None, None))
    asyncio.run(run())
    assert calls == [60000]
