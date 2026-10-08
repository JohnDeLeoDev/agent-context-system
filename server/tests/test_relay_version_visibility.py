'The daemon can see which relay source each machine\'s relay runs (X8). No exit is needed.\n\nNames the implementation must provide:\n  identity.HEADER_RELAY_ETAG = "x-relay-source-etag"\n  identity.Identity.relay_etag  (str | None, default None), filled by `from_headers`\n  identity.local_headers()      adds the header from ~/.local/share/agent-context/relay-source.etag\n                                when that file exists and is not empty; omits it otherwise\n  audit line `session-init`     carries `relay_etag` (the header value, or null) next to machine_id\n  session context               `machine_warning` names a stale relay when the header etag differs\n                                from etag_of(build_relay_source(<store>/server))\n  fleet rows                    `relay_etag` and `relay_stale` for a machine whose relay reached ls\n                                (no header: relay_etag null, relay_stale true)\n  The current etag is cached per source-tree mtime: the bundle is not rebuilt per request.'
import asyncio
import json
import platform
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_context import daemon, identity, machine, relay_source, server, token_table, write_audit
from agent_context import fstools as T
from agent_context import write_guard
from agent_context.store import emit_toml, stable_uuid
from relay_self_refresh_support import *  
from relay_self_refresh_support import CURRENT, STALE, need

SHARED = "the-shared-secret"
LAPTOP_UUID = "AAAAAAAA-1111-2222-3333-LAPTOPLAPTOP"
M4_UUID = "BBBBBBBB-1111-2222-3333-M4M4M4M4M4M4"
OLD_UUID = "CCCCCCCC-1111-2222-3333-OLDOLDOLDOLD"
NET = (b"x-forwarded-for", b"100.111.179.111")
ETAG_HEADER = "x-relay-source-etag"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _relay_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, etag: str | None) -> Path:
    home = tmp_path / "relay-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if etag is not None:
        _write(home / ".local" / "share" / "agent-context" / "relay-source.etag", etag + "\n")
    return home


@pytest.fixture
def remote_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", "example.invalid")
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "tok")
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: LAPTOP_UUID)
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: "laptop")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")





def test_the_header_name_is_x_relay_source_etag() -> None:
    assert need(identity, "HEADER_RELAY_ETAG") == ETAG_HEADER


def test_a_relay_sends_its_installed_etag(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_relay: None) -> None:
    _relay_home(tmp_path, monkeypatch, STALE)
    headers = {k.lower(): v for k, v in identity.local_headers().items()}
    assert headers.get(ETAG_HEADER) == STALE, headers


def test_every_connection_the_bridge_opens_carries_the_etag(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_relay: None) -> None:
    home = _relay_home(tmp_path, monkeypatch, STALE)
    first = {k.lower(): v for k, v in daemon.connect_headers().items()}
    _write(home / ".local" / "share" / "agent-context" / "relay-source.etag", CURRENT + "\n")
    second = {k.lower(): v for k, v in daemon.connect_headers().items()}
    assert first.get(ETAG_HEADER) == STALE
    assert second.get(ETAG_HEADER) == STALE, "a reconnect sent the etag on disk now, not its start etag"
    assert first["authorization"] == "Bearer tok"


def test_a_relay_with_no_etag_file_omits_the_header(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_relay: None) -> None:
    need(identity, "HEADER_RELAY_ETAG")
    _relay_home(tmp_path, monkeypatch, None)
    assert ETAG_HEADER not in {k.lower() for k in identity.local_headers()}


def test_an_empty_etag_file_omits_the_header(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_relay: None) -> None:
    need(identity, "HEADER_RELAY_ETAG")
    _relay_home(tmp_path, monkeypatch, "")
    assert ETAG_HEADER not in {k.lower() for k in identity.local_headers()}


def test_the_header_and_the_identity_round_trip(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_relay: None) -> None:
    _relay_home(tmp_path, monkeypatch, STALE)
    who = identity.from_headers(list(identity.local_headers().items()))
    assert who is not None
    assert getattr(who, "relay_etag", "<no field>") == STALE


