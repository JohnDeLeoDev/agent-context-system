'OAuth 2.1 for custom connectors (the Claude mobile app asks for a client id and secret, not a\nbearer token). The daemon becomes its own authorization server: one static client, authorization\ncode with PKCE (S256), a passphrase on the consent page, rotating refresh tokens persisted hashed.\nThe static bearer token keeps working for relays. Enabled only when the token, public URL, client\nid, client secret and passphrase are all set.'
import base64
import hashlib
import importlib
import json
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

FQDN = "example.invalid"
PUBLIC = f"https://{FQDN}"
BEARER = "static-bearer-for-tests"
CLIENT_ID = "claude-mobile"
CLIENT_SECRET = "client-secret-for-tests"
PASSPHRASE = "correct horse battery staple"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
INIT_BODY = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
ENV = {
    "AGENT_CONTEXT_TOKEN": BEARER,
    "AGENT_CONTEXT_PUBLIC_URL": PUBLIC,
    "AGENT_CONTEXT_OAUTH_CLIENT_ID": CLIENT_ID,
    "AGENT_CONTEXT_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
    "AGENT_CONTEXT_OAUTH_PASSPHRASE": PASSPHRASE,
    "AGENT_CONTEXT_ALLOWED_HOSTS": FQDN,
}
OAUTH_KEYS = [k for k in ENV if k != "AGENT_CONTEXT_TOKEN"] + [
    "AGENT_CONTEXT_OAUTH_REDIRECT_URIS", "AGENT_CONTEXT_OAUTH_AUTH_METHOD", "AGENT_CONTEXT_OAUTH_STATE",
]
Make = Callable[..., TestClient]


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str]) -> ModuleType:
    for key in [*ENV, *OAUTH_KEYS]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_CONTEXT_OAUTH_STATE", str(tmp_path / "oauth-tokens.json"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from agent_context import server as srv

    srv = importlib.reload(srv)
    monkeypatch.setattr(srv, "_get_conn", lambda: SimpleNamespace(root=tmp_path))
    return srv


@pytest.fixture
def make(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Make]:
    stack: list[TestClient] = []

    def factory(env: dict[str, str] | None = None) -> TestClient:
        srv = _load(monkeypatch, tmp_path, ENV if env is None else env)
        client = TestClient(srv.mcp.streamable_http_app(), base_url=PUBLIC, follow_redirects=False)
        client.__enter__()
        stack.append(client)
        return client

    yield factory
    for client in stack:
        client.__exit__(None, None, None)
    for key in [*ENV, *OAUTH_KEYS]:
        monkeypatch.delenv(key, raising=False)
    from agent_context import server as srv

    importlib.reload(srv)


def authorize(client: TestClient, *, challenge: str, client_id: str = CLIENT_ID,
              redirect_uri: str = REDIRECT, state: str = "st-123", method: str = "S256"):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
              "code_challenge": challenge, "code_challenge_method": method, "state": state}
    return client.get("/authorize", params=params)


def request_id_of(response) -> str:
    location = response.headers["location"]
    assert location.startswith(f"{PUBLIC}/oauth/consent"), location
    return parse_qs(urlparse(location).query)["request_id"][0]


def consent(client: TestClient, request_id: str, passphrase: str = PASSPHRASE):
    return client.post("/oauth/consent", data={"request_id": request_id, "passphrase": passphrase})


def get_code(client: TestClient, challenge: str) -> str:
    response = consent(client, request_id_of(authorize(client, challenge=challenge)))
    assert response.status_code == 302, response.text
    return parse_qs(urlparse(response.headers["location"]).query)["code"][0]


def exchange(client: TestClient, code: str, verifier: str, *, secret: str | None = CLIENT_SECRET,
             redirect_uri: str = REDIRECT):
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": CLIENT_ID, "redirect_uri": redirect_uri}
    if secret is not None:
        data["client_secret"] = secret
    return client.post("/token", data=data)


