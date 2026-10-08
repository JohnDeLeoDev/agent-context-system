'policy: a relay moves onto a new release in place.\n\n  - the stdio pipe hands over every byte it read and made no message from, and never cuts a line\n    it is writing;\n  - the release watch sees a release other than its own on any answer, and a dropped event stream\n    on the second GET;\n  - the handover state round-trips through the file the next process reads;\n  - the swap waits for the stdin handover and every forwarded request before it execs.'
import json
import os
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from agent_context import relay_stdio, relay_swap, relay_update
from agent_context.relay_source import RELEASE_HEADER


def _line(obj: dict) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", **obj}) + "\n").encode()


def _msg(obj: dict) -> SessionMessage:
    return SessionMessage(JSONRPCMessage.model_validate({"jsonrpc": "2.0", **obj}))





def test_the_pipe_parses_whole_lines_and_hands_over_the_rest() -> None:
    r, w = os.pipe()
    out_r, out_w = os.pipe()
    pipe = relay_stdio.StdioPipe(fd_in=r, fd_out=out_w)
    got: list[object] = []

    async def main() -> None:
        send, recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        async with anyio.create_task_group() as tg:
            tg.start_soon(pipe.read_into, send)
            os.write(w, _line({"id": 1, "method": "ping"}) + b'{"jsonrpc": "2.0", "id": 2, "me')
            got.append(await recv.receive())
            await anyio.sleep(0.3)
            leftover = pipe.freeze()
            got.append(leftover)
            async for item in recv:  
                got.append(item)
            tg.cancel_scope.cancel()

    anyio.run(main)
    assert isinstance(got[0], SessionMessage)
    assert got[1] == b'{"jsonrpc": "2.0", "id": 2, "me'
    assert got[2:] == []
    for fd in (r, w, out_r, out_w):
        os.close(fd)


def test_a_preloaded_line_is_parsed_before_anything_is_read() -> None:
    r, w = os.pipe()
    pipe = relay_stdio.StdioPipe(preload=_line({"id": 7, "method": "ping"}), fd_in=r)

    async def main() -> object:
        send, recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        got: list[object] = []
        async with anyio.create_task_group() as tg:
            tg.start_soon(pipe.read_into, send)
            got.append(await recv.receive())
            tg.cancel_scope.cancel()
        return got[0]

    item = anyio.run(main)
    assert isinstance(item, SessionMessage)
    assert item.message.model_dump(exclude_none=True)["id"] == 7
    os.close(r)
    os.close(w)


def test_a_last_line_without_a_newline_is_delivered_at_eof() -> None:
    r, w = os.pipe()
    pipe = relay_stdio.StdioPipe(fd_in=r)
    os.write(w, b'{"jsonrpc": "2.0", "id": 3, "method": "ping"}')
    os.close(w)

    async def main() -> list[object]:
        send, recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        items: list[object] = []
        async with anyio.create_task_group() as tg:
            tg.start_soon(pipe.read_into, send)
            items.extend([item async for item in recv])
            tg.cancel_scope.cancel()
        return items

    items = anyio.run(main)
    assert len(items) == 1 and isinstance(items[0], SessionMessage)
    os.close(r)


def test_nothing_is_written_after_the_writer_closes() -> None:
    r, w = os.pipe()
    pipe = relay_stdio.StdioPipe(fd_out=w)
    pipe.write_line(b"one\n")
    pipe.close_writer()
    pipe.write_line(b"two\n")
    os.close(w)
    assert os.read(r, 100) == b"one\n"
    os.close(r)





def _response(etag: str | None) -> httpx.Response:
    headers = {RELEASE_HEADER: etag} if etag else {}
    return httpx.Response(200, headers=headers, request=httpx.Request("POST", "http://x/mcp"))


