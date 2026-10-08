"Wake this bridge's own harness session with a peer message (policy).\n\nThe daemon holds every session's mailbox and pushes a message to the recipient's bridge as\n`notifications/agent-context/peer_message`, on the event stream the bridge already holds open.\nThe bridge runs on the recipient's machine as a child of the session it serves, so it performs\nthe wake there: no port, no ssh, no terminal. The push is consumed here and never forwarded,\nsince no harness knows the method.\n\nRoutes, by what the session's environment offers:\n\n- Claude Code: the per-session inbox socket (`CLAUDE_CODE_MESSAGING_SOCKET`), two JSON lines,\n  the auth line with `CLAUDE_CODE_MESSAGING_TOKEN` and then the message. It starts a turn in an\n  idle session and sends no reply, so a clean write is all a sender can learn. A Windows\n  session names a pipe there; that route is not written.\n- Codex: `codex queue --thread <id> --message <text>`, the thread being the session of the\n  hook claim that owns this bridge. Known by the bridge's parent process, `codex app-server`.\n- opencode: a file in a spool the store's opencode plugin watches; the plugin admits it to\n  the directory's most recently active session (`ctx.session.synthetic`).\n- pi: the bridge's client is the store's pi extension, which names itself in\n  `AGENT_CONTEXT_PEER_CLIENT`. The push is forwarded to it, not consumed, and it starts the\n  turn.\n\nA session with no route is not woken: the daemon keeps its message in the mailbox and pushes\n`notifications/agent-context/peer_waiting` with the count. The bridge leaves a flag named for\nthe session's hook claim, and the session's `peer-message-notice` hook, at its next tool call\nor prompt, tells the agent to call read_notifications. The hook pays one `stat` and never\ncalls the daemon.\n\nStdlib only at import: the bridge imports this on every start."
import json
import logging
import os
import re
import socket
import time
import uuid
from collections.abc import Mapping

log = logging.getLogger(__name__)

PEER_MESSAGE_METHOD = "notifications/agent-context/peer_message"
PEER_WAITING_METHOD = "notifications/agent-context/peer_waiting"


NOTICE_HEADER = "x-bridge-notice"
NOTICE_HOOK = "hook"
WAITING_DIR = "peer-waiting"
REF_CHARS = 8
HELD_SUFFIX = ".held"
WATCH_SUFFIX = ".watch"
PEER_WATCH_METHOD = "notifications/agent-context/peer_watch"
SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"
_SOCKET_TIMEOUT_SECONDS = 5.0



CLIENT_ENV = "AGENT_CONTEXT_PEER_CLIENT"
FORWARD_ROUTES = frozenset({"pi"})

COMMAND_ROUTES = frozenset({"codex"})
SPOOL_DIR = "peer-spool"
_COMMAND_TIMEOUT_SECONDS = 30.0



WAKE_HEADER = "x-bridge-wake"
BRIDGE_HEADER = "x-bridge-id"
BRIDGE_ENV = "AGENT_CONTEXT_BRIDGE_ID"

SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


CWD_HEADER = "x-bridge-cwd"
MAX_CWD_CHARS = 1024


class NoRoute(Exception):
    "This session's harness offers no way to wake it."


def peer_message(root: object) -> dict | None:
    'The push\'s `{"id", "text"}`, `{}` when malformed, and None for any other message.\n    `text` is what the session reads, envelope included: the daemon formats it once for\n    every harness.'
    if getattr(root, "method", None) != PEER_MESSAGE_METHOD:
        return None
    params = getattr(root, "params", None)
    text = params.get("text") if isinstance(params, dict) else None
    if not isinstance(text, str) or not text:
        return {}
    out = {"id": params.get("id"), "text": text}
    if isinstance(params.get("sender"), str):      
        out["sender"] = params["sender"]
    return out


