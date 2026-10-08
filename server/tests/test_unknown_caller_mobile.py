'The mobile OAuth connector is an unknown caller with its own, relay-free warning.'
from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from agent_context import claims, daemon, identity, integrity, session, token_table, write_guard
from agent_context import fstools as T
from agent_context.machine import get_machine_uuid
from agent_context.store import emit_toml, stable_uuid

NET_IP = "100.111.179.111"
LAPTOP = "AAAAAAAA-1111-2222-3333-LAPTOPLAPTOP"


@contextlib.contextmanager
def _caller(token_id: str, ip: str | None = NET_IP):
    token = write_guard.bind(write_guard.Caller(token_id, None, ip, None))
    try:
        yield
    finally:
        write_guard.reset(token)


def _warning(store, token_id: str) -> str:
    with _caller(token_id):
        return T.get_session_context(store, "/nowhere")["machine_warning"]


def test_the_connector_id_matches_the_token_table() -> None:
    assert token_table.OAUTH_ID == "mobile-oauth"


def test_the_connector_gets_a_null_machine_block_and_a_short_warning(store) -> None:
    with _caller("mobile-oauth"):
        out = T.get_session_context(store, "/nowhere")
    m = out["machine"]
    assert m["unknown"] is True
    for key in ("machine_uuid", "machine_id", "hostname", "display_name", "platform",
                "home_dir"):
        assert m[key] is None, key
    text = out["machine_warning"].lower()
    assert "client" in text and "no machine identity" in text
    for word in ("relay", "install", "restart", "token", "desktop"):
        assert word not in text, word
    assert len(text) < 160


def test_another_unknown_caller_keeps_the_relay_warning(store) -> None:
    for token_id in ("legacy-shared", "some-table-token"):
        text = _warning(store, token_id).lower()
        assert "relay" in text and "restart" in text, token_id
    assert _warning(store, "mobile-oauth") != _warning(store, "legacy-shared")


def test_the_connector_gets_the_unknown_caller_protections(
        store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(claims, "ensure_server_claim", lambda *a, **k: calls.append("claim"))
    monkeypatch.setattr(claims, "ensure_remote_claim", lambda *a, **k: calls.append("claim"))
    monkeypatch.setattr(integrity, "register_machine_path",
                        lambda *a, **k: calls.append("register") or {"ok": True})
    monkeypatch.setattr(daemon, "get_health",
                        lambda: {"verdict": "restart-loop", "starts_last_hour": 9})
    prefixes: list[str] = []
    real = session._inbox_items
    monkeypatch.setattr(session, "_inbox_items",
                        lambda s, p, sc: prefixes.append(p) or real(s, p, sc))
    with _caller("mobile-oauth"):
        out = T.get_session_context(store, "/nowhere", caller_pid=4242)
        reg = session.machine_admin(store, "register_path", cwd=str(tmp_path), project="p")
        rename = session.set_machine_display_name(store, "x")
    assert "daemon_health" not in out
    assert not [p for p in prefixes if p.startswith("inbox/machines/")]
    assert calls == []
    assert "error" in reg and "error" in rename
    assert not list((Path(store.root) / "machines").glob("*")) if (
        Path(store.root) / "machines").exists() else True


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", None])
def test_the_connector_from_loopback_keeps_the_host_identity(store, ip) -> None:
    with _caller("mobile-oauth", ip):
        out = T.get_session_context(store, "/nowhere")
    assert out["machine"]["machine_uuid"] == get_machine_uuid()
    assert "unknown" not in out["machine"] and "machine_warning" not in out


def test_a_bound_connector_keeps_its_machine_row(store) -> None:
    meta = {"type": "machine", "machine_uuid": LAPTOP, "hostname": "l", "platform": "darwin",
            "home_dir": "/Users/x", "display_name": "l", "machine_id": "laptop"}
    path = Path(store.root) / "machines" / f"{LAPTOP}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_toml(meta))
    meta["scope"] = "global"
    meta["uuid"] = stable_uuid("machine", "global", f"{LAPTOP}.toml")
    with store.lock:
        store._index(meta, str(path))
    bound = identity.bind(identity.Identity(LAPTOP, "laptop", "darwin", "/Users/x"))
    try:
        with _caller("mobile-oauth"):
            out = T.get_session_context(store, "/nowhere")
    finally:
        identity.reset(bound)
    assert out["machine"]["machine_id"] == "laptop"
    assert "unknown" not in out["machine"] and "machine_warning" not in out


class _BrokenCaller:
    ip = NET_IP

    @property
    def token_id(self) -> str:
        raise RuntimeError("no token id")


def test_a_failing_token_lookup_falls_back_to_the_relay_warning(
        store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(write_guard, "current", lambda: _BrokenCaller())
    out = T.get_session_context(store, "/nowhere")
    assert out["machine"]["unknown"] is True
    assert "relay" in out["machine_warning"].lower()


@pytest.mark.parametrize("mode", ["observe", "enforce", "off"])
def test_the_connector_warning_is_the_same_in_every_guard_mode(
        store, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", mode)
    text = _warning(store, "mobile-oauth").lower()
    assert "no machine identity" in text and "relay" not in text
