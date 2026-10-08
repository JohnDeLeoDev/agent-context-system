"OAuth 2.1 authorization server for custom connectors (the Claude mobile app asks for a client id\nand secret, not a bearer token).\n\nThe daemon issues its own tokens for ONE static client. Flow: /authorize (library handler: client,\nredirect_uri and PKCE checks) -> provider.authorize() parks the request and redirects to our\n/oauth/consent page -> the passphrase is checked there -> a single-use code goes to the client's\nredirect_uri -> /token (library handler: client secret, PKCE verifier, redirect_uri match) ->\nprovider.exchange_authorization_code() -> access token (1 h) and rotating refresh token (30 d).\n\nThe static bearer token (AGENT_CONTEXT_TOKEN) is still accepted by load_access_token, because\nFastMCP takes either a token verifier or an authorization-server provider, never both, and the\nrelays authenticate with the bearer.\n\nIssued tokens live in a 0600 JSON file as sha256 hashes, so a daemon restart does not log the app\nout and the file alone cannot be replayed. Codes and parked requests are short-lived and in memory.\nNothing secret is logged."
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from . import token_table
from .paths import write_atomic

log = logging.getLogger("agent-context")


now = time.time

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
PENDING_TTL = 600
MAX_PENDING = 100
LOCKOUT_WINDOW = 600
LOCKOUT_AFTER = 5
DEFAULT_REDIRECT_URIS = ("https://claude.ai/api/mcp/auth_callback",)
_REQUIRED = (
    "AGENT_CONTEXT_PUBLIC_URL",
    "AGENT_CONTEXT_OAUTH_CLIENT_ID",
    "AGENT_CONTEXT_OAUTH_CLIENT_SECRET",
    "AGENT_CONTEXT_OAUTH_PASSPHRASE",
)
AuthMethod = Literal["client_secret_post", "client_secret_basic"]


@dataclass(frozen=True)
class OAuthConfig:
    public_url: str
    client_id: str
    client_secret: str
    passphrase: str
    redirect_uris: tuple[str, ...]
    auth_method: AuthMethod
    state_path: Path


def _setting_problem(env: Mapping[str, str]) -> str | None:
    'Why a set-but-unusable setting keeps OAuth off (the library refuses a non-https issuer at\n    import, which would take the whole daemon down), or None. Never quotes a secret.'
    parsed = urlparse(env["AGENT_CONTEXT_PUBLIC_URL"])
    if (parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or parsed.params):
        return "AGENT_CONTEXT_PUBLIC_URL must be an https URL with no path, query or fragment"
    for uri in (u.strip() for u in env.get("AGENT_CONTEXT_OAUTH_REDIRECT_URIS", "").split(",")):
        if not uri:
            continue
        try:
            AnyUrl(uri)
        except ValueError:
            return "AGENT_CONTEXT_OAUTH_REDIRECT_URIS has an entry that is not a URL"
    return None


def config_from_env(env: Mapping[str, str]) -> OAuthConfig | None:
    'The OAuth settings, or None when OAuth is off. On means the bearer token AND all four\n    OAuth settings are set; a partial setup stays off and the log names what is missing.'
    if not any(env.get(key) for key in _REQUIRED):
        return None
    missing = [key for key in ("AGENT_CONTEXT_TOKEN", *_REQUIRED) if not env.get(key)]
    if missing:
        log.warning("oauth: connector login is OFF; not set: %s", ", ".join(missing))
        return None
    problem = _setting_problem(env)
    if problem:
        log.warning("oauth: connector login is OFF; %s", problem)
        return None
    uris = [u.strip() for u in env.get("AGENT_CONTEXT_OAUTH_REDIRECT_URIS", "").split(",") if u.strip()]
    method: AuthMethod = (
        "client_secret_basic"
        if env.get("AGENT_CONTEXT_OAUTH_AUTH_METHOD") == "client_secret_basic"
        else "client_secret_post"
    )
    state = env.get("AGENT_CONTEXT_OAUTH_STATE")
    if state:
        state_path = Path(state)
    else:
        from .paths import state_dir

        state_path = state_dir() / "oauth-tokens.json"
    return OAuthConfig(
        public_url=env["AGENT_CONTEXT_PUBLIC_URL"].rstrip("/"),
        client_id=env["AGENT_CONTEXT_OAUTH_CLIENT_ID"],
        client_secret=env["AGENT_CONTEXT_OAUTH_CLIENT_SECRET"],
        passphrase=env["AGENT_CONTEXT_OAUTH_PASSPHRASE"],
        redirect_uris=tuple(uris) or DEFAULT_REDIRECT_URIS,
        auth_method=method,
        state_path=state_path,
    )


def _entry_ok(entry: object) -> bool:
    'A stored token entry with every field the code reads. A hand-edited or damaged file must\n    give a 401, not a 500, so entries that fail this are dropped at load.'
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("client_id"), str)
        and isinstance(entry.get("expires_at"), int | float)
        and isinstance(entry.get("scopes"), list)
    )


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


@dataclass(frozen=True)
class ConsentResult:
    'kind: redirect (302 to `url`), denied (403), locked (429), invalid (400).'

    kind: Literal["redirect", "denied", "locked", "invalid"]
    url: str | None = None