def test_a_release_other_than_its_own_is_seen_once() -> None:
    async def main() -> tuple[bool, str | None, bool]:
        watch = relay_swap.ReleaseWatch('"old"')
        await watch.on_response(_response('"old"'))
        quiet = watch.release_seen.is_set()
        await watch.on_response(_response('"new"'))
        return quiet, watch.wanted, watch.release_seen.is_set()

    assert anyio.run(main) == (False, '"new"', True)


def test_a_release_that_failed_is_not_wanted_again() -> None:
    async def main() -> bool:
        watch = relay_swap.ReleaseWatch('"old"')
        watch.failed.add('"bad"')
        await watch.on_response(_response('"bad"'))
        return watch.release_seen.is_set()

    assert anyio.run(main) is False


def test_the_second_get_of_a_connection_means_the_stream_dropped() -> None:
    async def main() -> tuple[bool, bool, bool]:
        watch = relay_swap.ReleaseWatch("")
        get = httpx.Request("GET", "http://x/mcp")
        await watch.on_request(httpx.Request("POST", "http://x/mcp"))
        await watch.on_request(get)
        first = watch.stream_lost.is_set()
        await watch.on_request(get)
        second = watch.stream_lost.is_set()
        watch.reset_stream()
        return first, second, watch.stream_lost.is_set()

    assert anyio.run(main) == (False, True, False)


def test_the_client_factory_carries_the_hooks() -> None:
    watch = relay_swap.ReleaseWatch("")
    client = watch.client_factory()(headers={"Authorization": "Bearer t"})
    assert watch.on_request in client.event_hooks["request"]
    assert watch.on_response in client.event_hooks["response"]
    anyio.run(client.aclose)





def test_the_handover_state_round_trips_and_is_read_once(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    init = _msg({"id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25",
                                                              "capabilities": {},
                                                              "clientInfo": {"name": "c",
                                                                             "version": "1"}}})
    note = _msg({"method": "notifications/initialized"})
    state = relay_swap.resume_state({"init_msg": init, "init_note": note}, b'{"partial\xff')
    path = relay_update.write_resume_file(state)
    env = {relay_swap.RESUME_ENV: path}
    loaded = relay_swap.load_resume(env)
    assert loaded is not None
    assert loaded["init_msg"].message.model_dump(exclude_none=True)["method"] == "initialize"
    assert loaded["init_note"].message.model_dump(exclude_none=True)["method"] == \
        "notifications/initialized"
    assert loaded["leftover"] == b'{"partial\xff'
    assert not Path(path).exists() and relay_swap.RESUME_ENV not in env
    assert relay_swap.load_resume(env) is None


def test_the_swap_waits_for_the_handover_and_the_answers_before_it_execs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    r, w = os.pipe()
    pipe = relay_stdio.StdioPipe(preload=b'{"part', fd_in=r)
    monkeypatch.setattr(relay_stdio, "ACTIVE", pipe)
    target = tmp_path / "release"
    monkeypatch.setattr(relay_swap, "install", lambda etag: target)
    execs: list[tuple[Path, dict]] = []
    monkeypatch.setattr(relay_swap, "exec_release", lambda t, s: execs.append((t, s)))
    st = {"init_msg": _msg({"id": 0, "method": "initialize", "params": {}}),
          "init_note": _msg({"method": "notifications/initialized"}), "init_answered": True,
          "pending": {5: object()}, "stdin_handed_over": False, "client_eof": False}

    async def main() -> list[str]:
        seen: list[str] = []
        watch = relay_swap.ReleaseWatch('"old"')
        async with anyio.create_task_group() as tg:
            tg.start_soon(relay_swap.swap_when_released, watch, st)
            await watch.on_response(_response('"new"'))
            await anyio.sleep(0.3)
            seen.append(f"frozen={pipe.frozen} execs={len(execs)}")
            st["stdin_handed_over"] = True
            await anyio.sleep(0.2)
            seen.append(f"execs={len(execs)}")
            st["pending"].clear()
            with anyio.fail_after(5):
                while not execs:
                    await anyio.sleep(0.02)
            seen.append(f"execs={len(execs)}")
        return seen

    assert anyio.run(main) == ["frozen=True execs=0", "execs=0", "execs=1"]
    assert execs[0][0] == target
    assert execs[0][1]["leftover"] == '{"part'
    os.close(r)
    os.close(w)


