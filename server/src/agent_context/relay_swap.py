"policy: a relay moves onto a new release in place, the moment ls serves one.\n\nEvery daemon response names the release it serves (`X-Agent-Context-Release`, frozen when the\ndaemon boots, policy). A daemon that exec'd onto new code drops every session, so each relay\nreconnects at once (the GET stream watch below) and reads the new release off that reconnect. A\nrelay that starts on an old release reads it off its first answer. Nothing polls.\n\nOn a release other than its own, the relay:\n  1. runs `agent-context-relay-install --release ETAG`, which builds the release into its own\n     directory under ~/.local/share/agent-context/relay/, runs the release's self-check against\n     the daemon there, and only then points `current` and ~/.local/bin/agent-context at it. One\n     install per host: the installer holds a lock and the next relay finds the directory built;\n  2. stops reading stdin, lets every request it already forwarded finish, and stops writing;\n  3. `execv`s the new release's `agent-context` on the same stdin and stdout, handing over the\n     client's `initialize`, its `notifications/initialized` and the stdin bytes it had read but\n     not parsed. The new process replays the handshake to the daemon the way a reconnect does\n     and swallows the answer, so the client never sees the switch.\n\nA release whose install or self-check failed is not tried again by this process. Nothing here\nmay end a session: every failure is logged and the relay keeps running the code it has.\n\nWindows has no exec: `os.execve` there starts a new process and ends this one, so the harness would\nsee its server exit. A Windows relay does step 1 only, so the next session starts on the new\nrelease, and keeps serving this one on the code it runs (SWAPS_IN_PLACE)."
import logging
import os
import subprocess
import sys
from pathlib import Path

import anyio
import anyio.to_thread
import httpx
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from . import relay_stdio, relay_update
from .relay_source import RELEASE_HEADER

log = logging.getLogger("agent-context")

RESUME_ENV = "AGENT_CONTEXT_RELAY_RESUME"
INSTALL_TIMEOUT_SECONDS = 600
_DRAIN_POLL_SECONDS = 0.05

WINDOWS = os.name == "nt"
SWAPS_IN_PLACE = not WINDOWS


def installer_path(home: Path) -> Path:
    return home / ".local" / "bin" / "agent-context-relay-install"


def installer_command(installer: Path, etag: str) -> list[str]:
    "The installer is a Python script with a shebang; Windows runs nothing by shebang, so there\n    it runs under this relay's own interpreter (it needs only the standard library)."
    head = [sys.executable] if WINDOWS else []
    return [*head, str(installer), "--release", etag]


def release_program(target: Path) -> Path:
    "The relay command inside a release directory: uv puts a venv's scripts in bin/ on POSIX\n    and in Scripts\\ as .exe files on Windows."
    if WINDOWS:
        return target / "Scripts" / "agent-context.exe"
    return target / "bin" / "agent-context"


def own_release() -> str:
    'The ETag of the release this process runs ("" when unknown).'
    return relay_update.start_etag()


class ReleaseWatch:
    "What the daemon's answers say, gathered by httpx event hooks on the bridge's client:\n    the release ls serves, and that the event stream dropped (a second GET on one session)."

    def __init__(self, own: str) -> None:
        self.own = own
        self.wanted: str | None = None
        self.failed: set[str] = set()
        self.release_seen = anyio.Event()
        self.stream_lost = anyio.Event()
        self._gets = 0

    def reset_stream(self) -> None:
        'A new connection: its first GET opens the event stream.'
        self._gets = 0
        self.stream_lost = anyio.Event()

    async def on_request(self, request: httpx.Request) -> None:
        if request.method == "GET":
            self._gets += 1
            if self._gets > 1:  
                self.stream_lost.set()

    async def on_response(self, response: httpx.Response) -> None:
        etag = response.headers.get(RELEASE_HEADER)
        if etag and etag != self.own and etag not in self.failed and etag != self.wanted:
            self.wanted = etag
            self.release_seen.set()

    def client_factory(self):
        "An httpx client factory for `streamablehttp_client` with this watch's hooks."
        from mcp.shared._httpx_utils import create_mcp_http_client

        def factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
            client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
            client.event_hooks["request"].append(self.on_request)
            client.event_hooks["response"].append(self.on_response)
            return client
        return factory


