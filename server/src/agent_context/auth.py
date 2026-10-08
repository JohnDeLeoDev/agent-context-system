'Static Bearer-token auth for the agent-context daemon.\n\nReads AGENT_CONTEXT_TOKEN from the environment at verify time (not import\ntime), so rotating the token does not require a daemon restart.'

import hmac
import os

from mcp.server.auth.provider import AccessToken


class StaticTokenVerifier:
    'Verifies a single static Bearer token (AGENT_CONTEXT_TOKEN).'

    async def verify_token(self, token: str) -> AccessToken | None:
        expected = os.environ.get("AGENT_CONTEXT_TOKEN", "")
        if not expected:
            return None
        if not hmac.compare_digest(token, expected):
            return None
        return AccessToken(
            token=token,
            client_id="static",
            scopes=[],
            expires_at=None,
        )
