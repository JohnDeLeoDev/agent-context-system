"Session traffic is logged so an unbound machine can be diagnosed from ls.\n\nThe laptop's Claude sessions reach ls with no bound machine and nothing on ls says why. With a\nvalid bearer:\n  - every session initialize (a POST with no `mcp-session-id`), bound or not, writes one\n    `session-init` audit line: token id, bound machine id (or none), client address, user agent,\n    the NAMES of the x-agent-context-* headers seen, and `bound`;\n  - every later request of a session that is still unbound and comes from a non-loopback address\n    writes an `unbound-request` line with the same fields plus the mcp-session-id length, up to\n    five per session so a busy session cannot flood the log;\n  - an unbound non-loopback initialize also logs one warning.\nHeader values are never recorded. Logging only: the request is served exactly as before."
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from agent_context import identity, server, token_table, write_audit, write_guard
from agent_context.store import emit_toml, stable_uuid

SHARED = "the-shared-secret"
MACHINE = "AAAAAAAA-1111-2222-3333-LAPTOPLAPTOP"
NET = (b"x-forwarded-for", b"100.111.179.111")
UA = (b"user-agent", b"python-httpx/0.28.1")


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "observe")
    monkeypatch.setenv("AGENT_CONTEXT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", SHARED)
    monkeypatch.setattr(write_audit, "protect_file", lambda path: None)
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    write_guard.reset_state()
    server._SESSION_LOG_COUNTS.clear()


def _add_laptop(store) -> None:
    meta = {"type": "machine", "machine_uuid": MACHINE, "hostname": "l", "platform": "darwin",
            "home_dir": "/Users/x", "display_name": "l", "machine_id": "laptop"}
    path = Path(store.root) / "machines" / f"{MACHINE}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_toml(meta))
    meta["scope"] = "global"
    meta["uuid"] = stable_uuid("machine", "global", f"{MACHINE}.toml")
    with store.lock:
        store._index(meta, str(path))


def _rows(tmp_path: Path, op: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((tmp_path / "audit").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if op is None or r.get("op") == op]


def _call(headers: list[tuple[bytes, bytes]], client: str = "127.0.0.1",
          method: str = "POST") -> list[int]:
    ran: list[int] = []

    async def app(scope, receive, send) -> None:
        ran.append(1)

    scope = {"type": "http", "method": method, "client": (client, 40000), "headers": headers}
    asyncio.run(server._counting_app(app)(scope, None, None))
    return ran


def _auth(token: str = SHARED) -> tuple[bytes, bytes]:
    return (b"authorization", f"Bearer {token}".encode())


def _sid(value: str = "sess-1") -> tuple[bytes, bytes]:
    return (b"mcp-session-id", value.encode())





def test_an_unbound_network_initialize_is_audited_and_warned(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="agent-context"):
        assert _call([_auth(), NET, UA]) == [1]
    lines = _rows(tmp_path, "session-init")
    assert len(lines) == 1
    line = lines[0]
    assert line["token_id"] == "legacy-shared"
    assert line["machine_id"] is None
    assert line["ip"] == "100.111.179.111"
    assert line["user_agent"] == "python-httpx/0.28.1"
    assert line["headers"] == []
    assert line["bound"] is False
    assert line["session_id_len"] == 0
    assert line["kind"] == "session" and line["decision"] == "observed"
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING
              and "unbound network session" in r.getMessage()]
    assert len(warned) == 1 and "100.111.179.111" in warned[0].getMessage()


