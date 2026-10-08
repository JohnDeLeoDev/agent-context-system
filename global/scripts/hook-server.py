#!/usr/bin/env python3
"hook-server: a warm hook dispatcher for this host (policy).\n\nhook-client, the command the harness runs per dispatched hook event, connects here. This\nprocess has hook-dispatch.py loaded and forks one child per call; the child takes the\nclient's working directory, environment, arguments and stdin, runs hook-dispatch's run()\nas a cold start would, and sends back stdout, stderr and the exit code. A call skips\nPython's start-up and the dispatcher's imports, and fork keeps each call as isolated as a\nprocess of its own: a guard that changes the environment, the directory or a signal\nhandler changes it in the child only.\n\nWARMING. A child reports the standard-library modules and the regular expressions it\nused that this process does not hold yet; this process imports and compiles them, so\nthe next child starts with them. Nothing else is preloaded: store modules read their\nenvironment when they load, and a copy loaded here would carry this process's values\ninto every call. The learned names are kept in hook-server-warm.json for the next start.\n\nLIFE. hook-client starts this on demand when it finds no server, and uses the cold path\nfor that call. One server per host: a lock file decides, and a second copy exits. It\nexits after IDLE_SECS with no call, and re-executes itself when this file, the\ndispatcher or harness_paths changes on disk, answering that call with a retry so the\nclient runs it cold. A call whose dispatcher path or Python differs from this server's\ngets a retry too. It is not a service and runs on relay hosts as well (policy concerns\nthe store daemon).\n\nProtocol: see hook-client.rs. Usage: hook-server.py (no arguments). HOOK_SERVER_SOCK\noverrides the socket path, HOOK_SERVER_IDLE the idle limit in seconds."
import fcntl
import importlib.util
import io
import json
import os
import re
import select
import signal
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DISPATCHER = os.path.join(HERE, "hook-dispatch.py")


ADAPTER = os.path.join(HERE, "codex-hook-adapter.py")
MAGIC = b"AHS1"
IDLE_SECS = float(os.environ.get("HOOK_SERVER_IDLE") or 3600)
WARM_SAVE_SECS = 60

REPORT_LIMIT = select.PIPE_BUF


def load_dispatcher(name="hook_dispatch", path=DISPATCHER):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    saved = list(sys.argv)
    sys.argv[:] = [path]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv[:] = saved
    return mod


HD = load_dispatcher()
hp = HD.hp
try:
    CODEX = load_dispatcher("codex_hook_adapter", ADAPTER)
except Exception:  
    CODEX = None


def cache_path(name):
    return os.path.join(hp.cache_dir(), name)


def socket_path():
    return os.environ.get("HOOK_SERVER_SOCK") or cache_path("hook-server.sock")





NEVER_WARM = frozenset(("this", "antigravity", "__main__", "__phello__"))
STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) - NEVER_WARM


def regex_keys():
    "(pattern, flags) of every str pattern in re's cache."
    cache = getattr(re, "_cache", None)
    if not isinstance(cache, dict):
        return set()
    return {(k[1], k[2]) for k in cache
            if isinstance(k, tuple) and len(k) == 3 and isinstance(k[1], str)}


