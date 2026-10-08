'Review fixes for the relay self-refresh work (R1 to R5).\n\nNames and behavior the implementation provides:\n  relay_update.start_etag(home=None) -> str   the installed etag as of the FIRST call for that home\n        in this process (Path.home() when None; identity uses $HOME); "" when there was none.\n        Later calls return the same value whatever the file holds. RelayUpdater.started_etag is\n        this value. identity.local_headers() sends x-relay-source-etag from it and omits the\n        header when it is "".\n  R2: check_once arms the waiter only in the round it also requests the exit: idle, just before\n        request_exit, once per etag. A busy round arms nothing.\n        (CONFLICT: test_relay_self_exit.py::test_the_waiter_is_armed_at_most_once_per_etag and\n        ::test_a_busy_relay_never_exits_and_exits_at_its_next_idle_window assert the waiter is\n        armed while busy; they contradict R2 and must be revised with it.)\n  R3: run_waiter counts a relay-alive.json as a respawn only when its pid differs from the exited\n        relay\'s AND its ts is >= the exit record\'s ts (relay-exit.json under ~/.cache).\n  R4: identity.from_headers accepts x-relay-source-etag only when it is 1 to 200 printable ASCII\n        characters; anything else gives relay_etag None.\n        claims.note_relay_etag(machine_uuid, etag, store) records only a uuid that has a machine\n        row in `store`, and its read-modify-write is serialized so concurrent calls lose nothing.\n  R5: relay-exit.json (health and cache copies), relay-alive.json and relay-etags.json are\n        written with mode 0600, including a rewrite of an existing 0644 file.'
import inspect
import json
import os
import platform
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_context import claims, daemon, identity, machine, paths
from agent_context import relay_update as U
from relay_self_refresh_support import *  
from relay_self_refresh_support import (CURRENT, STALE, LsState, Rig, alive_record, dead_pid,
                                        exit_record, health_record, need, notify_calls,
                                        read_json, store_etag, write_installer, write_notify)
from test_relay_version_visibility import (LAPTOP_UUID, _add_machine, _initialize, _rows,  
                                           daemon_side)

ETAG_HEADER = "x-relay-source-etag"
_note: Any = claims.note_relay_etag  


@pytest.fixture
def remote_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", "example.invalid")
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "tok")
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: LAPTOP_UUID)
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: "laptop")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")


def _relay_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, etag: str | None) -> Path:
    relay_home = tmp_path / "relay-home"
    relay_home.mkdir()
    monkeypatch.setenv("HOME", str(relay_home))
    if etag is not None:
        store_etag(relay_home, etag)
    return relay_home


def _sent_header(headers: dict[str, str]) -> str | None:
    return {k.lower(): v for k, v in headers.items()}.get(ETAG_HEADER)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)




def test_the_header_keeps_the_start_etag_after_the_file_is_rewritten(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_relay: None) -> None:
    relay_home = _relay_home(tmp_path, monkeypatch, STALE)
    assert need(U, "start_etag")(relay_home) == STALE
    assert _sent_header(daemon.connect_headers()) == STALE
    store_etag(relay_home, CURRENT)  
    assert _sent_header(daemon.connect_headers()) == STALE, "a reconnect reported the file's etag"
    assert _sent_header(identity.local_headers()) == STALE


def test_a_relay_with_no_etag_file_at_start_never_sends_the_header(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_relay: None) -> None:
    relay_home = _relay_home(tmp_path, monkeypatch, None)
    assert need(U, "start_etag")(relay_home) == ""
    assert ETAG_HEADER not in {k.lower() for k in identity.local_headers()}
    store_etag(relay_home, CURRENT)
    assert ETAG_HEADER not in {k.lower() for k in daemon.connect_headers()}, \
        "a file that appeared after the start was reported"








BAD_ETAGS = ["a" * 201, "v\x01v", "v\x7f", "\x1b[31mred", "v\xffv", "line\nbreak", "\x00", ""]


@pytest.mark.parametrize("value", BAD_ETAGS)
def test_from_headers_ignores_an_unusable_etag(value: str) -> None:
    who = identity.from_headers([(b"x-agent-context-machine", b"u-1"),
                                 (ETAG_HEADER.encode(), value.encode("utf-8"))])
    assert who is not None
    assert who.relay_etag is None, repr(who.relay_etag)