def login(client: TestClient) -> dict:
    verifier, challenge = pkce()
    response = exchange(client, get_code(client, challenge), verifier)
    assert response.status_code == 200, response.text
    return response.json()


def refresh(client: TestClient, token: str):
    return client.post("/token", data={"grant_type": "refresh_token", "refresh_token": token,
                                       "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})


def mcp_init(client: TestClient, token: str | None):
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json=INIT_BODY, headers=headers)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, make: Make) -> Callable[[float], None]:
    "Move the provider's clock forward. `make` must run first so the module is loaded."
    state = {"offset": 0.0}
    from agent_context import oauth

    real = time.time
    monkeypatch.setattr(oauth, "now", lambda: real() + state["offset"])

    def advance(seconds: float) -> None:
        state["offset"] += seconds

    return advance







def test_authorization_server_metadata_names_the_public_issuer(make: Make) -> None:
    body = make().get("/.well-known/oauth-authorization-server").json()
    assert body["issuer"].rstrip("/") == PUBLIC
    assert body["authorization_endpoint"] == f"{PUBLIC}/authorize"
    assert body["token_endpoint"] == f"{PUBLIC}/token"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "registration_endpoint" not in body


def test_protected_resource_metadata_points_at_this_issuer(make: Make) -> None:
    body = make().get("/.well-known/oauth-protected-resource/mcp").json()
    assert [u.rstrip("/") for u in body["authorization_servers"]] == [PUBLIC]


def test_dynamic_client_registration_is_not_served(make: Make) -> None:
    response = make().post("/register", json={"redirect_uris": [REDIRECT], "client_name": "x"})
    assert response.status_code in (404, 405)







def test_unknown_client_id_is_refused_without_a_redirect(make: Make) -> None:
    response = authorize(make(), challenge=pkce()[1], client_id="someone-else")
    assert response.status_code == 400
    assert "location" not in response.headers


def test_unlisted_redirect_uri_is_refused_without_a_redirect(make: Make) -> None:
    response = authorize(make(), challenge=pkce()[1], redirect_uri="https://evil.example/cb")
    assert response.status_code == 400
    assert "location" not in response.headers


def test_missing_code_challenge_never_reaches_the_consent_page(make: Make) -> None:
    params = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "state": "s"}
    response = make().get("/authorize", params=params)
    assert "/oauth/consent" not in response.headers.get("location", "")
    assert "code=" not in response.headers.get("location", "")


def test_plain_pkce_is_refused(make: Make) -> None:
    response = authorize(make(), challenge=pkce()[0], method="plain")
    assert "/oauth/consent" not in response.headers.get("location", "")


def test_a_valid_request_goes_to_the_consent_page(make: Make) -> None:
    response = authorize(make(), challenge=pkce()[1])
    assert response.status_code == 302
    assert request_id_of(response)


def test_the_consent_page_asks_for_a_passphrase_and_never_prints_secrets(make: Make) -> None:
    client = make()
    request_id = request_id_of(authorize(client, challenge=pkce()[1]))
    page = client.get("/oauth/consent", params={"request_id": request_id})
    assert page.status_code == 200
    assert 'name="passphrase"' in page.text
    assert request_id in page.text
    for secret in (PASSPHRASE, CLIENT_SECRET, BEARER):
        assert secret not in page.text


def test_the_right_passphrase_redirects_with_a_code_and_the_state(make: Make) -> None:
    client = make()
    request_id = request_id_of(authorize(client, challenge=pkce()[1], state="keep-me"))
    response = consent(client, request_id)
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
    query = parse_qs(location.query)
    assert query["state"] == ["keep-me"]
    assert len(query["code"][0]) >= 32


def test_a_wrong_passphrase_is_403_and_issues_no_code(make: Make) -> None:
    client = make()
    response = consent(client, request_id_of(authorize(client, challenge=pkce()[1])), "nope")
    assert response.status_code == 403
    assert "code=" not in response.headers.get("location", "")


