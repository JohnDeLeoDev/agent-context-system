"A session's machine is the machine it runs on, not the machine the daemon runs on."
from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import cast

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCResponse

from agent_context import audit, claims, daemon, fleet, identity, machine, server
from agent_context import fstools as T
from agent_context import projects as P

REMOTE_HOST = "example.invalid"
LAPTOP_UUID = "AAAAAAAA-1111-2222-3333-LAPTOPLAPTOP"
LAPTOP = identity.Identity(machine_uuid=LAPTOP_UUID, machine_id="laptop",
                           platform="darwin", home="/Users/user", session_key="s-1")


def _add_machine(store, uuid: str, machine_id: str, plat: str, home: str, host: str) -> None:
    'A machine row as `_ensure_machine` writes it, for a machine other than this host.'
    from agent_context.store import emit_toml, stable_uuid
    meta = {"type": "machine", "machine_uuid": uuid, "hostname": host, "platform": plat,
            "home_dir": home, "display_name": host.split(".")[0], "machine_id": machine_id}
    path = Path(store.root) / "machines" / f"{uuid}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_toml(meta))
    meta["scope"] = "global"
    meta["uuid"] = stable_uuid("machine", "global", f"{uuid}.toml")
    with store.lock:
        store._index(meta, str(path))


@pytest.fixture
def laptop_row(store) -> None:
    _add_machine(store, LAPTOP_UUID, "laptop", "darwin", "/Users/user", "MacBook-Pro.local")


@pytest.fixture
def as_laptop(laptop_row: None):
    "Bind the laptop's identity for the duration of a test, as the wrapper would."
    token = identity.bind(LAPTOP)
    yield LAPTOP
    identity.reset(token)


@pytest.fixture
def relay_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    "A relay on the laptop: remote host set, and the laptop's local identity."
    monkeypatch.setenv("AGENT_CONTEXT_HOST", REMOTE_HOST)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "tok")
    monkeypatch.setenv("HOME", "/Users/user")
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: LAPTOP_UUID)
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: "laptop")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")


def _lower(d: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in d.items()}




def test_a_remote_relay_sends_its_identity_on_every_connection(relay_env: None) -> None:
    h = _lower(daemon.connect_headers())
    assert h[identity.HEADER_MACHINE] == LAPTOP_UUID
    assert h[identity.HEADER_MACHINE_ID] == "laptop"
    assert h[identity.HEADER_PLATFORM] == "darwin"
    assert h[identity.HEADER_HOME] == "/Users/user"
    assert h["authorization"] == "Bearer tok"


def test_a_machine_with_no_chezmoi_id_omits_that_header(
        relay_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: None)
    h = _lower(daemon.connect_headers())
    assert h[identity.HEADER_MACHINE] == LAPTOP_UUID
    assert identity.HEADER_MACHINE_ID not in h


def test_a_local_relay_sends_no_identity_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_HOST", raising=False)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "tok")
    h = _lower(daemon.connect_headers())
    assert [k for k in h if k.startswith("x-agent-context-")] == []
    assert h["authorization"] == "Bearer tok"


def test_headers_round_trip_into_an_identity(relay_env: None) -> None:
    pairs = [(k.encode(), v.encode()) for k, v in daemon.connect_headers().items()]
    got = identity.from_headers(pairs)
    assert got is not None
    assert (got.machine_uuid, got.machine_id, got.platform, got.home) == (
        LAPTOP_UUID, "laptop", "darwin", "/Users/user")


def test_requests_without_identity_headers_bind_nothing() -> None:
    assert identity.from_headers([(b"authorization", b"Bearer x")]) is None
    assert identity.from_headers([(identity.HEADER_MACHINE.encode(), b"  ")]) is None




def _scope(headers: list[tuple[bytes, bytes]], method: str = "POST") -> dict:
    return {"type": "http", "method": method, "client": ("127.0.0.1", 40000),
            "headers": headers}


def _laptop_headers() -> list[tuple[bytes, bytes]]:
    return [(identity.HEADER_MACHINE.encode(), LAPTOP_UUID.encode()),
            (identity.HEADER_MACHINE_ID.encode(), b"laptop"),
            (identity.HEADER_PLATFORM.encode(), b"darwin"),
            (identity.HEADER_HOME.encode(), b"/Users/user")]


