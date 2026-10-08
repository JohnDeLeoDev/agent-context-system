"The relay's stdio transport: mcp's `stdio_server`, with the reader and the writer under this\nmodule's control, so the relay can `execv` onto a new release on the same file descriptors\n(policy) without losing a byte the client sent or cutting a line it is reading.\n\nmcp's own transport reads stdin through a buffered text wrapper on a worker thread. Bytes that\nwrapper has read and not yet parsed live only in this process, and `execv` would drop them. Here\nevery byte read from fd 0 sits in `StdioPipe.pending` until a complete line is taken out of it,\nand both steps happen under one lock, so `freeze()` knows exactly which bytes no message was made\nfrom yet. It hands them to the next process, which parses them first.\n\nThe writer takes the same care in the other direction. `send` returns only once its line is on\nfd 1, so a caller that marks a request answered after `send` knows the client has the answer;\n`close_writer()` waits for a line being written to finish and drops anything sent after it, so the\nclient never reads half a line.\n\n`stdio_server()` yields the same (read, write) pair mcp's does. The pipe behind it is `ACTIVE`\nwhile it runs; a caller that swapped in another transport (the tests do) leaves `ACTIVE` None,\nand nothing that needs the pipe runs."
import contextlib
import os
import select
import threading
from collections.abc import AsyncIterator

import anyio
import anyio.to_thread
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.shared.message import SessionMessage

_READ_SIZE = 65536
_FREEZE_POLL_SECONDS = 0.2  



_BLOCKING_READ = os.name == "nt"


class StdioPipe:
    'fd 0 and fd 1 of this process, as JSON-RPC lines.'

    def __init__(self, preload: bytes = b"", fd_in: int = 0, fd_out: int = 1) -> None:
        self.fd_in = fd_in
        self.fd_out = fd_out
        self.pending = bytearray(preload)  
        self._lock = threading.Lock()
        self._frozen = False
        self._write_lock = threading.Lock()
        self._writer_closed = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def _take_lines(self) -> list[bytes]:
        with self._lock:
            if self._frozen:
                return []
            end = self.pending.rfind(b"\n")
            if end < 0:
                return []
            taken = bytes(self.pending[:end + 1])
            del self.pending[:end + 1]
        return [line for line in taken.split(b"\n") if line.strip()]

    def _read_chunk(self) -> bool:
        'Block until fd_in has bytes, append them to `pending`. False at EOF or once frozen.'
        while True:
            if _BLOCKING_READ:
                data = os.read(self.fd_in, _READ_SIZE)
                ready = True
            else:
                ready, _, _ = select.select([self.fd_in], [], [], _FREEZE_POLL_SECONDS)
            with self._lock:
                if self._frozen:
                    return False
                if not ready:
                    continue
                if not _BLOCKING_READ:
                    data = os.read(self.fd_in, _READ_SIZE)
                if not data:
                    self.pending += b"\n"  
                    return False
                self.pending += data
                return True

    def freeze(self) -> bytes:
        "Stop reading fd_in and return the bytes no message was made from. A message already\n        made is delivered to the reader's stream before it closes."
        with self._lock:
            self._frozen = True
            return bytes(self.pending)

    def write_line(self, data: bytes) -> None:
        with self._write_lock:
            if self._writer_closed:
                return
            view = memoryview(data)
            while view:
                view = view[os.write(self.fd_out, view):]

    def close_writer(self) -> None:
        'Wait for a line being written, then drop every later one.'
        with self._write_lock:
            self._writer_closed = True

    @staticmethod
    async def _deliver(sink: MemoryObjectSendStream[SessionMessage | Exception], line: bytes) -> None:
        try:
            message = types.JSONRPCMessage.model_validate_json(line.decode("utf-8", errors="replace"))
        except Exception as exc:
            await sink.send(exc)
            return
        await sink.send(SessionMessage(message))

    async def read_into(self, sink: MemoryObjectSendStream[SessionMessage | Exception]) -> None:
        async with sink:
            while True:
                for line in self._take_lines():
                    await self._deliver(sink, line)
                if not await anyio.to_thread.run_sync(self._read_chunk, abandon_on_cancel=True):
                    for line in self._take_lines():  
                        await self._deliver(sink, line)
                    return



class LineWriter:
    "The write half `stdio_server` yields: `send` writes the message's line to the pipe and\n    returns when it is written (a memory stream would return when a reader took it)."

    def __init__(self, pipe: StdioPipe) -> None:
        self.pipe = pipe

    async def send(self, item: SessionMessage) -> None:
        line = item.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
        await anyio.to_thread.run_sync(self.pipe.write_line, line.encode("utf-8"))


ACTIVE: StdioPipe | None = None
PRELOAD: bytes = b""  


@contextlib.asynccontextmanager
async def stdio_server() -> AsyncIterator[tuple[MemoryObjectReceiveStream[SessionMessage | Exception],
                                                LineWriter]]:
    "This process's stdio for the bridge, with `ACTIVE` set to the pipe."
    global ACTIVE, PRELOAD
    pipe = StdioPipe(PRELOAD)
    PRELOAD = b""
    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    ACTIVE = pipe
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(pipe.read_into, read_writer)
            yield read_stream, LineWriter(pipe)
            tg.cancel_scope.cancel()
    finally:
        ACTIVE = None
