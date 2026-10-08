'An unbound network caller is an unknown machine, never the daemon host.'
from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from agent_context import claims, daemon, identity, session, write_guard
from agent_context import fstools as T
from agent_context.machine import get_chezmoi_machine_id, get_machine_uuid
from agent_context.store import emit_toml, stable_uuid

LAPTOP = "AAAAAAAA-1111-2222-3333-LAPTOPLAPTOP"
NET_IP = "100.111.179.111"


@contextlib.contextmanager
def _caller(ip: str | None, token_id: str = "legacy-shared"):
    token = write_guard.bind(write_guard.Caller(token_id, None, ip, None))
    try:
        yield
    finally:
        write_guard.reset(token)


@contextlib.contextmanager
def _bound(uuid: str, machine_id: str | None = "laptop"):
    token = identity.bind(identity.Identity(machine_uuid=uuid, machine_id=machine_id,
                                            platform="darwin", home="/Users/x"))
    try:
        yield
    finally:
        identity.reset(token)


def _add_laptop(store) -> None:
    meta = {"type": "machine", "machine_uuid": LAPTOP, "hostname": "l", "platform": "darwin",
            "home_dir": "/Users/x", "display_name": "l", "machine_id": "laptop"}
    path = Path(store.root) / "machines" / f"{LAPTOP}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_toml(meta))
    meta["scope"] = "global"
    meta["uuid"] = stable_uuid("machine", "global", f"{LAPTOP}.toml")
    with store.lock:
        store._index(meta, str(path))


def _add_host(store) -> None:
    uid = get_machine_uuid()
    host = {"type": "machine", "machine_uuid": uid, "hostname": "h", "platform": "linux",
            "home_dir": "/home/x", "display_name": "h",
            **({"machine_id": get_chezmoi_machine_id()} if get_chezmoi_machine_id() else {})}
    path = Path(store.root) / "machines" / f"{uid}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_toml(host))
    host["scope"] = "global"
    host["uuid"] = stable_uuid("machine", "global", f"{uid}.toml")
    with store.lock:
        store._index(host, str(path))


def _machine_files(store) -> list[str]:
    root = Path(store.root) / "machines"
    return sorted(p.name for p in root.glob("*")) if root.exists() else []





def test_an_unknown_caller_gets_a_machine_block_of_nulls_and_a_warning(store) -> None:
    with _caller(NET_IP):
        out = T.get_session_context(store, "/nowhere")
    m = out["machine"]
    assert m["unknown"] is True
    for key in ("machine_uuid", "machine_id", "hostname", "display_name", "platform",
                "home_dir"):
        assert m[key] is None, key
    warning = out["machine_warning"]
    for word in ("relay", "token", "restart"):
        assert word in warning.lower(), word


def test_the_warning_names_a_long_running_desktop_relay(store) -> None:
    with _caller(NET_IP):
        warning = T.get_session_context(store, "/nowhere")["machine_warning"]
    assert "desktop" in warning.lower()





def test_an_unknown_caller_creates_no_machine_row(store) -> None:
    assert _machine_files(store) == []
    with _caller(NET_IP):
        T.get_session_context(store, "/nowhere")
    assert _machine_files(store) == []


