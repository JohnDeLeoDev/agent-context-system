'Findings from the adversarial review of the OAuth connector, each reproduced before the fix.\n\n1. A leftover token temp file with mode 0644 made the saved token file 0644.\n2. Malformed entries in the token file were a 500 on /mcp and /token, not a 401 or a normal login.\n3. A bad redirect URI in the environment made /authorize and /token fail for everyone.\n4. An http:// (or path-carrying) public URL crashed the daemon at import, taking the relays down.\n5. OAuth settings without the bearer token were silently ignored.\n6. /authorize had no cap on parked requests.'
import hashlib
import json
import stat
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_oauth_connector import (  
    BEARER,
    ENV,
    REDIRECT,
    authorize,
    login,
    make,
    mcp_init,
    pkce,
    refresh,
)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _write_state(tmp_path: Path, data: object) -> None:
    (tmp_path / "oauth-tokens.json").write_text(json.dumps(data))





def test_a_stale_wide_open_temp_file_does_not_widen_the_token_file(make, tmp_path: Path) -> None:
    stale = tmp_path / "oauth-tokens.json.tmp"
    stale.write_text("old")
    stale.chmod(0o644)
    login(make())
    assert stat.S_IMODE((tmp_path / "oauth-tokens.json").stat().st_mode) == 0o600





def test_a_malformed_access_entry_is_a_401_not_a_500(make, tmp_path: Path) -> None:
    _write_state(tmp_path, {"access": {_digest("victim"): {}}, "refresh": {}})
    assert mcp_init(make(), "victim").status_code == 401


def test_junk_entries_do_not_break_a_normal_login(make, tmp_path: Path) -> None:
    _write_state(tmp_path, {"access": {"h": {}, "k": 5}, "refresh": {"r": None}})
    client = make()
    assert mcp_init(client, login(client)["access_token"]).status_code == 200


def test_a_malformed_refresh_entry_is_refused_not_a_500(make, tmp_path: Path) -> None:
    _write_state(tmp_path, {"access": {}, "refresh": {_digest("rt"): {"expires_at": 9e12}}})
    assert refresh(make(), "rt").status_code == 400




BAD_SETTINGS = [
    ("AGENT_CONTEXT_OAUTH_REDIRECT_URIS", "notaurl, https://ok.example/cb"),
    ("AGENT_CONTEXT_PUBLIC_URL", "http://example.invalid"),
    ("AGENT_CONTEXT_PUBLIC_URL", "https://example.invalid/prefix"),
    ("AGENT_CONTEXT_PUBLIC_URL", "https://example.invalid/?q=1"),
    ("AGENT_CONTEXT_PUBLIC_URL", "not a url"),
]


@pytest.mark.parametrize(("key", "value"), BAD_SETTINGS)
def test_a_bad_setting_leaves_oauth_off_and_the_daemon_up(
    make, caplog: pytest.LogCaptureFixture, key: str, value: str
) -> None:
    caplog.set_level("WARNING")
    client: TestClient = make({**ENV, key: value})
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    assert mcp_init(client, BEARER).status_code == 200
    assert key in caplog.text





def test_oauth_settings_without_the_bearer_are_reported(make, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING")
    make({k: v for k, v in ENV.items() if k != "AGENT_CONTEXT_TOKEN"})
    assert "AGENT_CONTEXT_TOKEN" in caplog.text





def test_parked_requests_are_capped(make) -> None:
    client = make()
    _, challenge = pkce()
    locations = [authorize(client, challenge=challenge).headers.get("location", "") for _ in range(120)]
    parked = [loc for loc in locations if "/oauth/consent" in loc]
    assert len(parked) == 100