class ConnectorProvider:
    def __init__(self, config: OAuthConfig) -> None:
        self.config = config
        self._codes: dict[str, AuthorizationCode] = {}
        self._pending: dict[str, tuple[AuthorizationParams, float]] = {}
        self._failures: list[float] = []
        
        self._access: dict[str, dict] = {}
        
        self._refresh: dict[str, dict] = {}
        self._load()

    

    def _load(self) -> None:
        try:
            data = json.loads(self.config.state_path.read_text())
            self._access = {k: v for k, v in dict(data.get("access", {})).items() if _entry_ok(v)}
            self._refresh = {k: v for k, v in dict(data.get("refresh", {})).items() if _entry_ok(v)}
        except FileNotFoundError:
            return
        except (OSError, ValueError, AttributeError, TypeError):
            log.warning("oauth: token file %s is unreadable; starting with no tokens",
                        self.config.state_path)
            self._access, self._refresh = {}, {}

    def _save(self) -> None:
        path = self.config.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps({"access": self._access, "refresh": self._refresh}), 0o600)

    def _purge(self) -> None:
        moment = now()
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > moment}
        self._pending = {k: v for k, v in self._pending.items() if v[1] > moment}
        self._failures = [t for t in self._failures if t > moment - LOCKOUT_WINDOW]

    

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not _same(client_id, self.config.client_id):
            return None
        return OAuthClientInformationFull(
            client_id=self.config.client_id,
            client_secret=self.config.client_secret,
            redirect_uris=[AnyUrl(u) for u in self.config.redirect_uris],
            token_endpoint_auth_method=self.config.auth_method,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name="agent-context connector",
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("dynamic client registration is disabled")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._purge()
        if len(self._pending) >= MAX_PENDING:
            raise AuthorizeError("temporarily_unavailable", "too many pending requests")
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = (params, now() + PENDING_TTL)
        return f"{self.config.public_url}/oauth/consent?request_id={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        self._purge()
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code was already used")
        return self._issue(authorization_code.client_id, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        entry = self._refresh.get(_digest(refresh_token))
        if entry is None or entry["client_id"] != client.client_id or entry["expires_at"] <= now():
            return None
        return RefreshToken(token=refresh_token, client_id=entry["client_id"],
                            scopes=entry["scopes"], expires_at=int(entry["expires_at"]))

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        entry = self._refresh.pop(_digest(refresh_token.token), None)
        if entry is None:
            raise TokenError("invalid_grant", "refresh token was already used")
        self._access.pop(entry.get("access", ""), None)
        return self._issue(refresh_token.client_id, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        
        info = token_table.verify(token)
        if info is not None:
            return AccessToken(token=token, client_id=info.id, scopes=list(info.scopes),
                               expires_at=None)
        entry = self._access.get(_digest(token))
        if entry is None or entry["expires_at"] <= now():
            return None
        return AccessToken(token=token, client_id=entry["client_id"], scopes=entry["scopes"],
                           expires_at=int(entry["expires_at"]))

    def client_for(self, token: str) -> str | None:
        'The client id of a valid access token this provider issued, else None.'
        entry = self._access.get(_digest(token))
        if entry is None or entry["expires_at"] <= now():
            return None
        return entry["client_id"]

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        digest = _digest(token.token)
        for own, other in ((self._access, self._refresh), (self._refresh, self._access)):
            entry = own.pop(digest, None)
            if entry:
                other.pop(entry.get("refresh") or entry.get("access") or "", None)
        self._save()

    

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        moment = now()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        access_hash, refresh_hash = _digest(access), _digest(refresh)
        self._access = {k: v for k, v in self._access.items() if v["expires_at"] > moment}
        self._refresh = {k: v for k, v in self._refresh.items() if v["expires_at"] > moment}
        self._access[access_hash] = {"client_id": client_id, "scopes": scopes,
                                     "expires_at": moment + ACCESS_TTL, "refresh": refresh_hash}
        self._refresh[refresh_hash] = {"client_id": client_id, "scopes": scopes,
                                       "expires_at": moment + REFRESH_TTL, "access": access_hash}
        self._save()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes) or None)

    

    def has_pending(self, request_id: str) -> bool:
        self._purge()
        return request_id in self._pending

    def consent(self, request_id: str, passphrase: str) -> ConsentResult:
        self._purge()
        if len(self._failures) >= LOCKOUT_AFTER:
            return ConsentResult("locked")
        pending = self._pending.get(request_id)
        if pending is None:
            return ConsentResult("invalid")
        if not _same(passphrase, self.config.passphrase):
            self._failures.append(now())
            log.warning("oauth: consent refused (%d recent failures)", len(self._failures))
            return ConsentResult("denied")
        params, _ = self._pending.pop(request_id)
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=params.scopes or [],
            expires_at=now() + CODE_TTL,
            client_id=self.config.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self._codes[code.code] = code
        log.info("oauth: consent granted")
        return ConsentResult("redirect", construct_redirect_uri(
            str(params.redirect_uri), code=code.code, state=params.state))


def consent_page(request_id: str, *, message: str = "") -> str:
    note = f"<p>{html.escape(message)}</p>" if message else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>agent-context</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>"
        "<h1>Connect to agent-context</h1>"
        "<p>Enter the passphrase to let this app use the store.</p>"
        f"{note}"
        "<form method=\"post\" action=\"/oauth/consent\">"
        f"<input type=\"hidden\" name=\"request_id\" value=\"{html.escape(request_id, quote=True)}\">"
        "<input type=\"password\" name=\"passphrase\" autocomplete=\"current-password\" autofocus>"
        "<button type=\"submit\">Allow</button></form></body></html>"
    )


def provider_from_env(env: Mapping[str, str]) -> ConnectorProvider | None:
    config = config_from_env(env)
    return ConnectorProvider(config) if config else None