def _wake_claude_code(path: str, token: str, text: str) -> None:
    lines = (json.dumps({"type": "auth", "token": token}) + "\n"
             + json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(_SOCKET_TIMEOUT_SECONDS)
        s.connect(path)
        s.sendall(lines.encode("utf-8"))


def route(env: Mapping[str, str] | None = None) -> str | None:
    "The name of the wake route this session's environment offers, or None."
    env = os.environ if env is None else env
    if (env.get(SOCKET_ENV) and env.get(TOKEN_ENV) and hasattr(socket, "AF_UNIX")
            and os.name != "nt"):
        return "claude-code"
    if env.get(CLIENT_ENV) in FORWARD_ROUTES | COMMAND_ROUTES:
        return env[CLIENT_ENV]
    if env is os.environ:
        parent = _parent_command()
        if parent == "codex":
            return "codex"
        
        
        if parent == "opencode" and _opencode_spool().is_dir():
            return "opencode"
    return None


def _opencode_spool():
    "Where this bridge leaves a message for opencode's plugin. opencode runs one service\n    for the user, with one plugin instance and one bridge per project directory, so the\n    place is named by the service's pid and the directory. The store's generated plugin\n    (harness-materialize.py, `peerSpool`) makes the directory and watches it."
    import hashlib

    from . import paths
    real = os.path.realpath(os.getcwd())
    name = hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]
    return paths.state_dir() / SPOOL_DIR / str(os.getppid()) / name


def _wake_opencode(text: str) -> None:
    ' wake opencode.'
    from . import paths
    spool = _opencode_spool()
    if not spool.is_dir():
        raise NoRoute("opencode's plugin is not watching for this directory")
    paths.write_atomic(spool / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json",
                       json.dumps({"text": text}), mode=0o600)


