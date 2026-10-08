'policy through the real HTTP app: two relay sessions on two machines message each other.\n\nThe tools name the caller from its MCP session id, which only a real request carries, and the\nowner map is filled by the ASGI wrapper at `initialize`. test_messaging.py covers the rest\nwith stand-ins.'
from types import ModuleType

from mcp.server.streamable_http import GET_STREAM_KEY
from starlette.testclient import TestClient
from test_write_guard_http import (  
    MACHINE,
    OTHER_MACHINE,
    _call,
    _open_session,
    _text,
    client,
    env,
    srv,
)

from agent_context import identity, peer_wake

KEY_A = "a" * 32
KEY_B = "b" * 32


def _relay(machine: str, bridge: str) -> dict[str, str]:
    return {identity.HEADER_MACHINE: machine, peer_wake.BRIDGE_HEADER: bridge}


def test_two_relay_sessions_on_two_machines_exchange_a_message(
        client: TestClient, env: dict[str, str], srv: ModuleType) -> None:  
    laptop = _open_session(client, env["laptop"], **_relay(MACHINE, KEY_A))
    rp = _open_session(client, env["rp"], **_relay(OTHER_MACHINE, KEY_B))
    owners = srv._SESSION_OWNERS
    assert owners[laptop["mcp-session-id"]]["session_key"] == KEY_A
    assert owners[rp["mcp-session-id"]] == {
        "session_key": KEY_B, "machine_uuid": OTHER_MACHINE, "machine_id": "rp", "wake": None}

    
    assert _text(_call(client, laptop, "list_agents", {})) == "No other live session."
    for headers in (laptop, rp):
        transport = srv.mcp.session_manager._server_instances[headers["mcp-session-id"]]
        transport._request_streams[GET_STREAM_KEY] = object()   

    listed = _text(_call(client, laptop, "list_agents", {}))
    assert listed.startswith("rp [bbbbbbbb] · rp · ") and listed.endswith(" · queued")
    sent = _text(_call(client, laptop, "send_message", {"to": "rp", "message": "tests pass?"}))
    assert '"to":"rp [bbbbbbbb]"' in sent and "queued" in sent

    assert _text(_call(client, laptop, "read_notifications", {})) == "No notifications."
    got = _text(_call(client, rp, "read_notifications", {}))
    assert got == ('<cross-session-message from="laptop [aaaaaaaa]" via="agent-context">\ntests pass?\n'
                   "</cross-session-message>")
    reply = _text(_call(client, rp, "send_message",
                        {"to": "laptop [aaaaaaaa]", "message": "they do"}))
    assert '"to":"laptop [aaaaaaaa]"' in reply
    assert "they do" in _text(_call(client, laptop, "read_notifications", {}))
