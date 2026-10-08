"Hermetic guard for the server suite.\n\nNo test may write the machine's real daemon state or store, or open a connection to a host\nthat is not on this machine. conftest imports the fixtures below, so they apply to every test.\n\n  * writes    an audit hook refuses a write, rename, remove, mkdir or rmdir under a protected\n              root (the real state, health and log dirs and the real store's entity dirs) and\n              records who did it: the test, the path and whether the thread that did it was\n              started by an earlier test and outlived it;\n  * network   `socket.connect` refuses a non-loopback host for the test's duration. The relay\n              tests that call `S.main()` with the real host name used to reach production this\n              way (401s from the ls tailnet address);\n  * report    a recorded violation fails the test that was running, or the next test if none\n              was, and fails the session's exit code if it is still pending at the end.\n\nLimits, so nobody reads a green run as more than it is: only writes made by this Python\nprocess are seen (not a git or shell child), and a DNS lookup is not a connection.\n\nBoth allowlists start empty. An entry needs a written reason next to it."
from __future__ import annotations

import fcntl
import ipaddress
import os
import socket
import sys
import threading
from typing import Any

import pytest

ALLOWED_WRITE_PREFIXES: list[str] = []
ALLOWED_HOSTS: list[str] = []



_STORE_DIRS = ("machines", "global", "projects", "workspaces")

_WRITE_MODE_CHARS = frozenset("wax+")
_OPS = {"os.rename": "renamed", "os.link": "linked", "os.symlink": "symlinked",
        "os.truncate": "truncated", "os.chown": "changed the owner of",
        "os.chmod": "changed the mode of", "os.remove": "removed", "os.rmdir": "removed",
        "os.mkdir": "created"}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


class HermeticViolation(OSError):
    'Raised at the moment a test reaches something outside its own sandbox.'


def default_roots() -> list[str]:
    "The paths a test must never write. AGENT_CONTEXT_GUARD_ROOTS (os.pathsep list) replaces\n    them, which is how the guard's own end-to-end test aims it at a scratch directory."
    override = os.environ.get("AGENT_CONTEXT_GUARD_ROOTS")
    if override is not None:
        return [os.path.abspath(p) for p in override.split(os.pathsep) if p]
    home = os.path.expanduser("~")
    
    
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    roots: list[str] = [
        os.path.join(xdg, "agent-context"),
        os.path.join(home, ".local", "state", "agent-context"),
        os.path.join(home, "Library", "Application Support", "agent-context"),
        os.path.join(home, "Library", "Logs", "agent-context"),
    ]
    stores = {os.path.join(home, ".agent-context")}
    if os.environ.get("AGENT_CONTEXT_STORE"):
        stores.add(os.environ["AGENT_CONTEXT_STORE"])
    for store in stores:
        roots += [os.path.abspath(os.path.join(store, d)) for d in _STORE_DIRS]
    return sorted({os.path.abspath(r) for r in roots})


def is_loopback(host: str) -> bool:
    if host in ("", "localhost") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def describe(v: dict[str, Any]) -> str:
    who = v["test"] or "no test was running"
    if v["kind"] == "write":
        what = f"{v['op']} {v['path']}, which is under the real {v['root']}"
    else:
        what = f"connected to {v['target']}, which is not a loopback host"
    if v["thread_test"] and v["outlived"]:
        thread = (f"thread {v['thread']!r} was started by {v['thread_test']} and outlived "
                  "that test")
    elif v["thread_test"]:
        thread = f"thread {v['thread']!r} started in this test"
    else:
        thread = f"thread {v['thread']!r} (main or not started by a test)"
    return f"hermetic guard: {who}: {what}; {thread}"