def _parent_command() -> str:
    'The name of the program that started this bridge, "" when it cannot be read. Codex\n    gives its MCP servers nothing in the environment that names it, so its own process is\n    the evidence: the bridge is a child of `codex app-server`.'
    ppid = os.getppid()
    try:
        with open(f"/proc/{ppid}/cmdline", "rb") as fh:
            first = fh.read().split(b"\0", 1)[0].decode("utf-8", "replace")
    except OSError:
        try:
            import subprocess
            first = subprocess.run(["ps", "-o", "comm=", "-p", str(ppid)], capture_output=True,
                                   text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return os.path.basename(first)


def _wake_codex(text: str) -> None:
    ' wake codex.'
    import subprocess

    from . import claims
    rec = claims.owner_record(os.getpid()) or {}
    thread = str(rec.get("session") or "")
    if not thread or thread.startswith("pid"):
        raise NoRoute("this Codex session has no thread yet")
    cwd = rec.get("cwd") if os.path.isdir(str(rec.get("cwd") or "")) else None
    done = subprocess.run(["codex", "queue", "--thread", thread, "--message", text], cwd=cwd,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=_COMMAND_TIMEOUT_SECONDS, check=False)
    if done.returncode != 0:
        raise OSError(f"codex queue exited {done.returncode}: "
                      f"{(done.stderr or done.stdout).strip()[:200]}")


def forwards(env: Mapping[str, str] | None = None) -> bool:
    "True when this bridge's own client performs the wake: the push goes on to it and is\n    not consumed here."
    return route(env) in FORWARD_ROUTES


def wake(text: str, env: Mapping[str, str] | None = None) -> str:
    "Hand `text` to this process's harness session and return the route's name. Raises\n    NoRoute when the environment names none, OSError when the route failed. Blocks for at\n    most the socket timeout, so the bridge calls it off the event loop."
    env = os.environ if env is None else env
    name = route(env)
    if name == "claude-code":
        _wake_claude_code(env[SOCKET_ENV], env[TOKEN_ENV], text)
        return name
    if name == "codex":
        _wake_codex(text)
        return name
    if name == "opencode":
        _wake_opencode(text)
        return name
    raise NoRoute("no wake route in this session's environment")


def bridge_id() -> str:
    "This bridge's id. Kept in the environment, which `execv` carries into the next release\n    of the relay (policy), so a swap keeps it."
    kept = os.environ.get(BRIDGE_ENV)
    if kept:
        return kept
    session = os.environ.get(SESSION_ENV, "").strip()
    if session:
        import hashlib
        made = hashlib.sha256(f"session:{session}".encode()).hexdigest()[:32]
    else:
        made = uuid.uuid4().hex
    os.environ[BRIDGE_ENV] = made
    return made


def connect_headers() -> dict[str, str]:
    'What this bridge says of itself on a connection to the daemon.'
    out = {BRIDGE_HEADER: bridge_id()}
    name = route()
    if name:
        out[WAKE_HEADER] = name
    
    
    out[NOTICE_HEADER] = NOTICE_HOOK
    try:
        from urllib.parse import quote
        cwd = quote(os.getcwd(), safe="/")
        if cwd and len(cwd) <= MAX_CWD_CHARS:
            out[CWD_HEADER] = cwd
    except (OSError, ValueError):   
        pass
    return out


def declared_cwd(value: str) -> str | None:
    'The directory a `CWD_HEADER` value names, or None for one that is absent, too long, or\n    not an absolute path free of control characters.'
    from urllib.parse import unquote
    if not value or len(value) > MAX_CWD_CHARS:
        return None
    path = unquote(value)
    absolute = path.startswith("/") or re.match(r"[A-Za-z]:[\\/]", path)
    if not absolute or re.search(r"[\x00-\x1f\x7f]", path):
        return None
    return path


def peer_waiting(root: object) -> int | None:
    'The count a `peer_waiting` push carries, 0 when malformed, and None for any other\n    message.'
    if getattr(root, "method", None) != PEER_WAITING_METHOD:
        return None
    params = getattr(root, "params", None)
    count = params.get("count") if isinstance(params, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def peer_watch(root: object) -> bool | None:
    "True for the daemon's `peer_watch` push, None for any other message."
    return True if getattr(root, "method", None) == PEER_WATCH_METHOD else None


def note_watch() -> str | None:
    "Leave `<flag>.watch`: a sender asked to be told when this session next goes idle. The\n    session's `peer-message-notice` hook sees it when the turn ends, removes it and reports\n    the idle state to the daemon. Returns the path, None when no hook claim owns this bridge.\n    Never raises."
    try:
        flag = _flag_path()
        if flag is None:
            return None
        from . import paths
        watch = flag.with_name(flag.name + WATCH_SUFFIX)
        
        
        paths.write_atomic(watch, bridge_id(), mode=0o600)
        return str(watch)
    except Exception as exc:
        log.warning("agent-context bridge: idle watch not written: %r", exc)
        return None


def note_waiting(count: int) -> str | None:
    "Leave the flag this session's hook reads, and return its path; None when no hook claim\n    owns this bridge (a harness with no hooks has nothing to read the flag). The flag is named\n    by the session's ref, the first eight characters of its session id (what a live claim\n    publishes), so the hook finds it from its own session id. Never raises."
    try:
        path = _flag_path()
        if not count or path is None:
            return None
        from . import paths
        paths.write_atomic(path, str(count), mode=0o600)
        return str(path)
    except Exception as exc:   
        log.warning("agent-context bridge: waiting flag not written: %r", exc)
        return None


def _flag_path():
    '<state-dir>/peer-waiting/<ref> for the hook claim that owns this bridge, its directory\n    made; None when no hook claim owns it.'
    
    from . import claims
    rec = claims.owner(claims.read_live(), os.getpid())
    ref = str((rec or {}).get("session") or "")[:REF_CHARS]
    if not ref or ref.startswith("pid"):   
        return None
    path = claims.claims_dir().parent / WAITING_DIR / re.sub(r"[^\w.-]", "_", ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def hold(text: str) -> str | None:
    "Keep a message whose wake failed where the session's hook delivers it: one JSON line\n    appended to `<flag>.held`, which `peer-message-notice` prints in full at the session's\n    next tool call or prompt. The daemon stored nothing for a session with a wake route, so\n    without this a failed wake loses the message. Returns the file's path, None when no hook\n    claim owns this bridge. Never raises."
    try:
        flag = _flag_path()
        if flag is None:
            return None
        from . import paths
        held = flag.with_name(flag.name + HELD_SUFFIX)
        try:
            earlier = held.read_text(encoding="utf-8")
        except OSError:
            earlier = ""
        paths.write_atomic(held, earlier + json.dumps({"text": text}) + "\n", mode=0o600)
        return str(held)
    except Exception as exc:
        log.warning("agent-context bridge: undelivered message not held: %r", exc)
        return None


def attempt(message: dict) -> str | None:
    "Wake the session with a parsed push and return the route used, None when it did not\n    arrive; the message is then the caller's to report or hold. Never raises: a failed wake\n    must not take the bridge down."
    text = message.get("text")
    if not text:
        log.warning("agent-context bridge: dropped a malformed peer message")
        return None
    try:
        route = wake(text)
    except Exception as exc:   
        log.warning("agent-context bridge: peer message %s not delivered: %r",
                    message.get("id"), exc)
        return None
    log.info("agent-context bridge: peer message %s delivered by %s", message.get("id"), route)
    return route


def deliver(message: dict) -> str | None:
    "`attempt`, and a message whose wake failed is held here for the session's hook: what\n    a bridge does when it cannot report the failure to the daemon."
    route = attempt(message)
    if route is None and message.get("text"):
        hold(message["text"])
    return route










REPORT_TOOL = "relay_report"
REPORT_KIND = "peer"
REPORT_ID_PREFIX = "ac-peer-"


def report_id() -> str:
    return REPORT_ID_PREFIX + uuid.uuid4().hex[:12]


def report_request(rid: str, body: dict) -> object:
    "The request that carries one report, ready for the bridge's stream to the daemon."
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest
    return SessionMessage(JSONRPCMessage(JSONRPCRequest(
        jsonrpc="2.0", id=rid, method="tools/call",
        params={"name": REPORT_TOOL, "arguments": {
            "kind": REPORT_KIND, "uuid_hint": "", "body": json.dumps(body)}})))


def undelivered(message: dict) -> dict:
    'The report of a push whose wake failed.'
    return {"event": "undelivered", "id": message.get("id"), "text": message.get("text"),
            "sender": message.get("sender")}


def turn(state: str) -> dict:
    "The report that this session's turn started (`busy`) or ended (`idle`)."
    return {"event": "turn", "state": state}








TURN_STATES = ("busy", "idle")

TURN_POLL_SECONDS = 2.0


_TURN_RESOLVE_SECONDS = 30.0


class TurnWatch:
    "What this bridge has to tell the daemon of its session's turn. `poll` and `woke` return\n    the state to report, None when the daemon already has it. Neither raises."

    def __init__(self) -> None:
        self.path = None        
        self.tried = 0.0
        self.mtime: int | None = None
        self.state: str | None = None
        self.told: str | None = None

    def forget(self) -> None:
        "A new connection to the daemon: it knows nothing of this session's turn."
        self.told = None

    def _next(self) -> str | None:
        if self.state is None or self.state == self.told:
            return None
        self.told = self.state
        return self.state

    def poll(self) -> str | None:
        try:
            if self.path is None:
                now = time.monotonic()
                if self.tried and now - self.tried < _TURN_RESOLVE_SECONDS:
                    return None
                self.tried = now
                from . import claims
                session = str((claims.owner_record(os.getpid()) or {}).get("session") or "")
                if not session or session.startswith("pid"):   
                    return None
                self.path = claims.claims_dir() / (re.sub(r"[^\w.-]", "_", session) + ".json")
            try:
                mtime = os.stat(self.path).st_mtime_ns
            except OSError:         
                self.path, self.mtime, self.state = None, None, None
                return None
            if mtime != self.mtime:
                with open(self.path, encoding="utf-8") as fh:
                    said = json.load(fh).get("turn")
                self.mtime, self.state = mtime, said if said in TURN_STATES else None
            return self._next()
        except Exception as exc:   
            log.debug("agent-context bridge: turn state not read: %r", exc)
            return None

    def woke(self) -> str | None:
        "This bridge started a turn in the session itself, and a wake fires no prompt hook.\n        The claim's own word stands again once the hook rewrites it. A session whose claim\n        never named a turn has no hook to end one, so nothing is said of it."
        if self.state is None:
            return None
        self.state = "busy"
        return self._next()


def report_accepted(root: object) -> bool:
    "True when the daemon's answer to a report says it acted. A daemon from before the\n    reports answers with an error, and the bridge then holds the message itself."
    result = getattr(root, "result", None)
    if getattr(root, "error", None) is not None or not isinstance(result, dict):
        return False
    if result.get("isError"):
        return False
    for block in result.get("content") or []:
        try:
            answer = json.loads(block.get("text") or "")
        except (AttributeError, ValueError):
            return False
        return isinstance(answer, dict) and "error" not in answer
    return False