def test_a_bound_initialize_is_audited_with_its_machine_and_not_warned(
        store, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _add_laptop(store)
    hdrs = [_auth(), NET, UA, (identity.HEADER_MACHINE.encode(), MACHINE.encode()),
            (identity.HEADER_MACHINE_ID.encode(), b"laptop")]
    with caplog.at_level(logging.INFO, logger="agent-context"):
        assert _call(hdrs) == [1]
    line = _rows(tmp_path, "session-init")[0]
    assert line["bound"] is True and line["machine_id"] == "laptop"
    assert line["headers"] == ["x-agent-context-machine", "x-agent-context-machine-id"]
    assert not [r for r in caplog.records if "unbound network session" in r.getMessage()]


def test_a_machine_bound_token_initialize_reports_its_machine(tmp_path: Path, store) -> None:
    _add_laptop(store)
    secret = token_table.issue(tmp_path / "tokens.json", id="laptop", machine_uuid=MACHINE,
                               machine_id="laptop", scopes=["read"])
    _call([_auth(secret), NET, UA])
    line = _rows(tmp_path, "session-init")[0]
    assert line["bound"] is True and line["machine_id"] == "laptop"
    assert line["token_id"] == "laptop"


def test_a_loopback_initialize_is_audited_but_not_warned(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="agent-context"):
        _call([_auth(), UA], client="127.0.0.1")
    assert len(_rows(tmp_path, "session-init")) == 1
    assert not [r for r in caplog.records if "unbound network session" in r.getMessage()]


def test_only_header_names_are_recorded_never_their_values(tmp_path: Path) -> None:
    hdrs = [_auth(), NET, (b"x-agent-context-platform", b"darwin"),
            (b"x-agent-context-home", b"/Users/secretname"), (b"x-agent-context-machine", b"  ")]
    _call(hdrs)
    line = _rows(tmp_path, "session-init")[0]
    assert line["headers"] == ["x-agent-context-home", "x-agent-context-machine",
                               "x-agent-context-platform"]
    text = json.dumps(line)
    assert "darwin" not in text and "secretname" not in text


def test_a_long_user_agent_is_capped_and_a_missing_one_is_empty(tmp_path: Path) -> None:
    _call([_auth(), NET, (b"user-agent", b"x" * 5000)])
    _call([_auth(), NET])
    lines = _rows(tmp_path, "session-init")
    assert len(lines[0]["user_agent"]) <= 300
    assert lines[1]["user_agent"] == ""





def test_a_later_unbound_request_is_audited_with_the_session_id_length(
        tmp_path: Path) -> None:
    _call([_auth(), NET, UA])
    _call([_auth(), NET, UA, _sid("abcdef0123456789")])
    line = _rows(tmp_path, "unbound-request")[0]
    assert line["session_id_len"] == 16
    assert line["ip"] == "100.111.179.111" and line["user_agent"] == "python-httpx/0.28.1"
    assert line["headers"] == [] and line["bound"] is False
    assert "abcdef0123456789" not in json.dumps(line)


def test_unbound_request_lines_stop_after_five_per_session(tmp_path: Path) -> None:
    _call([_auth(), NET, UA])
    for _ in range(9):
        _call([_auth(), NET, UA, _sid("sess-1")])
    _call([_auth(), NET, UA, _sid("sess-2")])
    lines = _rows(tmp_path, "unbound-request")
    assert len(lines) == 6                     
    assert len(_rows(tmp_path, "session-init")) == 1


def test_a_bound_session_writes_no_request_lines(store, tmp_path: Path) -> None:
    _add_laptop(store)
    hdrs = [_auth(), NET, UA, (identity.HEADER_MACHINE.encode(), MACHINE.encode())]
    _call(hdrs)
    _call([*hdrs, _sid()])
    _call([*hdrs, _sid()])
    assert _rows(tmp_path, "unbound-request") == []


def test_a_loopback_unbound_session_writes_no_request_lines(tmp_path: Path) -> None:
    _call([_auth(), UA], client="127.0.0.1")
    _call([_auth(), UA, _sid()], client="127.0.0.1")
    assert _rows(tmp_path, "unbound-request") == []


def test_a_forwarded_address_from_a_non_loopback_peer_does_not_count_as_loopback(
        tmp_path: Path) -> None:
    _call([_auth(), (b"x-forwarded-for", b"127.0.0.1")], client="203.0.113.5")
    assert _rows(tmp_path, "session-init")[0]["ip"] == "203.0.113.5"





def test_no_bearer_and_a_bad_bearer_write_nothing(tmp_path: Path) -> None:
    _call([NET, UA])
    _call([_auth("not-a-token"), NET, UA])
    _call([_auth("not-a-token"), NET, UA, _sid()])
    assert _rows(tmp_path) == []


def test_the_event_stream_get_writes_nothing(tmp_path: Path) -> None:
    _call([_auth(), NET, UA, _sid()], method="GET")
    assert _rows(tmp_path) == []


@pytest.mark.parametrize("mode", ["observe", "enforce", "off"])
def test_the_lines_are_written_in_every_guard_mode(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", mode)
    assert _call([_auth(), NET, UA]) == [1]
    assert len(_rows(tmp_path, "session-init")) == 1


def test_a_failing_audit_write_does_not_stop_the_request(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(write_audit.WriteAudit, "record", boom)
    assert _call([_auth(), NET, UA]) == [1]
    assert _call([_auth(), NET, UA, _sid()]) == [1]


def test_the_chain_still_verifies_with_the_new_lines(tmp_path: Path) -> None:
    _call([_auth(), NET, UA])
    _call([_auth(), NET, UA, _sid()])
    logs = sorted((tmp_path / "audit").glob("*.jsonl"))
    assert logs, "no audit log was written"
    assert write_audit.verify_chain(logs[0]) == (True, None)
    assert len(_rows(tmp_path)) == 2


def test_the_request_is_bound_and_served_exactly_as_before() -> None:
    seen: list[str] = []

    async def app(scope, receive, send) -> None:
        who = write_guard.current()
        seen.append(who.token_id if who else "-")

    scope = {"type": "http", "method": "POST", "client": ("127.0.0.1", 1),
             "headers": [_auth(), NET, UA]}
    asyncio.run(server._counting_app(app)(scope, None, None))
    assert seen == ["legacy-shared"]
