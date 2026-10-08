'policy: the per-relay self-check. `agent-context-relay-install` runs it with the python of a\nrelease it just built, before any relay switches to that release:\n\n    <release>/bin/python -m agent_context.relay_selfcheck\n\nIt loads every module a relay runs and opens a real session with the daemon the relay will talk\nto: handshake, tool list, and the bundle a relay materializes. Exit 0 when all of it worked, 1\nwith one line on stderr naming what did not. The ls gate already ran the suite on this code; this\nchecks the build on this machine: its python, its dependencies, its network path and token.'
import asyncio
import importlib
import json
import os
import sys

RELAY_MODULES = ("agent_context.server", "agent_context.daemon", "agent_context.relay_materialize",
                 "agent_context.relay_swap", "agent_context.relay_stdio",
                 "agent_context.relay_env", "agent_context.identity", "agent_context.peer_wake")
TIMEOUT_SECONDS = 30.0


async def _session_check(url: str, headers: dict[str, str]) -> str | None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with (create_mcp_http_client(headers=headers or None) as http,
                streamable_http_client(url, http_client=http) as (read, write, _),
                ClientSession(read, write) as session):
        await session.initialize()
        tools = await session.list_tools()
        if not tools.tools:
            return "the daemon listed no tools"
        res = await session.call_tool("get_materialized", {})
        text = getattr(res.content[0], "text", "") if res.content else ""
        if res.isError or not isinstance(json.loads(text or "null"), dict):
            return "get_materialized did not return a bundle"
    return None


def check() -> str | None:
    'None when the release works here, else what failed.'
    for name in RELAY_MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:
            return f"import {name} failed: {type(e).__name__}: {e}"
    from .daemon import connect_headers, is_remote, mcp_url
    from .relay_env import load_relay_env
    load_relay_env(os.environ)
    if not is_remote():
        return "AGENT_CONTEXT_HOST names no remote daemon"
    try:
        return asyncio.run(asyncio.wait_for(_session_check(mcp_url(), connect_headers()),
                                            TIMEOUT_SECONDS))
    except Exception as e:
        return f"session with {mcp_url()} failed: {type(e).__name__}"


def main() -> int:
    problem = check()
    if problem:
        print(f"relay self-check: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
