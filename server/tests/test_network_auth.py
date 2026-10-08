'C4: network auth. The MCP transport enforces the bearer token on every request,\nincluding a get_materialized tool call, and accepts the vhost hostname named in\nAGENT_CONTEXT_ALLOWED_HOSTS (and no other).'
import importlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

TOKEN = "s3cret-token-for-tests"
FQDN = "example.invalid"
INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _load_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, token: str | None,
                 allowed_hosts: str | None) -> ModuleType:
    'Import agent_context.server fresh under the given env, so `mcp` is built with it.'
    for key, value in (("AGENT_CONTEXT_TOKEN", token), ("AGENT_CONTEXT_ALLOWED_HOSTS", allowed_hosts)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    from agent_context import server as srv

    srv = importlib.reload(srv)
    monkeypatch.setattr(srv, "_get_conn", lambda: SimpleNamespace(root=tmp_path))
    return srv


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[object]:
    'Factory: build a TestClient over the streamable-HTTP app under a given env.'
    stack: list[TestClient] = []

    def make(*, token: str | None = TOKEN, allowed_hosts: str | None = None,
             host: str = "127.0.0.1:8765") -> TestClient:
        srv = _load_server(monkeypatch, tmp_path, token=token, allowed_hosts=allowed_hosts)
        client = TestClient(srv.mcp.streamable_http_app(), base_url=f"http://{host}")
        client.__enter__()
        stack.append(client)
        return client

    yield make
    for client in stack:
        client.__exit__(None, None, None)
    
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_ALLOWED_HOSTS", raising=False)
    from agent_context import server as srv

    importlib.reload(srv)















def test_mcp_is_open_when_no_token_configured(make_client) -> None:
    client = make_client(token=None)
    assert client.post("/mcp", json=INIT_BODY, headers=MCP_HEADERS).status_code == 200







def test_listed_host_with_valid_token_initializes(make_client) -> None:
    client = make_client(allowed_hosts=FQDN, host=FQDN)
    response = client.post("/mcp", json=INIT_BODY, headers={**MCP_HEADERS, **_bearer(TOKEN)})
    assert response.status_code == 200


def test_allowed_hosts_accepts_a_comma_list(make_client) -> None:
    client = make_client(allowed_hosts=f"other.example, {FQDN}", host=FQDN)
    response = client.post("/mcp", json=INIT_BODY, headers={**MCP_HEADERS, **_bearer(TOKEN)})
    assert response.status_code == 200







def test_fqdn_is_rejected_when_not_listed(make_client) -> None:
    client = make_client(allowed_hosts=None, host=FQDN)
    response = client.post("/mcp", json=INIT_BODY, headers={**MCP_HEADERS, **_bearer(TOKEN)})
    assert response.status_code == 421


def test_unlisted_host_is_rejected_even_when_others_are_allowed(make_client) -> None:
    client = make_client(allowed_hosts=FQDN, host="evil.example")
    response = client.post("/mcp", json=INIT_BODY, headers={**MCP_HEADERS, **_bearer(TOKEN)})
    assert response.status_code == 421







def test_mcp_without_token_is_401(make_client) -> None:
    client = make_client(token=TOKEN)
    assert client.post("/mcp", json=INIT_BODY, headers=MCP_HEADERS).status_code == 401


def test_mcp_with_wrong_token_is_401(make_client) -> None:
    client = make_client(token=TOKEN)
    response = client.post("/mcp", json=INIT_BODY, headers={**MCP_HEADERS, **_bearer("nope")})
    assert response.status_code == 401


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765"])
def test_localhost_hosts_still_work_with_allowed_hosts_set(make_client, host: str) -> None:
    client = make_client(allowed_hosts=FQDN, host=host)
    response = client.post("/mcp", json=INIT_BODY, headers={**MCP_HEADERS, **_bearer(TOKEN)})
    assert response.status_code == 200