def test_the_first_request_binds_the_identity_for_the_session_task(
        store, laptop_row: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    seen: list[identity.Identity | None] = []

    async def app(scope, receive, send) -> None:
        seen.append(identity.current())

    wrapped = server._counting_app(app)

    async def run() -> None:
        for scope in (_scope(_laptop_headers()), _scope([])):
            await asyncio.create_task(wrapped(scope, None, None))

    asyncio.run(run())
    bound, unbound = seen
    assert bound is not None
    assert (bound.machine_uuid, bound.machine_id) == (LAPTOP_UUID, "laptop")
    assert bound.session_key
    assert unbound is None


def test_each_session_gets_its_own_session_key(
        store, laptop_row: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    keys: list[str | None] = []

    async def app(scope, receive, send) -> None:
        cur = identity.current()
        keys.append(cur.session_key if cur else None)

    wrapped = server._counting_app(app)

    async def run() -> None:
        for _ in range(2):
            await asyncio.create_task(wrapped(_scope(_laptop_headers()), None, None))

    asyncio.run(run())
    assert keys[0] and keys[1] and keys[0] != keys[1]


def test_bound_identity_does_not_change_the_daemon_hosts_own_identity(
        laptop_row: None, as_laptop: identity.Identity) -> None:
    assert machine.get_machine_uuid() != LAPTOP_UUID
    assert identity.session_machine_uuid() == LAPTOP_UUID
    assert identity.session_machine_id() == "laptop"




def test_a_known_uuid_is_accepted(store, laptop_row: None) -> None:
    assert identity.refusal(store, LAPTOP) is None


def test_an_unknown_uuid_is_refused_and_the_known_machines_are_listed(
        store, laptop_row: None) -> None:
    ghost = identity.Identity("NOPE-0000", "ghost", "linux", "/home/x")
    r = identity.refusal(store, ghost)
    assert r is not None
    assert "NOPE-0000" in r["error"]
    assert "laptop" in json.dumps(r)


def test_the_wrapper_refuses_an_unknown_machine_with_403_and_never_calls_the_app(
        store, laptop_row: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    called: list[int] = []
    sent: list[dict] = []

    async def app(scope, receive, send) -> None:
        called.append(1)

    async def send(message: dict) -> None:
        sent.append(message)

    hdrs = [(identity.HEADER_MACHINE.encode(), b"NOPE-0000")]
    asyncio.run(server._counting_app(app)(_scope(hdrs), None, send))
    assert called == []
    assert sent[0]["status"] == 403
    body = b"".join(m.get("body", b"") for m in sent[1:])
    assert b"laptop" in body




def test_a_session_with_no_identity_reports_the_daemon_host(store) -> None:
    m = T.get_session_context(store, "/nowhere")["machine"]
    assert m["machine_uuid"] == machine.get_machine_uuid()
    assert m["machine_id"] == machine.get_chezmoi_machine_id()
    assert identity.session_machine_uuid() == machine.get_machine_uuid()




def test_get_session_context_names_the_calling_machine(
        store, as_laptop: identity.Identity) -> None:
    m = T.get_session_context(store, "/Users/user/x")["machine"]
    assert (m["machine_uuid"], m["machine_id"]) == (LAPTOP_UUID, "laptop")
    assert (m["platform"], m["home_dir"]) == ("darwin", "/Users/user")


def test_the_context_does_not_write_a_row_for_the_daemon_host_on_behalf_of_a_remote_caller(
        store, as_laptop: identity.Identity) -> None:
    before = {e["machine_uuid"] for e in store.entities.values() if e["type"] == "machine"}
    T.get_session_context(store, "/Users/user/x")
    after = {e["machine_uuid"] for e in store.entities.values() if e["type"] == "machine"}
    assert after == before == {LAPTOP_UUID}


def test_a_bound_unknown_machine_gets_an_error_naming_the_known_ones(
        store, laptop_row: None) -> None:
    token = identity.bind(identity.Identity("NOPE-0000", "ghost", "linux", "/x", "s"))
    try:
        r = T.get_session_context(store, "/x")
    finally:
        identity.reset(token)
    assert "error" in r and "laptop" in json.dumps(r)




def _project(store, name: str = "example-api", remote: str = "git@github.com:acme/mobile-api.git") -> dict:
    T.upsert_project(store, remote, name)
    return next(e for e in store.entities.values()
                if e.get("type") == "project" and e.get("display_name") == name)


def test_a_marker_id_in_the_evidence_resolves_the_project_without_touching_disk(store) -> None:
    proj = _project(store)
    cwd = "/Users/user/Developer/example-api"
    ev = {"cwd": cwd, "marker_id": proj["uuid"], "remotes": []}
    r = P.resolve_project(store, cwd, evidence=ev)
    assert r.get("display_name") == "example-api" and r.get("id") == proj["uuid"]
    assert r["resolved_via"] == "marker"
    paths = cast("list[dict[str, object]]", r["paths"])
    assert paths[0]["local_path"] == cwd
    assert not Path(cwd).exists()
    assert "marker_written" not in r


def test_git_remotes_in_the_evidence_resolve_the_project(store) -> None:
    proj = _project(store)
    cwd = "/Users/user/Developer/example-api"
    ev = {"cwd": cwd, "marker_id": None, "remotes": ["https://github.com/acme/mobile-api.git"]}
    r = P.resolve_project(store, cwd, evidence=ev)
    assert r.get("id") == proj["uuid"] and r["resolved_via"] == "remote"
    assert "marker_written" not in r


def test_evidence_for_a_different_cwd_is_ignored(store) -> None:
    proj = _project(store)
    ev = {"cwd": "/Users/user/Developer/Other", "marker_id": proj["uuid"], "remotes": []}
    assert "error" in P.resolve_project(store, "/Users/user/Developer/example-api", evidence=ev)


def test_evidence_that_matches_no_project_is_an_error(store) -> None:
    _project(store)
    cwd = "/Users/user/Developer/Unknown"
    ev = {"cwd": cwd, "marker_id": "no-such-id", "remotes": ["git@github.com:x/y.git"]}
    assert "error" in P.resolve_project(store, cwd, evidence=ev)


def test_without_evidence_an_absent_directory_still_resolves_nothing(store) -> None:
    _project(store)
    assert "error" in P.resolve_project(store, "/Users/user/Developer/example-api")


def test_the_session_context_uses_the_evidence_and_skips_the_hosts_disk_warning(
        store, as_laptop: identity.Identity) -> None:
    proj = _project(store)
    cwd = "/Users/user/Developer/example-api"
    ev = {"cwd": cwd, "marker_id": proj["uuid"], "remotes": []}
    ctx = T.get_session_context(store, cwd, evidence=ev)
    assert ctx["project"]["display_name"] == "example-api"
    assert "project_warning" not in ctx


def test_the_relay_reads_marker_and_remotes_from_its_own_disk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/mobile-api.git"],
                   cwd=repo, check=True)
    P._write_project_marker(str(repo), "proj-123", "example-api")
    ev = identity.project_evidence(str(repo))
    assert ev["cwd"] == str(repo)
    assert ev["marker_id"] == "proj-123"
    assert "git@github.com:acme/mobile-api.git" in ev["remotes"]


def test_the_relay_evidence_for_a_plain_directory_is_empty(tmp_path: Path) -> None:
    ev = identity.project_evidence(str(tmp_path))
    assert ev["cwd"] == str(tmp_path)
    assert ev["marker_id"] is None and ev["remotes"] == []


def test_a_tool_call_with_a_cwd_carries_the_evidence_in_meta(tmp_path: Path) -> None:
    params = {"name": "resolve_project", "arguments": {"cwd": str(tmp_path)},
              "_meta": {"progressToken": 7}}
    out = identity.add_project_evidence(params)
    assert out["_meta"][identity.EVIDENCE_META_KEY]["cwd"] == str(tmp_path)
    assert out["_meta"]["progressToken"] == 7
    assert out["arguments"] == params["arguments"]
    assert identity.EVIDENCE_META_KEY not in params["_meta"]          


def test_a_tool_call_without_a_cwd_is_left_alone() -> None:
    params = {"name": "list_machines", "arguments": {}}
    assert identity.add_project_evidence(params) == params
    assert "_meta" not in identity.add_project_evidence(params)


def test_the_server_reads_the_evidence_back_from_meta(tmp_path: Path) -> None:
    out = identity.add_project_evidence({"name": "x", "arguments": {"cwd": str(tmp_path)}})
    assert identity.evidence_from_meta(out["_meta"]) == out["_meta"][identity.EVIDENCE_META_KEY]
    assert identity.evidence_from_meta(None) is None
    assert identity.evidence_from_meta({"progressToken": 1}) is None




def test_list_machines_flags_the_calling_machine(store, as_laptop: identity.Identity) -> None:
    rows = {r["machine_uuid"]: r for r in T.list_machines(store)}
    assert rows[LAPTOP_UUID]["is_current"] is True
    assert [u for u, r in rows.items() if r["is_current"]] == [LAPTOP_UUID]


def test_observations_are_stamped_with_the_calling_machine(
        as_laptop: identity.Identity) -> None:
    assert audit._this_machine() == "laptop"


def test_a_local_session_stamps_the_daemon_host(store) -> None:
    assert audit._this_machine() == (machine.get_chezmoi_machine_id() or machine.get_machine_uuid())


def test_inbox_items_follow_the_calling_machines_fleet_id(
        store, as_laptop: identity.Identity) -> None:
    T.upsert_doc(store, "inbox/machines/laptop/hello.md", body="hi", title="For laptop")
    T.upsert_doc(store, "inbox/machines/MacBook-Pro.local/old.md", body="no", title="By hostname")
    T.upsert_doc(store, "inbox/machines/server-host/other.md", body="no", title="For ls")
    paths = [i["path"] for i in T.get_session_context(store, "/Users/user/x")["inbox"]]
    assert "inbox/machines/laptop/hello.md" in paths
    assert "inbox/machines/MacBook-Pro.local/old.md" not in paths
    assert "inbox/machines/server-host/other.md" not in paths


def test_a_remote_session_is_claimed_under_its_own_machine(
        store, as_laptop: identity.Identity) -> None:
    T.get_session_context(store, "/Users/user/Developer/example-api")
    mine = claims.read_remote(LAPTOP_UUID)
    assert len(mine) == 1
    assert mine[0]["cwd"] == "/Users/user/Developer/example-api"
    assert mine[0]["machine_uuid"] == LAPTOP_UUID
    assert claims.read_remote("someone-else") == []
    T.get_session_context(store, "/Users/user/Developer/example-api")
    assert len(claims.read_remote(LAPTOP_UUID)) == 1               


def test_remote_claims_appear_under_the_machines_fleet_row() -> None:
    now = time.time()
    rows = [{"machine_uuid": LAPTOP_UUID, "machine_id": "laptop", "updated_at": now,
             "sessions": []},
            {"machine_uuid": "ls-uuid", "machine_id": "server-host", "updated_at": now,
             "sessions": []}]
    remote = [{"session": "s-1", "cwd": "/Users/user/x", "machine_uuid": LAPTOP_UUID,
               "last_seen": now, "files": []}]
    merged = {r["machine_uuid"]: r for r in fleet.merge_remote_claims(rows, remote)}
    assert [s["session"] for s in merged[LAPTOP_UUID]["sessions"]] == ["s-1"]
    assert merged["ls-uuid"]["sessions"] == []


def test_a_remote_claim_for_a_machine_with_no_fleet_row_gets_one() -> None:
    remote = [{"session": "s-1", "cwd": "/x", "machine_uuid": "u-9", "machine_id": "rp",
               "last_seen": time.time(), "files": []}]
    merged = fleet.merge_remote_claims([], remote)
    assert [(r["machine_uuid"], r.get("machine_id")) for r in merged] == [("u-9", "rp")]




class _Conn:
    def __init__(self) -> None:
        self.to_bridge, self.d_read = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        self.d_write, self.from_bridge = anyio.create_memory_object_stream[SessionMessage](16)


async def _until(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    with anyio.fail_after(timeout):
        while not pred():
            await anyio.sleep(0.01)


async def _drive(monkeypatch: pytest.MonkeyPatch, drops: int,
                 tool_call: JSONRPCRequest | None = None
                 ) -> tuple[list[dict[str, str] | None], list[SessionMessage]]:
    'Run the real `_bridge` against fake stdio and fake daemon sessions.\n\n    Returns the headers of every connection the bridge opened, and what the first\n    session received after `initialize` (the forwarded tool call, when one was sent).'
    headers_seen: list[dict[str, str] | None] = []
    conns: list[_Conn] = []
    c_in_send, c_read = anyio.create_memory_object_stream[SessionMessage | Exception](16)
    c_write, c_out_recv = anyio.create_memory_object_stream[SessionMessage](16)

    @contextlib.asynccontextmanager
    async def fake_stdio() -> AsyncIterator[tuple[object, object]]:
        yield c_read, c_write

    @contextlib.asynccontextmanager
    async def fake_client(url: str, headers: dict[str, str] | None = None
                          ) -> AsyncIterator[tuple[object, object, Callable[[], None]]]:
        headers_seen.append(headers)
        conn = _Conn()
        conns.append(conn)
        yield conn.d_read, conn.d_write, lambda: None

    monkeypatch.setattr(daemon, "stdio_server", fake_stdio)
    monkeypatch.setattr(daemon, "streamablehttp_client", fake_client)
    monkeypatch.setattr(daemon, "ensure_daemon", lambda: "url")

    class _NoRefresh:
        def __init__(self, *a: object, **k: object) -> None: ...
        def request(self) -> None: ...
        def notify(self, paths: list[str]) -> None: ...
        async def run(self) -> None:
            await anyio.sleep_forever()

    from agent_context import relay_materialize as R
    monkeypatch.setattr(R, "ContentRefresher", _NoRefresh)

    init = SessionMessage(JSONRPCMessage(JSONRPCRequest(
        jsonrpc="2.0", id=1, method="initialize", params={
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}})))
    answer = SessionMessage(JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=1, result={})))
    forwarded: list[SessionMessage] = []
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(daemon._bridge)
            await c_in_send.send(init)
            for n in range(drops + 1):
                await _until(lambda n=n: len(conns) == n + 1)
                await conns[n].from_bridge.receive()
                await conns[n].to_bridge.send(answer)
                if n == 0:
                    await c_out_recv.receive()
                    if tool_call is not None:
                        await c_in_send.send(SessionMessage(JSONRPCMessage(tool_call)))
                        forwarded.append(await conns[0].from_bridge.receive())
                if n < drops:
                    await anyio.sleep(0.05)
                    await conns[n].to_bridge.send(RuntimeError("daemon restarted"))
            await anyio.sleep(0.2)
            await c_in_send.aclose()
    return headers_seen, forwarded