def test_from_headers_keeps_a_printable_etag_up_to_two_hundred_characters() -> None:
    for value in ('"' + "a" * 64 + '"', "x" * 200):
        who = identity.from_headers([(b"x-agent-context-machine", b"u-1"),
                                     (ETAG_HEADER.encode(), value.encode())])
        assert who is not None and who.relay_etag == value


@pytest.mark.parametrize("value", BAD_ETAGS[:-1])
def test_a_session_start_audits_an_unusable_etag_as_null(
        value: str, tmp_path: Path, daemon_side: Path) -> None:
    _initialize(LAPTOP_UUID, "laptop", value)
    rows = _rows(tmp_path, "session-init")
    assert len(rows) == 1 and rows[0]["relay_etag"] is None, rows


def test_the_audit_line_has_no_newline_in_it_for_an_etag_with_one(
        tmp_path: Path, daemon_side: Path) -> None:
    _initialize(LAPTOP_UUID, "laptop", "v1\nINJECTED")
    text = "".join(f.read_text() for f in sorted((tmp_path / "audit").glob("*.jsonl")))
    assert "INJECTED" not in text, "the unsanitized etag reached the audit log"
    lines = [line for line in text.splitlines() if line.strip()]
    assert all(json.loads(line) for line in lines)


def test_a_huge_etag_does_not_grow_the_recorded_file(tmp_path: Path, daemon_side: Path) -> None:
    _initialize(LAPTOP_UUID, "laptop", "x" * 100_000)
    recorded = paths.state_dir() / "relay-etags.json"
    size = recorded.stat().st_size if recorded.exists() else 0
    assert size < 500, f"{size} bytes recorded"


def test_note_relay_etag_takes_the_store_and_records_only_known_machines(
        store: Any, daemon_side: Path) -> None:
    signature = inspect.signature(claims.note_relay_etag)
    assert "store" in signature.parameters, "claims.note_relay_etag has no store parameter"
    assert _note("no-such-machine-uuid", STALE, store) is False
    assert "no-such-machine-uuid" not in claims.relay_etags()
    assert _note(LAPTOP_UUID, STALE, store) is True
    assert claims.relay_etags()[LAPTOP_UUID] == STALE


def test_a_session_of_an_unknown_machine_records_nothing(
        daemon_side: Path, tmp_path: Path) -> None:
    import asyncio

    from agent_context import server
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [(b"authorization", b"Bearer the-shared-secret"),
               (b"x-forwarded-for", b"100.111.179.111"),
               (identity.HEADER_MACHINE.encode(), b"ZZZZ-unknown"),
               (ETAG_HEADER.encode(), STALE.encode())]

    async def app(scope: Any, receive: Any, send: Any) -> None:
        return None

    scope = {"type": "http", "method": "POST", "client": ("127.0.0.1", 40000), "headers": headers}
    asyncio.run(server._counting_app(app)(scope, None, send))  
    assert sent and sent[0].get("status") == 403
    assert "ZZZZ-unknown" not in claims.relay_etags()


def test_fifty_concurrent_notes_for_fifty_machines_all_persist(
        store: Any, daemon_side: Path) -> None:
    assert "store" in inspect.signature(claims.note_relay_etag).parameters, \
        "claims.note_relay_etag has no store parameter"
    uuids = [f"MACHINE-{n:02d}-0000-0000-0000-000000000000" for n in range(50)]
    for n, uuid in enumerate(uuids):
        _add_machine(store, uuid, f"m{n}")
    barrier = threading.Barrier(len(uuids))

    def note(uuid: str) -> None:
        barrier.wait()
        _note(uuid, f'"etag-{uuid}"', store)

    threads = [threading.Thread(target=note, args=(u,)) for u in uuids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    recorded = json.loads((paths.state_dir() / "relay-etags.json").read_text())
    assert sorted(recorded) == sorted(uuids), f"{len(recorded)} of {len(uuids)} persisted"
    assert all(recorded[u] == f'"etag-{u}"' for u in uuids)




def test_the_recorded_etags_file_is_mode_0600(store: Any, daemon_side: Path) -> None:
    assert "store" in inspect.signature(claims.note_relay_etag).parameters, \
        "claims.note_relay_etag has no store parameter"
    assert _note(LAPTOP_UUID, STALE, store) is True
    assert _mode(paths.state_dir() / "relay-etags.json") == 0o600