def install(etag: str, home: Path | None = None) -> Path | None:
    'Run the installer for `etag`. The release directory it built and checked, or None.'
    root = home if home is not None else Path.home()
    installer = installer_path(root)
    if not (installer.is_file() and os.access(installer, os.X_OK)):
        log.warning("agent-context: relay installer missing at %s; staying on this release",
                    installer)
        return None
    env = {k: v for k, v in os.environ.items() if k != RESUME_ENV}
    try:
        done = subprocess.run(installer_command(installer, etag), env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=INSTALL_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("agent-context: relay install of %s did not run: %s", etag, type(e).__name__)
        return None
    if done.returncode != 0:
        log.warning("agent-context: relay install of %s failed (exit %d): %s", etag,
                    done.returncode, (done.stderr or "").strip()[-400:])
        return None
    lines = (done.stdout or "").strip().splitlines()
    target = Path(lines[-1]) if lines else None
    if target is None or not release_program(target).is_file():
        log.warning("agent-context: relay installer named no release directory")
        return None
    return target


def resume_state(st: dict, leftover: bytes) -> dict:
    "What the next process needs to carry the client's session on."
    def dump(item: SessionMessage | None) -> str | None:
        return None if item is None else item.message.model_dump_json(by_alias=True,
                                                                      exclude_none=True)
    return {"init_msg": dump(st["init_msg"]), "init_note": dump(st["init_note"]),
            "leftover": leftover.decode("latin-1")}


def load_resume(env: dict | None = None) -> dict | None:
    'The state a previous process of this relay handed over, read once; None when this process\n    was not started by a swap or the state cannot be used.'
    data = relay_update.take_resume_file(RESUME_ENV, env)
    if data is None:
        return None

    def load(text: object) -> SessionMessage | None:
        if not isinstance(text, str):
            return None
        return SessionMessage(JSONRPCMessage.model_validate_json(text))
    try:
        init_msg, init_note = load(data.get("init_msg")), load(data.get("init_note"))
    except ValueError as e:
        log.warning("agent-context: relay swap state unusable: %s", type(e).__name__)
        return None
    leftover = data.get("leftover")
    release = data.get("release")
    return {"init_msg": init_msg, "init_note": init_note,
            "leftover": leftover.encode("latin-1") if isinstance(leftover, str) else b"",
            "release": release if isinstance(release, str) else ""}


def failed_release(resume: dict | None, own: str) -> str | None:
    'The release a swap meant to reach when this process resumed on another one: the exec into\n    it failed, so this process must not try it again.'
    wanted = (resume or {}).get("release") or ""
    return wanted if wanted and wanted != own else None


def _flush_everything() -> None:
    import contextlib
    with contextlib.suppress(Exception):
        from . import usage
        usage.flush()
    for handler in logging.getLogger().handlers:
        with contextlib.suppress(Exception):
            handler.flush()
    with contextlib.suppress(Exception):
        sys.stderr.flush()


RESUME_SELF = "from agent_context.server import main; main()"


def exec_release(target: Path, state: dict) -> None:
    "Replace this process with `target`'s relay on the same stdio. If that exec fails, stdin is\n    already frozen, so this process cannot go on serving: it execs its own release with the same\n    handover instead, and exits 1 (the client sees the server end) only if that fails too."
    program = release_program(target)
    path = relay_update.write_resume_file(state)
    env = dict(os.environ)
    env[RESUME_ENV] = str(path)
    _flush_everything()
    try:
        os.execve(program, [str(program), *sys.argv[1:]], env)
    except OSError as e:
        log.warning("agent-context: relay swap exec failed (%s); resuming on this release", e)
    _flush_everything()
    try:
        os.execve(sys.executable, [sys.executable, "-c", RESUME_SELF], env)
    except OSError as e:
        relay_update.drop_resume_file(path)
        log.error("agent-context: relay could not resume after a failed swap: %s", e)
        _flush_everything()
        os._exit(1)


async def swap_when_released(watch: ReleaseWatch, st: dict) -> None:
    "The bridge's swap task: wait for a new release, install it, quiesce, exec. Runs for the\n    life of the bridge; returns only if the stdio transport cannot be handed over."
    pipe = relay_stdio.ACTIVE
    if pipe is None:
        return  
    while True:
        await watch.release_seen.wait()
        etag = watch.wanted
        watch.release_seen = anyio.Event()
        if etag is None:
            continue
        log.info("agent-context: ls serves relay release %s; this relay runs %s", etag,
                 watch.own or "an unrecorded one")
        target = await anyio.to_thread.run_sync(install, etag)
        if target is None:
            watch.failed.add(etag)
            continue
        if not SWAPS_IN_PLACE:
            watch.failed.add(etag)  
            log.warning("agent-context: relay release %s installed; the next session starts on "
                        "it (no in-place switch on this OS)", etag)
            continue
        
        while st["init_msg"] is None or not st["init_answered"] or st["init_note"] is None:
            await anyio.sleep(_DRAIN_POLL_SECONDS)
        leftover = pipe.freeze()
        
        
        while not st["stdin_handed_over"] or st["pending"]:
            if st["client_eof"]:
                return
            await anyio.sleep(_DRAIN_POLL_SECONDS)
        await anyio.to_thread.run_sync(pipe.close_writer)
        log.warning("agent-context: relay switching to release %s in place", etag)
        exec_release(target, {**resume_state(st, leftover), "release": etag})
        return  