def test_five_wrong_passphrases_lock_out_even_the_right_one(make: Make) -> None:
    client = make()
    request_id = request_id_of(authorize(client, challenge=pkce()[1]))
    for _ in range(5):
        assert consent(client, request_id, "nope").status_code == 403
    assert consent(client, request_id).status_code == 429


def test_the_lockout_ends_after_ten_minutes(make: Make, clock: Callable[[float], None]) -> None:
    client = make()
    request_id = request_id_of(authorize(client, challenge=pkce()[1]))
    for _ in range(5):
        consent(client, request_id, "nope")
    clock(601)
    request_id = request_id_of(authorize(client, challenge=pkce()[1]))
    assert consent(client, request_id).status_code == 302


def test_an_unknown_request_id_issues_nothing(make: Make) -> None:
    response = consent(make(), "not-a-request")
    assert response.status_code in (400, 404)
    assert "code=" not in response.headers.get("location", "")


def test_a_pending_request_expires_after_ten_minutes(make: Make, clock: Callable[[float], None]) -> None:
    client = make()
    request_id = request_id_of(authorize(client, challenge=pkce()[1]))
    clock(601)
    assert consent(client, request_id).status_code in (400, 404)


def test_a_consent_request_can_be_used_once(make: Make) -> None:
    client = make()
    request_id = request_id_of(authorize(client, challenge=pkce()[1]))
    assert consent(client, request_id).status_code == 302
    assert consent(client, request_id).status_code in (400, 404)


def test_extra_redirect_uris_come_from_the_environment(make: Make) -> None:
    extra = "https://example.test/callback"
    client = make({**ENV, "AGENT_CONTEXT_OAUTH_REDIRECT_URIS": f"{REDIRECT},{extra}"})
    assert authorize(client, challenge=pkce()[1], redirect_uri=extra).status_code == 302







def test_the_code_exchange_returns_access_and_refresh_tokens(make: Make) -> None:
    body = login(make())
    assert body["token_type"].lower() == "bearer"
    assert body["expires_in"] == 3600
    assert body["access_token"] and body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]


@pytest.mark.parametrize("secret", ["wrong-secret", None])
def test_a_wrong_or_missing_client_secret_is_401(make: Make, secret: str | None) -> None:
    client = make()
    verifier, challenge = pkce()
    assert exchange(client, get_code(client, challenge), verifier, secret=secret).status_code == 401


def test_a_code_is_single_use(make: Make) -> None:
    client = make()
    verifier, challenge = pkce()
    code = get_code(client, challenge)
    assert exchange(client, code, verifier).status_code == 200
    second = exchange(client, code, verifier)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


def test_a_code_expires_after_five_minutes(make: Make, clock: Callable[[float], None]) -> None:
    client = make()
    verifier, challenge = pkce()
    code = get_code(client, challenge)
    clock(301)
    response = exchange(client, code, verifier)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_a_wrong_pkce_verifier_is_refused(make: Make) -> None:
    client = make()
    _, challenge = pkce()
    response = exchange(client, get_code(client, challenge), pkce()[0])
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_a_different_redirect_uri_at_the_token_step_is_refused(make: Make) -> None:
    client = make()
    verifier, challenge = pkce()
    code = get_code(client, challenge)
    assert exchange(client, code, verifier, redirect_uri="https://evil.example/cb").status_code == 400


def test_a_refresh_token_rotates_and_works_once(make: Make) -> None:
    client = make()
    first = login(client)
    second = refresh(client, first["refresh_token"])
    assert second.status_code == 200
    tokens = second.json()
    assert tokens["access_token"] != first["access_token"]
    assert tokens["refresh_token"] != first["refresh_token"]
    assert refresh(client, first["refresh_token"]).status_code == 400
    assert mcp_init(client, tokens["access_token"]).status_code == 200


