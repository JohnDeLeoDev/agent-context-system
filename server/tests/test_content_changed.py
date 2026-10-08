'C3: content_changed push. Broadcast on store write.'
import os
import sys
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from agent_context.store import ContextStore


def _make_ft(write_stream: Any, terminated: bool = False) -> Any:
    'A stand-in for StreamableHTTPServerTransport with the two attrs the broadcast reads.'
    return SimpleNamespace(
        _write_stream=write_stream, is_terminated=terminated, mcp_session_id="test"
    )


def _install_fake_mcp(monkeypatch: pytest.MonkeyPatch, transports: dict[str, Any]) -> None:
    'Point server.mcp at a stub whose session_manager holds the given transports.'
    from agent_context import server as srv

    fake_sm = SimpleNamespace(_server_instances=transports)
    monkeypatch.setattr(srv, "mcp", SimpleNamespace(session_manager=fake_sm))







def test_upsert_fires_write_callback(store: ContextStore):
    "upsert invokes the registered callback with the entity's relative path."
    paths: list[str] = []
    store.set_write_callback(lambda p: paths.extend(p))  

    store.upsert("doc", "test.md", {"title": "test"}, body="hello")

    assert paths, "callback did not fire on upsert"
    rel = os.path.relpath(os.path.join(store.root, "global", "docs", "test.md"), store.root)
    assert any(rel in p for p in paths), f"expected {rel!r} in {paths}"


def test_delete_fires_write_callback(store: ContextStore):
    "delete invokes the registered callback with the entity's relative path."
    store.upsert("doc", "test.md", {"title": "test"}, body="hello")
    paths: list[str] = []
    store.set_write_callback(lambda p: paths.extend(p))  

    store.delete("doc", "test.md")

    assert paths, "callback did not fire on delete"
    rel = "global/docs/test.md"
    assert any(rel in p for p in paths), f"expected {rel!r} in {paths}"


def test_callback_receives_list_of_paths(store: ContextStore):
    'The callback argument is a list, not a single string.'
    seen: list[list[str]] = []
    store.set_write_callback(lambda p: seen.append(p))  
    store.upsert("doc", "test.md", {"title": "test"}, body="hello")
    assert len(seen) == 1
    assert isinstance(seen[0], list)
    assert all(isinstance(p, str) for p in seen[0])







def test_broadcast_sends_notification_to_all_transports(monkeypatch: pytest.MonkeyPatch):
    '_broadcast_content_changed sends a JSONRPCNotification to every transport.'
    from agent_context import server as srv

    results1: list[SessionMessage] = []
    results2: list[SessionMessage] = []

    async def _run():
        write1, read1 = anyio.create_memory_object_stream[SessionMessage](16)
        write2, read2 = anyio.create_memory_object_stream[SessionMessage](16)
        _install_fake_mcp(monkeypatch, {"s1": _make_ft(write1), "s2": _make_ft(write2)})

        async def drain(r: list, rs: Any) -> None:
            async with rs:
                async for msg in rs:
                    r.append(msg)  

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain, results1, read1)
            tg.start_soon(drain, results2, read2)
            await srv._broadcast_content_changed(["global/docs/test.md"])  
            await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

    anyio.run(_run)

    assert len(results1) >= 1, "transport 1 received no notification"
    assert len(results2) >= 1, "transport 2 received no notification"


def test_broadcast_no_sessions_is_noop():
    '_broadcast_content_changed with an empty _server_instances raises nothing.'
    from agent_context import server as srv

    async def _run():
        await srv._broadcast_content_changed(["global/docs/test.md"])  

    anyio.run(_run)  







def test_broadcast_notification_shape(monkeypatch: pytest.MonkeyPatch):
    'The notification is a JSONRPCNotification with the content_changed method.'
    from agent_context import server as srv

    sent: list[SessionMessage] = []

    async def _run():
        write_stream, read_stream = anyio.create_memory_object_stream[SessionMessage](16)
        _install_fake_mcp(monkeypatch, {"s1": _make_ft(write_stream)})

        async def drain() -> None:
            async with read_stream:
                async for msg in read_stream:
                    sent.append(msg)  

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain)
            await srv._broadcast_content_changed(["global/docs/a.md", "global/docs/b.md"])  
            await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

    anyio.run(_run)

    assert len(sent) == 1, f"expected 1 message, got {len(sent)}"
    msg = sent[0]
    assert isinstance(msg, SessionMessage)
    inner = msg.message
    assert isinstance(inner, JSONRPCMessage)
    root = inner.root
    assert isinstance(root, JSONRPCNotification)
    assert root.method == "notifications/agent-context/content_changed"
    assert root.params == {"paths": ["global/docs/a.md", "global/docs/b.md"]}







def test_broadcast_registered_in_daemon_init():
    "The daemon init registers _broadcast_content_changed as the store's write callback."
    import inspect

    from agent_context import server as srv

    src = inspect.getsource(srv)
    assert "set_write_callback" in src, (
        "server.py must call store.set_write_callback(_broadcast_content_changed)"
    )