def _params(message: SessionMessage) -> dict:
    root = message.message.root
    assert isinstance(root, JSONRPCRequest) and root.params is not None
    return cast("dict", root.params)


def test_the_bridge_sends_the_identity_on_the_first_connection_and_after_a_daemon_restart(
        relay_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = iter(["laptop", "laptop-renamed", "laptop-renamed-again"])
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: next(ids))
    headers, _ = anyio.run(_drive, monkeypatch, 1)
    assert len(headers) == 2
    first, second = (_lower(h or {}) for h in headers)
    assert first[identity.HEADER_MACHINE] == second[identity.HEADER_MACHINE] == LAPTOP_UUID
    assert first[identity.HEADER_MACHINE_ID] != second[identity.HEADER_MACHINE_ID]
    assert first["authorization"] == second["authorization"] == "Bearer tok"


def test_a_local_bridge_sends_no_identity_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_HOST", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    headers, _ = anyio.run(_drive, monkeypatch, 1)
    assert all(not [k for k in (h or {}) if k.lower().startswith("x-agent-context-")]
               for h in headers)


def test_a_remote_bridge_attaches_evidence_to_a_tool_call_with_a_cwd(
        relay_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    call = JSONRPCRequest(jsonrpc="2.0", id=2, method="tools/call", params={
        "name": "resolve_project", "arguments": {"cwd": str(tmp_path)}})
    _, forwarded = anyio.run(_drive, monkeypatch, 0, call)
    params = _params(forwarded[0])
    assert params["_meta"][identity.EVIDENCE_META_KEY]["cwd"] == str(tmp_path)


def test_a_local_bridge_forwards_tool_calls_unchanged(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_HOST", raising=False)
    call = JSONRPCRequest(jsonrpc="2.0", id=2, method="tools/call", params={
        "name": "resolve_project", "arguments": {"cwd": str(tmp_path)}})
    _, forwarded = anyio.run(_drive, monkeypatch, 0, call)
    params = _params(forwarded[0])
    assert "_meta" not in params