def test_a_refresh_token_expires_after_thirty_days(make: Make, clock: Callable[[float], None]) -> None:
    client = make()
    tokens = login(client)
    clock(30 * 86400 + 1)
    assert refresh(client, tokens["refresh_token"]).status_code == 400


def test_the_basic_auth_method_is_selectable(make: Make) -> None:
    client = make({**ENV, "AGENT_CONTEXT_OAUTH_AUTH_METHOD": "client_secret_basic"})
    verifier, challenge = pkce()
    code = get_code(client, challenge)
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    response = client.post(
        "/token", headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
              "client_id": CLIENT_ID, "redirect_uri": REDIRECT},
    )
    assert response.status_code == 200







def test_the_static_bearer_still_works(make: Make) -> None:
    assert mcp_init(make(), BEARER).status_code == 200


def test_an_oauth_access_token_works(make: Make) -> None:
    client = make()
    assert mcp_init(client, login(client)["access_token"]).status_code == 200


def test_an_expired_access_token_is_401(make: Make, clock: Callable[[float], None]) -> None:
    client = make()
    token = login(client)["access_token"]
    clock(3601)
    assert mcp_init(client, token).status_code == 401


@pytest.mark.parametrize("token", ["not-a-token", None])
def test_unknown_or_missing_tokens_are_401_with_resource_metadata(make: Make, token: str | None) -> None:
    response = mcp_init(make(), token)
    assert response.status_code == 401
    assert "resource_metadata" in response.headers.get("www-authenticate", "")







def test_the_token_file_is_private_and_holds_only_hashes(make: Make, tmp_path: Path) -> None:
    tokens = login(make())
    path = tmp_path / "oauth-tokens.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    json.loads(text)
    assert tokens["access_token"] not in text
    assert tokens["refresh_token"] not in text
    assert hashlib.sha256(tokens["access_token"].encode()).hexdigest() in text


def test_tokens_survive_a_daemon_restart(make: Make) -> None:
    tokens = login(make())
    restarted = make()
    assert mcp_init(restarted, tokens["access_token"]).status_code == 200
    assert refresh(restarted, tokens["refresh_token"]).status_code == 200


def test_a_corrupt_token_file_does_not_stop_the_daemon(make: Make, tmp_path: Path) -> None:
    (tmp_path / "oauth-tokens.json").write_text("{ not json")
    client = make()
    assert mcp_init(client, BEARER).status_code == 200
    assert mcp_init(client, "anything").status_code == 401


def test_secrets_are_never_logged(make: Make, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    client = make()
    tokens = login(client)
    consent(client, request_id_of(authorize(client, challenge=pkce()[1])), "wrong passphrase attempt")
    for secret in (PASSPHRASE, CLIENT_SECRET, BEARER, tokens["access_token"], tokens["refresh_token"],
                   "wrong passphrase attempt"):
        assert secret not in caplog.text







def test_with_only_the_bearer_configured_there_is_no_oauth(make: Make) -> None:
    client = make({"AGENT_CONTEXT_TOKEN": BEARER, "AGENT_CONTEXT_ALLOWED_HOSTS": FQDN})
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    assert mcp_init(client, BEARER).status_code == 200
    assert mcp_init(client, None).status_code == 401


@pytest.mark.parametrize("missing", [k for k in ENV if k not in ("AGENT_CONTEXT_TOKEN", "AGENT_CONTEXT_ALLOWED_HOSTS")])
def test_a_partial_oauth_setup_leaves_oauth_off(make: Make, missing: str) -> None:
    client = make({k: v for k, v in ENV.items() if k != missing})
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    assert mcp_init(client, BEARER).status_code == 200


def test_with_no_token_there_is_no_auth_at_all(make: Make) -> None:
    client = make({"AGENT_CONTEXT_ALLOWED_HOSTS": FQDN})
    assert mcp_init(client, None).status_code == 200
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
