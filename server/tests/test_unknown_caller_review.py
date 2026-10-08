'Review findings on the unknown-caller change: address forms, register_path, audit stamping.'
from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from agent_context import audit, identity, integrity, session, write_guard
from agent_context.machine import get_chezmoi_machine_id, get_machine_uuid


@contextlib.contextmanager
def _caller(ip: str | None):
    token = write_guard.bind(write_guard.Caller("legacy-shared", None, ip, None))
    try:
        yield
    finally:
        write_guard.reset(token)


@pytest.mark.parametrize("ip", [
    "0:0:0:0:0:0:0:1", "::ffff:7f00:1", "::FFFF:127.0.0.1", "LOCALHOST", "127.0.0.1:5000",
    "[::1]", "::1%lo", " 127.0.0.1", "127.0.0.2", "[::1]:8765",
])
def test_every_loopback_spelling_is_the_host(ip: str) -> None:
    with _caller(ip):
        assert session.unknown_caller() is False


@pytest.mark.parametrize("ip", ["100.111.179.111", "10.0.0.5:443", "100.111.179.111 ",
                                "2001:db8::1", "[2001:db8::1]:443", "not-an-address"])
def test_a_network_address_in_any_form_is_unknown(ip: str) -> None:
    with _caller(ip):
        assert session.unknown_caller() is True


def test_register_path_is_refused_for_an_unknown_caller(
        store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(integrity, "register_machine_path",
                        lambda *a, **k: calls.append("wrote") or {"ok": True})
    with _caller("100.111.179.111"):
        out = session.machine_admin(store, "register_path", cwd=str(tmp_path), project="p")
    assert "error" in out and calls == []


def test_register_path_still_works_for_the_host(
        store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(integrity, "register_machine_path",
                        lambda *a, **k: calls.append("wrote") or {"ok": True})
    with _caller("127.0.0.1"):
        session.machine_admin(store, "register_path", cwd=str(tmp_path), project="p")
    assert calls == ["wrote"]


def test_register_path_still_refuses_a_bound_remote_identity(store, tmp_path: Path) -> None:
    token = identity.bind(identity.Identity("x", "laptop", "darwin", "/Users/x"))
    try:
        out = session.machine_admin(store, "register_path", cwd=str(tmp_path))
    finally:
        identity.reset(token)
    assert "error" in out


def test_an_unknown_caller_is_stamped_unknown_not_as_the_host() -> None:
    with _caller("100.111.179.111"):
        assert audit._this_machine() == "unknown"


def test_a_loopback_caller_is_still_stamped_with_the_host() -> None:
    with _caller("127.0.0.1"):
        assert audit._this_machine() == (get_chezmoi_machine_id() or get_machine_uuid())