class Guard:
    def __init__(self, roots: list[str]) -> None:
        self.roots = [os.path.abspath(r) for r in roots]
        self._real_roots = [os.path.realpath(r) for r in self.roots]
        self.current: str | None = None
        
        
        self.sandboxes: list[str] = []
        self.violations: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _root_of(self, path: Any, follow: bool = True) -> str | None:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return None
        try:
            p = os.path.abspath(os.fsdecode(path))
        except (TypeError, ValueError):
            return None
        if any(p == a or p.startswith(a + os.sep) for a in ALLOWED_WRITE_PREFIXES):
            return None
        
        
        
        if "__pycache__" in p.split(os.sep):
            return None
        
        
        real = (os.path.realpath(p) if follow
                else os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p)))
        if self._in_tmpdir(real) or self._in_sandbox(real):
            return None
        for r, rr in zip(self.roots, self._real_roots):
            for cand, root in ((p, r), (real, rr)):
                if cand == root or cand.startswith(root + os.sep):
                    return r
        return None

    def _in_sandbox(self, real: str) -> bool:
        return any(self._within(real, sb) for sb in self.sandboxes)

    def _within(self, real: str, sandbox: str) -> bool:
        "`real` is inside the sandbox directory and not inside a protected root that is the\n        sandbox itself or lies within it. A root that contains the sandbox (the state dir\n        holding the gate's TMPDIR) does not matter: the sandbox is carved out of it."
        if not (real == sandbox or real.startswith(sandbox + os.sep)):
            return False
        for r in self._real_roots:
            if (r == sandbox or r.startswith(sandbox + os.sep)) and (
                    real == r or real.startswith(r + os.sep)):
                return False
        return True

    def _in_tmpdir(self, real: str) -> bool:
        "True for a real path inside the process's TMPDIR: that directory is the sandbox, and\n        the deploy gate puts it under the state dir (daemon._gate_env). TMPDIR that is relative\n        exempts nothing. `real` is already resolved, so `..` and a symlink out of TMPDIR do not\n        count. The directory itself counts: TemporaryFile (capfd) opens it with O_TMPFILE."
        tmp = os.environ.get("TMPDIR")
        if not tmp or not os.path.isabs(tmp):
            return False
        return self._within(real, os.path.realpath(tmp))

    def _record(self, kind: str, **fields: Any) -> dict[str, Any]:
        t = threading.current_thread()
        started_in = getattr(t, "_hermetic_started_in", None)
        v = {"kind": kind, "test": self.current, "thread": t.name, "thread_test": started_in,
             "outlived": started_in is not None and started_in != self.current, **fields}
        with self._lock:
            self.violations.append(v)
        return v

    @staticmethod
    def _at(path: Any, dir_fd: Any) -> Any:
        "A relative path given with dir_fd is relative to that directory, not to the\n        process's working directory."
        if not isinstance(dir_fd, int) or dir_fd < 0:
            return path
        if not isinstance(path, (str, bytes, os.PathLike)):
            return path
        p = os.fsdecode(path)
        if os.path.isabs(p):
            return p
        try:
            return os.path.join(Guard._fd_dir(dir_fd), p)
        except OSError:
            return path

    @staticmethod
    def _fd_dir(dir_fd: int) -> str:
        'The directory an open fd names. Linux reads /proc; macOS has no /proc and asks\n        fcntl(F_GETPATH), which returns a NUL-padded buffer of MAXPATHLEN (1024) bytes.\n        Without the macOS branch every dir_fd write there went unchecked.'
        get_path = getattr(fcntl, "F_GETPATH", None)
        if get_path is None:
            return os.readlink(f"/proc/self/fd/{dir_fd}")
        buf = fcntl.fcntl(dir_fd, get_path, bytes(1024))
        return os.fsdecode(buf.split(b"\0", 1)[0])

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        'sys.addaudithook target. Raising here stops the operation before it happens.'
        try:
            if event == "open":
                path, mode, flags = args[0], args[1], args[2]
                is_write = ((isinstance(mode, str) and bool(_WRITE_MODE_CHARS & set(mode)))
                            or (isinstance(flags, int) and bool(flags & _WRITE_FLAGS)))
                if not is_write:
                    return
                paths, op = [path], "opened for writing"
            elif event in ("os.rename", "os.link"):
                paths = [self._at(args[0], args[2]), self._at(args[1], args[3])]
                op = _OPS[event]
            elif event == "os.symlink":
                paths, op = [self._at(args[1], args[2])], _OPS[event]
            elif event in ("os.truncate", "os.chown"):
                paths, op = [args[0]], _OPS[event]
            elif event == "os.chmod":
                paths, op = [self._at(args[0], args[2])], _OPS[event]
            elif event in ("os.remove", "os.rmdir", "os.mkdir"):
                target = self._at(args[0], args[-1])
                
                
                if event == "os.mkdir" and isinstance(target, (str, bytes, os.PathLike)) \
                        and os.path.isdir(target) and not os.path.islink(target):
                    return
                paths, op = [target], _OPS[event]
            else:
                return
        except (IndexError, TypeError):
            return
        follow = event in ("open", "os.truncate", "os.chmod", "os.chown")
        for p in paths:
            root = self._root_of(p, follow)
            if root:
                v = self._record("write", op=op, path=os.fsdecode(p), root=root)
                raise HermeticViolation(describe(v))

    def refuse_network(self, host: str, address: Any) -> None:
        v = self._record("network", target=f"{host}:{address[1]}" if len(address) > 1 else host)
        raise HermeticViolation(describe(v))

    def take(self, test: str | None) -> list[dict[str, Any]]:
        'Remove and return the violations recorded while `test` was running.'
        with self._lock:
            mine = [v for v in self.violations if v["test"] == test]
            self.violations = [v for v in self.violations if v["test"] != test]
        return mine

    def take_all(self) -> list[dict[str, Any]]:
        with self._lock:
            out, self.violations = self.violations, []
        return out


