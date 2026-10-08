'test caller identity.'
import asyncio
import json
import os
import time

from agent_context import claims, fleet, server
from agent_context import fstools as T



def test_linux_port_lookup_walks_proc(tmp_path):
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:CC78 0100007F:223D 01 00000000:00000000 00:00000000 00000000   501        0 999 1 0\n"
        "   1: 0100007F:CC79 0100007F:223D 0A 00000000:00000000 00:00000000 00000000   501        0 998 1 0\n")
    (proc / "net" / "tcp6").write_text("header\n")
    for pid, inode in (("41", "777"), ("42", "999")):
        fd = proc / pid / "fd"
        fd.mkdir(parents=True)
        os.symlink(f"socket:[{inode}]", fd / "5")
    (proc / "notapid").mkdir()
    assert claims._pid_for_port_linux(52344, proc=str(proc)) == 42
    assert claims._pid_for_port_linux(52345, proc=str(proc)) is None   
    assert claims._pid_for_port_linux(1, proc=str(proc)) is None


def test_lsof_parse_picks_the_client_side(monkeypatch):
    import subprocess

    class P:
        stdout = ("p8765\nf12\nn127.0.0.1:8765->127.0.0.1:52344\n"
                  "p51598\nf20\nn127.0.0.1:52344->127.0.0.1:8765\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    assert claims._pid_for_port_lsof(52344) == 51598


def test_caller_matches_the_ancestry_chain():
    sessions = [{"session": "aaaa", "pids": [100, 51598, 2122]},
                {"session": "bbbb", "pids": [200, 51700, 2122]}]
    assert claims.caller(sessions, 51700)["session"] == "bbbb"
    assert claims.caller(sessions, 51598)["session"] == "aaaa"
    assert claims.caller(sessions, 1) is None and claims.caller(sessions, None) is None


def test_caller_matches_a_relay_pid_through_its_own_ancestry(monkeypatch):
    'test caller matches a relay pid through its own ancestry.'
    sessions = [{"session": "aaaa", "pids": [634201, 700, 500]}]
    monkeypatch.setattr(claims, "_ancestors",
                        lambda pid, **k: [634201, 700] if pid == 634343 else [])
    assert claims.caller(sessions, 634343)["session"] == "aaaa"
    assert claims.caller(sessions, 999999) is None




def test_first_request_of_a_session_resolves_the_peer(monkeypatch):
    monkeypatch.setattr(claims, "peer_pid", lambda port: 4242 if port == 52344 else None)
    seen = []

    async def app(scope, receive, send):
        seen.append(server.caller_pid())

    wrapped = server._counting_app(app)
    first = {"type": "http", "method": "POST", "client": ("127.0.0.1", 52344),
             "headers": [(b"content-type", b"application/json")]}
    later = {"type": "http", "method": "POST", "client": ("127.0.0.1", 52400),
             "headers": [(b"mcp-session-id", b"abc")]}
    remote = {"type": "http", "method": "POST", "client": ("10.0.0.9", 52344),
              "headers": []}

    async def run():
        
        for scope in (first, later, remote):
            await asyncio.create_task(wrapped(scope, None, None))
    asyncio.run(run())
    assert seen == [4242, None, None]




def _local_claim(session, cwd, pids):
    d = claims.claims_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.json").write_text(json.dumps(
        {"session": session, "cwd": cwd, "project": "agent-context",
         "last_seen": time.time(), "pids": pids, "files": []}))


def test_two_sessions_in_one_directory_are_told_apart(store, monkeypatch):
    me = T.get_session_context(store, "/nowhere")["machine"]["machine_uuid"]
    _local_claim("aaaaaaaa-1", store.root, [100, 51598])
    _local_claim("bbbbbbbb-2", store.root, [200, 51700])
    now = time.time()
    rows = [{"machine_id": "laptop", "machine_uuid": me, "updated_at": now,
             "sessions": claims.read_live(now=now)}]
    monkeypatch.setattr(fleet, "read_all", lambda root, now=None: rows)
    monkeypatch.setattr(store, "_maybe_fetch_for_guard", lambda: None)
    
    ctx = T.get_session_context(store, store.root, caller_pid=51598)
    lines = ctx["active_sessions"]["sessions"]
    assert len(lines) == 1 and lines[0].startswith("laptop · agent-context")
    assert "may be you" not in ctx["active_sessions"]["note"]
    ctx = T.get_session_context(store, store.root, caller_pid=51700)
    assert len(ctx["active_sessions"]["sessions"]) == 1
    
    ctx = T.get_session_context(store, store.root)
    assert len(ctx["active_sessions"]["sessions"]) == 2
    assert "may be you" in ctx["active_sessions"]["note"]


def test_bootstrap_fetches_before_reading_fleet_rows(store, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "_maybe_fetch_for_guard", lambda: calls.append(1))
    T.get_session_context(store, "/nowhere")
    assert calls == [1]