def test_a_request_without_the_header_has_no_relay_etag() -> None:
    who = identity.from_headers([(b"x-agent-context-machine", LAPTOP_UUID.encode())])
    assert who is not None
    assert getattr(who, "relay_etag", "<no field>") is None





def _add_machine(store, uuid: str, machine_id: str) -> None:
    meta = {"type": "machine", "machine_uuid": uuid, "hostname": machine_id, "platform": "darwin",
            "home_dir": "/Users/x", "display_name": machine_id, "machine_id": machine_id}
    path = Path(store.root) / "machines" / f"{uuid}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_toml(meta))
    meta["scope"] = "global"
    meta["uuid"] = stable_uuid("machine", "global", f"{uuid}.toml")
    with store.lock:
        store._index(meta, str(path))


def _publish_status(store, uuid: str, machine_id: str) -> None:
    'A fresh daemon-status row, so the machine is in the fleet view.'
    import time
    _write(Path(store.root) / "machines" / uuid / "daemon-status.json", json.dumps({
        "machine_id": machine_id, "hostname": machine_id, "build": "1", "code_version": 1.0,
        "code_current": True, "code_stale_since": None, "verdict": "healthy",
        "updated_at": int(time.time())}, separators=(",", ":")))


def _source_tree(store, body: str = "def main() -> int:\n    return 0\n") -> Path:
    tree = Path(store.root) / "server"
    _write(tree / "pyproject.toml", '[project]\nname = "agent-context"\nversion = "0.1.0"\n')
    _write(tree / "src" / "agent_context" / "__init__.py", "")
    _write(tree / "src" / "agent_context" / "server.py", body)
    return tree


def _current_etag(tree: Path) -> str:
    return relay_source.etag_of(relay_source.build_relay_source(tree))


@pytest.fixture
def daemon_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store) -> Path:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "observe")
    monkeypatch.setenv("AGENT_CONTEXT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", SHARED)
    monkeypatch.setattr(write_audit, "protect_file", lambda path: None)
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    write_guard.reset_state()
    server._SESSION_LOG_COUNTS.clear()
    _add_machine(store, LAPTOP_UUID, "laptop")
    return _source_tree(store)


def _rows(tmp_path: Path, op: str) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((tmp_path / "audit").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("op") == op]


def _initialize(uuid: str, machine_id: str, etag: str | None) -> None:
    headers = [(b"authorization", f"Bearer {SHARED}".encode()), NET,
               (b"user-agent", b"relay-test"),
               (identity.HEADER_MACHINE.encode(), uuid.encode()),
               (identity.HEADER_MACHINE_ID.encode(), machine_id.encode())]
    if etag is not None:
        headers.append((ETAG_HEADER.encode(), etag.encode()))

    async def app(scope, receive, send) -> None:
        return None

    scope = {"type": "http", "method": "POST", "client": ("127.0.0.1", 40000), "headers": headers}
    asyncio.run(server._counting_app(app)(scope, None, None))


def test_a_session_start_is_audited_with_its_machine_and_relay_etag(
        tmp_path: Path, daemon_side: Path) -> None:
    _initialize(LAPTOP_UUID, "laptop", STALE)
    lines = _rows(tmp_path, "session-init")
    assert len(lines) == 1
    assert lines[0]["machine_id"] == "laptop"
    assert lines[0].get("relay_etag") == STALE, lines[0]


def test_a_session_start_without_the_header_is_audited_with_a_null_etag(
        tmp_path: Path, daemon_side: Path) -> None:
    _initialize(LAPTOP_UUID, "laptop", None)
    line = _rows(tmp_path, "session-init")[0]
    assert "relay_etag" in line, line
    assert line["relay_etag"] is None