GUARD = Guard(default_roots())
_installed = False


def install() -> None:
    'Once per process: the audit hook cannot be removed, and thread starts are tagged with\n    the test that made them so a leaked thread can be recognised later.'
    global _installed
    if _installed:
        return
    _installed = True
    sys.addaudithook(GUARD.audit)
    original_start = threading.Thread.start

    def start(self: threading.Thread, *a: Any, **k: Any) -> None:
        self._hermetic_started_in = GUARD.current  
        return original_start(self, *a, **k)

    threading.Thread.start = start  


install()


@pytest.fixture(scope="session", autouse=True)
def _hermetic_basetemp(tmp_path_factory: pytest.TempPathFactory) -> None:
    "Names pytest's own numbered temp dir to the guard. The deploy gate puts it under the\n    state dir (TMPDIR=<state>/gate-tmp), and a test that changes TMPDIR must not turn its own\n    tmp_path into a protected path."
    GUARD.sandboxes.append(os.path.realpath(str(tmp_path_factory.getbasetemp())))


@pytest.fixture(autouse=True)
def _hermetic_guard(request: pytest.FixtureRequest):
    'Names this test to the guard, and fails it for anything the guard recorded.'
    node = request.node.nodeid
    leftover = GUARD.take(None)
    GUARD.current = node
    if leftover:
        pytest.fail("; ".join(describe(v) for v in leftover)
                    + " (recorded before this test started)", pytrace=False)
    yield
    mine = GUARD.take(node)
    GUARD.current = None
    if mine:
        pytest.fail("; ".join(describe(v) for v in mine), pytrace=False)


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch: pytest.MonkeyPatch):
    'Refuse a connection to a host that is not on this machine, and fail the test.'
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(sock: socket.socket, address: Any) -> None:
        if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple):
            return
        host = str(address[0])
        if not is_loopback(host) and host not in ALLOWED_HOSTS:
            GUARD.refuse_network(host, address)

    def connect(self: socket.socket, address: Any) -> None:
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    'A violation recorded after the last test still fails the run.'
    left = GUARD.take_all()
    if left:
        sys.stderr.write("\n" + "\n".join(describe(v) for v in left) + "\n")
        if exitstatus == 0:
            session.exitstatus = 1