class Warm:
    'What this process has loaded, and what children report they needed beyond it.'

    def __init__(self):
        self.path = cache_path("hook-server-warm.json")
        self.modules = set()
        self.regex = set()
        self.dirty = False
        self.saved_at = time.monotonic()

    def baseline(self):
        return set(sys.modules), regex_keys()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(doc, dict):
            self.apply(doc.get("modules") or [], doc.get("regex") or [])
        self.dirty = False

    def apply(self, modules, regex):
        for name in modules:
            if not isinstance(name, str) or name in sys.modules:
                continue
            if name.partition(".")[0] not in STDLIB:
                continue
            try:
                importlib.import_module(name)
            except Exception:  
                continue
            self.modules.add(name)
            self.dirty = True
        for item in regex:
            if not (isinstance(item, list) and len(item) == 2):
                continue
            pattern, flags = item
            if not isinstance(pattern, str) or not isinstance(flags, int):
                continue
            if (pattern, flags) in self.regex:
                continue
            try:
                re.compile(pattern, flags)
            except (re.error, TypeError, ValueError, OverflowError):
                continue
            self.regex.add((pattern, flags))
            self.dirty = True

    def save(self, force=False):
        if not self.dirty or (not force and time.monotonic() - self.saved_at < WARM_SAVE_SECS):
            return
        doc = {"modules": sorted(self.modules), "regex": sorted([p, f] for p, f in self.regex)}
        partial = "%s.%d.tmp" % (self.path, os.getpid())
        try:
            with open(partial, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            os.replace(partial, self.path)
        except OSError:
            return
        self.dirty = False
        self.saved_at = time.monotonic()


def child_report(before_modules, before_regex):
    'One JSON line of what this child loaded beyond its parent, within REPORT_LIMIT.'
    modules = sorted(n for n in set(sys.modules) - before_modules
                     if n.partition(".")[0] in STDLIB)
    regex = sorted([p, f] for p, f in regex_keys() - before_regex)
    doc = {"modules": modules, "regex": regex}
    line = json.dumps(doc).encode("utf-8") + b"\n"
    while len(line) > REPORT_LIMIT and (doc["regex"] or doc["modules"]):
        if doc["regex"]:
            doc["regex"] = doc["regex"][: len(doc["regex"]) // 2]
        else:
            doc["modules"] = doc["modules"][: len(doc["modules"]) // 2]
        line = json.dumps(doc).encode("utf-8") + b"\n"
    return line if len(line) <= REPORT_LIMIT else b""


def drain_reports(fd, warm, pending):
    'Apply every complete report waiting on the pipe.'
    while True:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            break
        except OSError:
            break
        if not chunk:
            break
        pending += chunk
    *lines, rest = pending.split(b"\n")
    for line in lines:
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            warm.apply(doc.get("modules") or [], doc.get("regex") or [])
    return rest




def frame(tag, data):
    return tag + struct.pack(">I", len(data)) + data


def read_request(conn):
    '(cwd, python, dispatcher, args, env, payload) or None for a malformed request.'
    chunks = []
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    data = b"".join(chunks)
    if len(data) < 8 or data[:4] != MAGIC:
        return None
    (length,) = struct.unpack(">I", data[4:8])
    fields = data[8:8 + length].split(b"\0")[:-1]
    payload = data[8 + length:]
    try:
        count = int(fields[3])
    except (IndexError, ValueError):
        return None
    if len(fields) < 4 + count:
        return None
    args = [os.fsdecode(a) for a in fields[4:4 + count]]
    env = {}
    for pair in fields[4 + count:]:
        key, sep, value = pair.partition(b"=")
        if sep and key:
            env[os.fsdecode(key)] = os.fsdecode(value)
    return (os.fsdecode(fields[0]), os.fsdecode(fields[1]), os.fsdecode(fields[2]),
            args, env, payload)


def same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False




CLIENT_PID_ENV = "AGENT_CONTEXT_HOOK_CLIENT_PID"


def client_pid(conn):
    'The pid of the hook-client on the other end of `conn`, or 0 when the platform does\n    not say. Linux: SO_PEERCRED. macOS: LOCAL_PEERPID.'
    import socket
    import struct
    try:
        if hasattr(socket, "SO_PEERCRED"):
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[0]
        if sys.platform == "darwin":
            raw = conn.getsockopt(0, 0x002, struct.calcsize("i"))   
            return struct.unpack("i", raw)[0]
    except (OSError, struct.error):
        pass
    return 0


def serve_call(conn, report_fd, before):
    'Runs in the forked child; never returns.'
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        request = read_request(conn)
        if request is None:
            conn.sendall(frame(b"r", b""))
            os._exit(0)
        cwd, python, dispatcher, args, env, payload = request
        if same_file(dispatcher, DISPATCHER):
            entry, script = HD.run, DISPATCHER
        elif CODEX is not None and same_file(dispatcher, ADAPTER):
            entry, script = (lambda argv: CODEX.main(argv[1:])), ADAPTER
        else:
            entry = script = None
        if entry is None or not same_file(python, sys.executable):
            conn.sendall(frame(b"r", b""))
            os._exit(0)
        out_file, err_file = HD.Capture._unlinked(), HD.Capture._unlinked()
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        os.dup2(out_file.fileno(), 1)
        os.dup2(err_file.fileno(), 2)
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
        sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
        sys.stderr = open(2, "w", encoding="utf-8", closefd=False)
        os.environ.clear()
        os.environ.update(env)
        
        
        
        
        client = client_pid(conn)
        if client:
            os.environ[CLIENT_PID_ENV] = str(client)
        else:
            os.environ.pop(CLIENT_PID_ENV, None)
        try:
            os.chdir(cwd)
        except OSError:
            pass
        argv = [script] + args
        sys.argv[:] = argv
    except BaseException:  
        try:
            conn.sendall(frame(b"r", b""))
        except OSError:
            pass
        os._exit(0)
    try:
        code = entry(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:  
        code = 0
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  
            pass
    try:
        reply = b""
        for tag, fh in ((b"o", out_file), (b"e", err_file)):
            fh.seek(0)
            data = fh.read()
            if data:
                reply += frame(tag, data)
        conn.sendall(reply + frame(b"x", bytes([(code or 0) & 255])))
    except OSError:
        pass
    try:
        line = child_report(*before)
        if line:
            os.write(report_fd, line)
    except OSError:
        pass
    os._exit(0)




def watched():
    '(path, mtime_ns, size) of the files whose change makes this process re-execute.'
    out = []
    for path in (os.path.abspath(__file__), DISPATCHER, ADAPTER, hp.__file__):
        try:
            st = os.stat(path)
            out.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((path, None, None))
    return out


def bind(path):
    'The listening socket, or None when another server holds the lock.'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = open(path + ".lock", "a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None, None
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    sock.listen(128)
    return sock, lock


def main():
    path = socket_path()
    sock, lock = bind(path)
    if sock is None or lock is None:
        return 0
    HD.Health(enabled=True).clear("hook-server")
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    warm = Warm()
    warm.load()
    report_r, report_w = os.pipe()
    os.set_blocking(report_r, False)
    pending = b""
    stamp = watched()
    reexec = False
    try:
        while True:
            ready, _, _ = select.select([sock, report_r], [], [], IDLE_SECS)
            if not ready:
                break
            if report_r in ready:
                pending = drain_reports(report_r, warm, pending)
                warm.save()
            if sock not in ready:
                continue
            conn, _ = sock.accept()
            if watched() != stamp:
                try:
                    conn.sendall(frame(b"r", b""))
                except OSError:
                    pass
                conn.close()
                reexec = True
                break
            before = warm.baseline()
            try:
                pid = os.fork()
            except OSError:
                try:
                    conn.sendall(frame(b"r", b""))
                except OSError:
                    pass
                conn.close()
                continue
            if pid == 0:
                sock.close()
                os.close(report_r)
                serve_call(conn, report_w, before)
            conn.close()
    finally:
        warm.save(force=True)
        try:
            os.unlink(path)
        except OSError:
            pass
        sock.close()
        lock.close()
    if reexec:
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        HD.Health(enabled=True).fail("hook-server", "%s: %s" % (type(exc).__name__, exc))
        sys.exit(1)