def _context_as(store, etag: str | None) -> dict:
    headers = [(identity.HEADER_MACHINE.encode(), LAPTOP_UUID.encode()),
               (identity.HEADER_MACHINE_ID.encode(), b"laptop")]
    if etag is not None:
        headers.append((ETAG_HEADER.encode(), etag.encode()))
    who = identity.from_headers(headers)
    assert who is not None
    token = identity.bind(replace(who, session_key="s-1"))
    try:
        return T.get_session_context(store, "/Users/x/nowhere")
    finally:
        identity.reset(token)


def test_a_stale_relay_gets_a_note_in_machine_warning(store, daemon_side: Path) -> None:
    assert need(identity, "HEADER_RELAY_ETAG") == ETAG_HEADER
    ctx = _context_as(store, STALE)
    warning = ctx.get("machine_warning")
    assert isinstance(warning, str) and "relay" in warning.lower(), ctx.keys()


def test_a_current_relay_gets_no_note(store, daemon_side: Path) -> None:
    assert need(identity, "HEADER_RELAY_ETAG") == ETAG_HEADER
    ctx = _context_as(store, _current_etag(daemon_side))
    assert "machine_warning" not in ctx
    assert _context_as(store, STALE).get("machine_warning"), "the stale one must be told"


def test_a_session_with_no_identity_gets_no_relay_note(store, daemon_side: Path) -> None:
    assert need(identity, "HEADER_RELAY_ETAG") == ETAG_HEADER
    assert "machine_warning" not in T.get_session_context(store, "/nowhere")


def test_the_current_etag_is_cached_until_the_source_tree_changes(
        store, daemon_side: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert need(identity, "HEADER_RELAY_ETAG") == ETAG_HEADER
    calls: list[int] = []
    real = relay_source.build_relay_source

    def counting(server_dir: Path) -> bytes:
        calls.append(1)
        return real(server_dir)

    for module in list(sys.modules.values()):
        if (getattr(module, "__name__", "").startswith("agent_context")
                and getattr(module, "build_relay_source", None) is real):
            monkeypatch.setattr(module, "build_relay_source", counting)
    first = _current_etag(daemon_side)   
    calls.clear()
    for _ in range(3):
        assert _context_as(store, first).get("machine_warning") is None
    assert len(calls) == 1, f"the bundle was built {len(calls)} times for three sessions"
    _source_tree(store, "def main() -> int:\n    return 1\n")
    import os
    bumped = daemon_side / "src" / "agent_context" / "server.py"
    os.utime(bumped, (2_000_000_000, 2_000_000_000))
    changed = _current_etag(daemon_side)
    assert changed != first
    calls.clear()
    assert _context_as(store, first).get("machine_warning"), "the old etag is stale after an edit"
    assert _context_as(store, changed).get("machine_warning") is None
    assert len(calls) == 1


def _fleet_rows(store) -> dict[str, dict]:
    rows = json.loads(server.get_fleet_health())["machines"]
    return {r["machine_id"]: r for r in rows if r.get("machine_id")}


def test_fleet_health_lists_each_relay_etag_and_whether_it_is_stale(
        store, daemon_side: Path) -> None:
    assert need(identity, "HEADER_RELAY_ETAG") == ETAG_HEADER
    _add_machine(store, M4_UUID, "m4")
    _add_machine(store, OLD_UUID, "oldrelay")
    for uuid, name in ((LAPTOP_UUID, "laptop"), (M4_UUID, "m4"), (OLD_UUID, "oldrelay")):
        _publish_status(store, uuid, name)
    current = _current_etag(daemon_side)
    _initialize(LAPTOP_UUID, "laptop", STALE)
    _initialize(M4_UUID, "m4", current)
    _initialize(OLD_UUID, "oldrelay", None)
    rows = _fleet_rows(store)
    assert rows["laptop"].get("relay_etag") == STALE and rows["laptop"].get("relay_stale") is True
    assert rows["m4"].get("relay_etag") == current and rows["m4"].get("relay_stale") is False
    assert "relay_etag" in rows["oldrelay"] and rows["oldrelay"]["relay_etag"] is None
    assert rows["oldrelay"].get("relay_stale") is True