def test_a_failed_install_keeps_the_relay_on_its_release(monkeypatch: pytest.MonkeyPatch) -> None:
    r, w = os.pipe()
    pipe = relay_stdio.StdioPipe(fd_in=r)
    monkeypatch.setattr(relay_stdio, "ACTIVE", pipe)
    monkeypatch.setattr(relay_swap, "install", lambda etag: None)
    st = {"init_msg": None, "init_note": None, "init_answered": False, "pending": {},
          "stdin_handed_over": False, "client_eof": False}

    async def main() -> set[str]:
        watch = relay_swap.ReleaseWatch('"old"')
        async with anyio.create_task_group() as tg:
            tg.start_soon(relay_swap.swap_when_released, watch, st)
            await watch.on_response(_response('"new"'))
            await anyio.sleep(0.3)
            tg.cancel_scope.cancel()
        return watch.failed

    assert anyio.run(main) == {'"new"'}
    assert not pipe.frozen
    os.close(r)
    os.close(w)





def test_the_release_directory_record_wins_over_the_installed_etag(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = tmp_path / "relay" / "abc"
    prefix.mkdir(parents=True)
    (prefix / relay_update.RELEASE_FILE).write_text('"from-release"\n')
    home = tmp_path / "home"
    relay_update.etag_path(home).parent.mkdir(parents=True)
    relay_update.etag_path(home).write_text('"from-installer"\n')
    monkeypatch.setattr(relay_update.sys, "prefix", str(prefix))
    monkeypatch.setattr(relay_update, "_START_ETAGS", {})
    assert relay_update.start_etag(home) == '"from-release"'


def test_without_a_release_record_the_installed_etag_is_used_and_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    relay_update.etag_path(home).parent.mkdir(parents=True)
    relay_update.etag_path(home).write_text('"first"\n')
    monkeypatch.setattr(relay_update.sys, "prefix", str(tmp_path / "no-release"))
    monkeypatch.setattr(relay_update, "_START_ETAGS", {})
    assert relay_update.start_etag(home) == '"first"'
    relay_update.etag_path(home).write_text('"second"\n')
    assert relay_update.start_etag(home) == '"first"'





def test_send_returns_only_once_the_line_is_written() -> None:
    'A request is marked answered after `send`; a swap then closes the writer. The answer must\n    already be on the pipe by then, or the client waits on that id for good.'
    r, w = os.pipe()
    pipe = relay_stdio.StdioPipe(fd_out=w)
    writer = relay_stdio.LineWriter(pipe)

    async def main() -> None:
        await writer.send(_msg({"id": 9, "result": {}}))
        pipe.close_writer()

    anyio.run(main)
    os.close(w)
    assert json.loads(os.read(r, 1000))["id"] == 9
    os.close(r)


def test_a_resume_after_a_failed_exec_marks_that_release_failed() -> None:
    assert relay_swap.failed_release({"release": '"new"'}, '"old"') == '"new"'
    assert relay_swap.failed_release({"release": '"new"'}, '"new"') is None
    assert relay_swap.failed_release({"release": ""}, '"old"') is None
    assert relay_swap.failed_release(None, '"old"') is None


def test_the_release_rides_the_handover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state = {**relay_swap.resume_state({"init_msg": _msg({"id": 0, "method": "initialize",
                                                          "params": {}}),
                                        "init_note": None}, b""), "release": '"r"'}
    env = {relay_swap.RESUME_ENV: str(relay_update.write_resume_file(state))}
    loaded = relay_swap.load_resume(env)
    assert loaded is not None and loaded["release"] == '"r"'
