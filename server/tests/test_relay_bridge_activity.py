"The stdio bridge feeds the idle tracker and can be told to end gracefully (X2).\n\nNames the implementation must provide:\n  relay_update.ACTIVITY          the ActivityTracker the bridge updates, read at call time\n  daemon.request_relay_exit()    thread-safe; makes `daemon._bridge` return normally (status 0)\n                                 through the task group, never through `os._exit`\nThe bridge counts a request (a message with both `method` and `id`) in when it forwards it to the\ndaemon and out when the daemon's response for that id reaches the client."
import contextlib
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCResponse

from agent_context import daemon
from agent_context import relay_update as U
from relay_self_refresh_support import *  
from relay_self_refresh_support import need


class _Conn:
    def __init__(self) -> None:
        self.to_bridge, self.d_read = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        self.d_write, self.from_bridge = anyio.create_memory_object_stream[SessionMessage](16)


async def _until(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    with anyio.fail_after(timeout):
        while not pred():
            await anyio.sleep(0.01)