def test_an_unknown_caller_records_no_claim(store, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(claims, "ensure_server_claim",
                        lambda *a, **k: calls.append("server") or None)
    monkeypatch.setattr(claims, "ensure_remote_claim",
                        lambda *a, **k: calls.append("remote") or None)
    monkeypatch.setattr(claims, "note_relay_seen", lambda *a, **k: calls.append("seen"))
    with _caller(NET_IP):
        T.get_session_context(store, "/nowhere", caller_pid=4242)
    assert calls == []





def test_an_unknown_caller_gets_no_host_inbox_or_digest(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    prefixes: list[str] = []
    real = session._inbox_items
    monkeypatch.setattr(session, "_inbox_items",
                        lambda s, prefix, scope: prefixes.append(prefix) or real(s, prefix, scope))
    digest_calls: list[str] = []
    monkeypatch.setattr(session, "audit_digest",
                        lambda s, host: digest_calls.append(host) or {"rows": [], "path": "",
                                                                      "title": ""})
    with _caller(NET_IP):
        T.get_session_context(store, "/nowhere")
    assert not [p for p in prefixes if p.startswith("inbox/machines/")]
    assert digest_calls == []


def test_an_unknown_caller_gets_no_daemon_health_block(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "get_health",
                        lambda: {"verdict": "restart-loop", "starts_last_hour": 9})
    with _caller(NET_IP):
        assert "daemon_health" not in T.get_session_context(store, "/nowhere")


def test_project_scope_still_loads_for_an_unknown_caller(store) -> None:
    with _caller(NET_IP):
        out = T.get_session_context(store, "/nowhere")
    assert "instructions" in out and "memory_index" in out and "inbox" in out





@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1", None])
def test_a_loopback_or_unaddressed_caller_keeps_the_host_identity(store, ip) -> None:
    with _caller(ip):
        out = T.get_session_context(store, "/nowhere")
    assert out["machine"]["machine_uuid"] == get_machine_uuid()
    assert "unknown" not in out["machine"]
    assert "machine_warning" not in out


def test_a_call_with_no_bound_caller_keeps_the_host_identity(store) -> None:
    out = T.get_session_context(store, "/nowhere")
    assert out["machine"]["machine_uuid"] == get_machine_uuid()
    assert "machine_warning" not in out


def test_a_loopback_caller_still_gets_the_daemon_health_block(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "get_health",
                        lambda: {"verdict": "restart-loop", "starts_last_hour": 9})
    with _caller("127.0.0.1"):
        assert "daemon_health" in T.get_session_context(store, "/nowhere")





def test_a_bound_remote_identity_keeps_its_machine_row(store) -> None:
    _add_laptop(store)
    with _caller(NET_IP), _bound(LAPTOP):
        out = T.get_session_context(store, "/nowhere")
    m = out["machine"]
    assert m["machine_id"] == "laptop" and m["platform"] == "darwin"
    assert m["home_dir"] == "/Users/x"
    assert "unknown" not in m and "machine_warning" not in out





def test_list_machines_marks_nothing_current_for_an_unknown_caller(store) -> None:
    _add_laptop(store)
    _add_host(store)
    with _caller(NET_IP):
        rows = session.list_machines(store)
    assert rows and not [r for r in rows if r["is_current"]]


def test_a_bound_caller_still_sees_its_own_machine_as_current(store) -> None:
    _add_laptop(store)
    with _caller(NET_IP), _bound(LAPTOP):
        rows = session.list_machines(store)
    assert [r["machine_id"] for r in rows if r["is_current"]] == ["laptop"]


def test_renaming_without_a_machine_is_refused_for_an_unknown_caller(store) -> None:
    _add_laptop(store)
    _add_host(store)
    before = sorted(store.entities.values(), key=lambda e: str(e.get("machine_uuid")))
    names = [e.get("display_name") for e in before]
    with _caller(NET_IP):
        out = session.set_machine_display_name(store, "renamed")
    assert "error" in out
    after = sorted(store.entities.values(), key=lambda e: str(e.get("machine_uuid")))
    assert [e.get("display_name") for e in after] == names


def test_renaming_a_named_machine_still_works_for_an_unknown_caller(store) -> None:
    _add_laptop(store)
    with _caller(NET_IP):
        out = session.set_machine_display_name(store, "renamed", machine=LAPTOP)
    assert out.get("display_name") == "renamed"





@pytest.mark.parametrize("mode", ["observe", "enforce", "off"])
def test_the_unknown_block_is_the_same_in_every_guard_mode(
        store, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", mode)
    with _caller(NET_IP):
        out = T.get_session_context(store, "/nowhere")
    assert out["machine"]["unknown"] is True and "machine_warning" in out





def test_a_failing_caller_lookup_falls_back_to_the_host_identity(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise RuntimeError("no caller")

    _add_host(store)
    monkeypatch.setattr(write_guard, "current", boom)
    out = T.get_session_context(store, "/nowhere")
    assert out["machine"]["machine_uuid"] == get_machine_uuid()





def test_an_unregistered_machine_uuid_is_still_refused(store) -> None:
    with _caller(NET_IP), _bound("NOT-IN-THE-STORE"):
        out = T.get_session_context(store, "/nowhere")
    assert "error" in out and "machine" not in out


def test_the_bound_response_keeps_its_shape(store) -> None:
    _add_laptop(store)
    with _caller(NET_IP), _bound(LAPTOP):
        bound = T.get_session_context(store, "/nowhere")
    with _caller(NET_IP):
        unknown = T.get_session_context(store, "/nowhere")
    assert set(bound["machine"]) == {"machine_uuid", "machine_id", "hostname", "display_name",
                                     "platform", "home_dir"}
    assert set(unknown["machine"]) == set(bound["machine"]) | {"unknown"}
