"Observe mode, which ls runs until user switches to enforce (policy), used to let a locked\ntest's change through with only a would-deny line: block-locked-test-edit on the calling\nhost was the only real enforcement, and a relay machine had no lock records for that hook\nto read. A lock is user's decision, not a token-scope question, so the server refuses it\nin every mode and hands back the approval question."
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_context import entities, token_table, write_audit, write_guard
from agent_context.write_guard import Caller, WriteDenied


@pytest.fixture(autouse=True)
def guard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_GUARD", "observe")
    monkeypatch.setenv("AGENT_CONTEXT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))
    monkeypatch.setattr(write_audit, "protect_file", lambda path: None)
    write_guard.reset_state()
    token_table.issue(tmp_path / "tokens.json", id="admin",
                      scopes=["read", "entity-write", "protected-write"])


def _lock(store, rel: str, body: str) -> None:
    locks = Path(store.root) / "global" / "test-locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / (rel.replace("/", "__") + ".json")).write_text(json.dumps(
        {"locked_at": "2026-09-26T00:00:00Z", "path": rel,
         "sha256": hashlib.sha256(body.encode()).hexdigest()}))


def test_observe_mode_still_refuses_a_locked_test_and_names_the_approval(store) -> None:
    original = "def test_a():\n    assert True\n"
    entities.upsert_script(store, "test-x", script_body=original, language="py")
    _lock(store, "global/scripts/test-x.py", original)
    bound = write_guard.bind(Caller(token_id="admin", machine_id="m4", ip="100.64.0.9",
                                    session_key="s-1"))
    try:
        with pytest.raises(WriteDenied) as info:
            entities.upsert_script(store, "test-x", script_body="assert False\n", language="py")
    finally:
        write_guard.reset(bound)
    text = str(info.value)
    assert "locked test" in text
    path = str(Path(store.root) / "global" / "scripts" / "test-x.py")
    assert f"[approval:test-unlock:{path}:0]" in text


def test_observe_mode_still_lets_an_unlocked_script_through(store) -> None:
    bound = write_guard.bind(Caller(token_id="admin", machine_id="m4", ip="100.64.0.9",
                                    session_key="s-1"))
    try:
        entities.upsert_script(store, "free", script_body="print(1)\n", language="py")
    finally:
        write_guard.reset(bound)
