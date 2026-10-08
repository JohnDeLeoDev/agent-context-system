#!/usr/bin/env python3
"lspd - one warm language server per (server, workspace), shared by every client.\n\nWhy this exists. Several things each want a language server for the same workspace:\nthe SessionStart canary, the MCP bridge, Claude Code's built-in LSP tool, and every\nsubagent. Separate servers starve each other: Kotlin dies on a single-writer RocksDB\nLOCK; TypeScript runs several tsserver chains until findReferences times out while\nhover still answers; C# races two MSBuildWorkspaces on one obj/. So there is one\nlanguage server per workspace and everything attaches to it.\nObservations guarded: #219, #226, #228, #233, #235, #243.\n\nDesign reference: gopls -remote=auto. Its state split is the one copied here:\n    per-client -- open documents, request-id namespace, cancellation\n    shared     -- the parsed/analyzed cache and the on-disk index\nGet that wrong in one direction and one agent's unsaved edits leak into another agent's\nview of a file; wrong in the other and you pay N times to rebuild the index, which is\nthe whole cost being avoided.\n\nWhy not lspmux/ra-multiplex. It documents that it drops server-initiated requests it\ncannot attribute to a client. This setup needs `workspace/configuration` (Roslyn aborts\non a null answer) and `$/progress` (the signal that tells a cold index from an absent\nsymbol). So the lifecycle server-initiated requests are answered here with the reply\neach server's contract demands, anything else is routed to a client, and progress is\ntracked.\n\nTwo modes:\n    --attach   a tiny stateless byte pump: connect-or-spawn, then splice stdin<->socket.\n               This is what a bridge or a probe execs in place of the real server.\n    --daemon   the long-lived multiplexer. Spawned automatically by --attach; never\n               launched by hand in normal operation.\n\nUsage:\n    lspd.py --attach --key kotlin-lsp --workspace /path -- kotlin-lsp --stdio\n    lspd.py --status [--key K]                 one key's rows and exit code only\n    lspd.py --resync [--key K] [paths...]      re-seat documents whose disk moved\n    lspd.py --restart --key K [--workspace W]  replace the language server child\n    lspd.py --upgrade --key K [--workspace W]  retire the daemon so it adopts new code"
import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.path.expanduser("~")
RUN = os.path.join(hp.state_dir(HOME), "lsp", "run")
LOGS = os.path.join(hp.state_dir(HOME), "lsp", "logs")











CODE_PATH = os.path.abspath(__file__)
















LSPD_PROTOCOL = 10


def code_fingerprint(path=CODE_PATH):
    '(sha256-prefix, mtime, size) of an lspd.py, or None when it cannot be read.\n\n    Content-hashed, deliberately. lspd.py reaches a machine\n    by materialization, which rewrites the file whether or not its bytes changed, so\n    mtime alone reports a stale daemon on every SessionStart and the report becomes\n    noise people learn to skim. mtime and size ride along for the log only.'
    try:
        with open(path, "rb") as fh:
            body = fh.read()
        st = os.stat(path)
    except OSError:
        return None
    return {"sha": hashlib.sha256(body).hexdigest()[:12], "protocol": LSPD_PROTOCOL,
            "mtime": int(st.st_mtime), "size": st.st_size, "path": path}






CODE_AT_START = code_fingerprint()





IDLE_EXIT_SECONDS = float(os.environ.get("LSPD_IDLE_EXIT", 3600))




WARM_WAIT_SECONDS = float(os.environ.get("LSPD_WARM_WAIT", 240))


SPAWN_WAIT_SECONDS = float(os.environ.get("LSPD_SPAWN_WAIT", 90))




GIT_POLL_SECONDS = float(os.environ.get("LSPD_GIT_POLL", 3))


TRACE = bool(os.environ.get("LSPD_TRACE"))


TERM_GRACE = 4.0










REPLAY_PAUSE = float(os.environ.get("LSPD_REPLAY_PAUSE", 0))
MAX_RESTARTS = int(os.environ.get("LSPD_MAX_RESTARTS", 3))
RESTART_WINDOW = float(os.environ.get("LSPD_RESTART_WINDOW", 900))


CANARY_POLL_SECONDS = float(os.environ.get("LSPD_CANARY_POLL", 300))
CANARY_MIN_GAP = float(os.environ.get("LSPD_CANARY_MIN_GAP", 30))
CANARY_STRIKES = int(os.environ.get("LSPD_CANARY_STRIKES", 2))
CANARY_TIMEOUT = float(os.environ.get("LSPD_CANARY_TIMEOUT", 20))
CANARY_COOLDOWN = float(os.environ.get("LSPD_CANARY_COOLDOWN", 600))
CANARY_CANDIDATES = int(os.environ.get("LSPD_CANARY_CANDIDATES", 5))
CANARY_ON = os.environ.get("LSPD_CANARY", "1") not in ("0", "no", "off", "")


CANARY_KINDS = {5, 6, 11, 12, 23}


NO_ANSWER = object()


NAME_SEARCH_LINES = 12




NAV_METHODS = {
    "workspace/symbol",
    "textDocument/definition",
    "textDocument/declaration",
    "textDocument/typeDefinition",
    "textDocument/implementation",
    "textDocument/references",
    "textDocument/documentSymbol",
    "textDocument/prepareCallHierarchy",
    "callHierarchy/incomingCalls",
    "callHierarchy/outgoingCalls",
}




INDEXING_RE = re.compile(
    r"index|import|load|analyz|build|restor|prepar|discover|scan", re.I)







SEED = {
    "tsgo":                       (("*.ts", "*.tsx"), "typescript"),
    "typescript-language-server": (("*.ts", "*.tsx"), "typescript"),
    "kotlin-lsp":                 (("*.kt",), "kotlin"),
    "csharp-ls":                  (("*.cs",), "csharp"),
    "sourcekit-lsp":              (("*.swift",), "swift"),
    "basedpyright":               (("*.py",), "python"),
    "rust-analyzer":              (("*.rs",), "rust"),
}


def seed_spec(key):
    '(patterns, languageId) to seed a server key with, or None.\n\n    LSPD_SEED ("*.ts,*.tsx:typescript") overrides the table, so a test can give a mock\n    server a seed spec. Unset, the table decides.'
    override = os.environ.get("LSPD_SEED", "")
    if ":" in override:
        pats, langid = override.rsplit(":", 1)
        return tuple(p for p in pats.split(",") if p), langid
    return SEED.get(key)
SEED_SKIP = {"node_modules", ".git", "build", "dist", ".next", "obj", "bin",
             ".build", "DerivedData", hp.CLAUDE_DIRNAME, hp.AGENTS_DIRNAME, "Pods", "vendor"}





SYNC_THROTTLE = float(os.environ.get("LSPD_SYNC_THROTTLE", 0.5))
SYNC_SKIP_DIRS = {".git", "node_modules", "dist", "build", "out", "bin", ".idea", ".vscode",
                  ".cache", "coverage", "target", "vendor"} | SEED_SKIP
SYNC_SKIP_EXTS = {".swp", ".swo", ".tmp", ".temp", ".bak", ".log", ".o", ".so", ".dylib",
                  ".dll", ".a", ".exe", ".lock"}










DOC_EXTS = {
    "tsgo":                       {".ts", ".tsx", ".mts", ".cts",
                                   ".js", ".jsx", ".mjs", ".cjs", ".json"},
    "typescript-language-server": {".ts", ".tsx", ".mts", ".cts",
                                   ".js", ".jsx", ".mjs", ".cjs", ".json"},
    "kotlin-lsp":                 {".kt", ".kts", ".java"},
    "csharp-ls":                  {".cs", ".csx", ".razor", ".cshtml", ".vb"},
    "sourcekit-lsp":              {".swift", ".h", ".m", ".mm", ".c", ".cc",
                                   ".cpp", ".hpp"},
    "basedpyright":               {".py", ".pyi"},
    "rust-analyzer":              {".rs"},
}



DOC_LANG = {".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
            ".tsx": "typescriptreact", ".js": "javascript", ".mjs": "javascript",
            ".cjs": "javascript", ".jsx": "javascriptreact", ".json": "json",
            ".kt": "kotlin", ".kts": "kotlin", ".java": "java",
            ".cs": "csharp", ".csx": "csharp", ".vb": "vb",
            ".swift": "swift", ".c": "c", ".h": "c", ".m": "objective-c",
            ".mm": "objective-cpp", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
            ".py": "python", ".pyi": "python", ".rs": "rust"}


def lang_id(path):
    return DOC_LANG.get(os.path.splitext(path)[1].lower(), "plaintext")








SCRATCH_CWD = {"csharp-ls"}









RUST_ANALYZER_CONFIG = {"cargo": {"targetDir": True},
                        "workspace": {"symbol": {"search": {"kind": "all_symbols"}}}}



MANIFEST_WATCH_KEYS = {"rust-analyzer"}


def cargo_manifests(root):
    'The project manifests rust-analyzer would discover for `root`, as a frozenset.\n\n    Mirrors rust-analyzer\'s ProjectManifest::discover, which runs once, when the server\n    starts and no linkedProjects is configured. A rust-project.json, then a\n    .rust-project.json, then a Cargo.toml in `root` or any directory above it wins\n    outright and is the whole answer. Only when none exists does it take the Cargo.toml of\n    each direct child of `root`, one level and no deeper, with no exception for hidden\n    directories or target/. Following the rule exactly matters in both directions: a\n    looser set would restart the server over a crate it never loads, and a stricter one\n    would miss the move this exists to notice.\n\n    Cheap on purpose, since it runs every poll: a stat per ancestor and one listdir of\n    the root plus a stat per child. An unreadable root reads as None, not as "no\n    manifests", so a transient error is never mistaken for every crate vanishing.'
    for name in ("rust-project.json", ".rust-project.json", "Cargo.toml"):
        d = root
        while True:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return frozenset([p])
            up = os.path.dirname(d)
            if up == d:
                break
            d = up
    try:
        names = os.listdir(root)
    except OSError:
        return None
    found = set()
    for n in names:
        p = os.path.join(root, n, "Cargo.toml")
        if os.path.isfile(p):
            found.add(p)
    return frozenset(found)


def strip_nulls(obj):
    'Recursively drop null-valued keys.\n\n    Needed because strict servers reject what lenient ones ignore. isaacphi/mcp-language-server\n    sends `capabilities.textDocument.semanticTokens.requests.range: null`; tsserver shrugs,\n    but TypeScript 7\'s Go server refuses the whole initialize:\n\n        InvalidParams: json: cannot unmarshal into Go lsproto.ClientSemanticTokensRequestOptions\n        within "/capabilities/textDocument/semanticTokens/requests/range":\n        null value is not allowed for field "range" (code: -32602)\n\n    which the harness then surfaces as a bare dead MCP server. The same bridge also sends\n    `workDoneToken: null` on every workspace/symbol, which tsgo rejects identically. In LSP\n    a capability or token field is an object, a boolean or a string; an explicit null\n    carries no meaning that absence does not, so removing it is lossless and fixes every\n    strict server at once rather than one at a time.'
    if isinstance(obj, dict):
        return {k: strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [strip_nulls(v) for v in obj]
    return obj


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM   
    return True


def log(where, msg):
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(os.path.join(LOGS, where + ".log"), "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def key_digest(key, workspace):
    'One daemon per (server, workspace). Hashed because these live in a flat directory\n    and a worktree path is long enough to blow past NAME_MAX on its own.'
    d = hashlib.sha1(os.path.realpath(workspace).encode()).hexdigest()[:16]
    return "%s-%s" % (key, d)


def sock_path(key, workspace):
    os.makedirs(RUN, exist_ok=True)
    
    return os.path.join(RUN, key_digest(key, workspace) + ".sock")






def read_message(fh):
    'One LSP message off a buffered binary reader. None at EOF or on a malformed frame.'
    length = None
    while True:
        line = fh.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if b":" in line:
            k, v = line.split(b":", 1)
            if k.strip().lower() == b"content-length":
                try:
                    length = int(v.strip())
                except ValueError:
                    return None
    if not length:
        return None
    buf = b""
    while len(buf) < length:
        chunk = fh.read(length - len(buf))
        if not chunk:
            return None
        buf += chunk
    try:
        return json.loads(buf)
    except ValueError:
        return None


def frame(obj):
    raw = json.dumps(obj).encode()
    return b"Content-Length: %d\r\n\r\n" % len(raw) + raw






class Client:
    def __init__(self, cid, conn):
        self.cid = cid
        self.conn = conn
        self.wfh = conn.makefile("wb")
        self.rfh = conn.makefile("rb")
        self.lock = threading.Lock()
        self.alive = True

    def send(self, obj):
        with self.lock:
            if not self.alive:
                return
            try:
                self.wfh.write(frame(obj))
                self.wfh.flush()
            except (OSError, ValueError):
                self.alive = False

    def close(self):
        self.alive = False
        for f in (self.rfh, self.wfh):
            try:
                f.close()
            except OSError:
                pass
        try:
            self.conn.close()
        except OSError:
            pass


class Daemon:
    def __init__(self, key, workspace, argv):
        self.key = key
        self.workspace = workspace
        self.argv = argv
        self.tag = key_digest(key, workspace)

        self.lock = threading.RLock()
        self.clients = {}
        self.next_cid = 1
        self.next_gid = 1

        
        
        self.pending = {}
        
        self.server_reqs = {}

        self.init_result = None
        self.init_done = threading.Event()
        self.init_inflight = False
        self.initialized_sent = False
        
        
        self.init_params = None
        self.reinit_done = threading.Event()
        self.restarts = []

        
        
        
        self.doc_refs = {}
        
        
        self.canary_opened = set()
        
        
        
        self.doc_open_sent = set()
        
        self.sync_lock = threading.Lock()
        self.sync_seen = None
        self.sync_at = 0.0
        
        
        
        
        self.doc_text = {}
        
        
        self.docs_refused = set()

        self.progress = {}          
        self.warm = threading.Event()
        self.warm.set()             

        
        
        
        self.git_state = None
        
        
        self.manifests = None
        
        
        self.manifest_deferred = None

        self.proc = None
        self.started_at = int(time.time())
        self.dead = threading.Event()
        
        
        self.down = threading.Event()
        self.revive_lock = threading.Lock()
        self.last_empty = time.time()

        
        self.awaiting = {}
        
        
        self.canary_lock = threading.Lock()
        self.canary_kick = threading.Event()
        self.canary = {"state": "uncalibrated", "uri": None, "position": None,
                       "symbol": None, "baseline": 0, "last": None, "last_at": 0.0,
                       "checked_at": 0.0, "strikes": 0, "restarts": 0,
                       "restarted_at": 0.0}

    

    def reap_orphans(self):
        "Kill parentless instances of this server before starting another.\n\n        An orphaned kotlin-lsp holds the single-writer RocksDB LOCK\n        forever, so every later start dies on `Resource temporarily unavailable` and\n        retrying never succeeds.\n\n        The test is deliberately narrow: argv[0]'s basename equals this server exactly,\n        ppid is 1, not a zombie. A language server speaks stdio to the process that\n        spawned it, so one with no parent is serving nobody and can only do harm. Anything\n        with a live parent is somebody's working server and is never touched."
        name = os.path.basename(self.argv[0])
        try:
            out = subprocess.run(["/bin/ps", "-Ao", "pid,ppid,state,command"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return                          
        for line in out.splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if ppid != 1 or parts[2].startswith("Z"):
                continue
            argv0 = parts[3].split()[0] if parts[3] else ""
            if os.path.basename(argv0) != name or pid == os.getpid():
                continue
            log(self.tag, "reaping orphaned %s (pid %d, parent gone) -- it would hold "
                          "the index lock against this start." % (name, pid))
            
            
            
            try:
                os.kill(pid, 15)
            except OSError:
                continue
            deadline = time.time() + TERM_GRACE
            while time.time() < deadline:
                if not alive(pid):
                    break
                time.sleep(0.25)
            if alive(pid):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

    def start_server(self):
        self.reap_orphans()
        exe = shutil.which(self.argv[0])
        if not exe:
            log(self.tag, "FATAL: %s not on PATH" % self.argv[0])
            return False
        errlog = open(os.path.join(LOGS, self.tag + ".server.log"), "ab", 0)
        cwd = self.workspace
        if self.key in SCRATCH_CWD:
            cwd = os.path.join(hp.state_dir(HOME), "lsp", "cwd", self.tag)
            os.makedirs(cwd, exist_ok=True)
        
        
        
        if self.key in MANIFEST_WATCH_KEYS:
            self.manifests = cargo_manifests(self.workspace)
        self.proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errlog, cwd=cwd, bufsize=0)
        log(self.tag, "started %s pid=%d cwd=%s" % (self.argv, self.proc.pid, cwd))
        threading.Thread(target=self.pump_server, args=(self.proc,),
                         daemon=True).start()
        threading.Thread(target=self.watch_child, daemon=True).start()
        return True

    def watch_child(self):
        'The fault isaacphi/mcp-language-server never handled: the child dies and the\n        parent keeps accepting calls, answering `broken pipe` for the rest of the session\n        while every health probe stays green.'
        proc = self.proc
        if proc is None:
            return
        proc.wait()
        if self.dead.is_set():
            return
        log(self.tag, "language server exited rc=%s" % proc.returncode)
        if not self.restart_child():
            self.hold_down()

    def hold_down(self):
        'The server keeps dying. Hold the socket open anyway.\n\n        Staying up costs nothing (an idle daemon still exits on its own timer). When the\n        cause of the crashes goes away, the next request revives the server under the\n        same clients.'
        log(self.tag, "restart budget exhausted -- server DOWN, holding the socket open")
        self.down.set()
        self.warm.set()          
        with self.lock:
            inflight = list(self.pending.items())
            self.pending.clear()
            self.server_reqs.clear()
        for _gid, (cid, orig_id, _m) in inflight:
            if isinstance(orig_id, str) and orig_id.startswith("__"):
                continue
            self.send_to(cid, {"jsonrpc": "2.0", "id": orig_id,
                               "error": {"code": -32099,
                                         "message": "language server is down; retry"}})

    def revive(self):
        'Try to bring a held-down server back. True if one is running.\n\n        Serialized on its own lock so a burst of requests produces one spawn attempt, not\n        one per request, and rate-limited by the same refilling restart budget -- a server\n        that is still broken is retried every RESTART_WINDOW, not every call.'
        with self.revive_lock:
            if not self.down.is_set():
                return True
            now = time.time()
            with self.lock:
                self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW]
                budget = len(self.restarts) < MAX_RESTARTS
            if not budget:
                return False
            log(self.tag, "reviving held-down server")
            self.down.clear()
            if not self.restart_child():
                self.down.set()
                return False
            
            
            
            if self.init_params is not None:
                end = time.time() + SPAWN_WAIT_SECONDS
                while time.time() < end and not self.initialized_sent:
                    time.sleep(0.05)
            return True

    def forced_restart(self, keep_budget=False):
        'The fault this exists for is the one nothing automatic can see: the server is\n        alive and answering, and answering wrongly. kotlin-lsp loses its stub serializers\n        on a warm-cache restore (Kotlin/kotlin-lsp#249, open), after which position\n        requests keep resolving correctly while every workspace-symbol request answers\n        "not found" -- byte-identical to a true negative. watch_child never fires because\n        nothing died, and no probe over the response text can separate a half-dead bridge\n        from a symbol that genuinely is not there. The only repair is a fresh process.\n\n        `keep_budget` is for a caller that is a watcher, not a person: manifest_check\n        must not wipe the record a crash loop is counted against, so its restart is\n        charged to the budget like any other.'
        if not keep_budget:
            with self.lock:
                
                
                
                self.restarts = []
        old = self.proc
        old_pid = old.pid if old is not None else None
        if self.down.is_set():
            log(self.tag, "restart requested while held down; reviving")
            ok = self.revive()
        elif old is None or old.poll() is not None:
            log(self.tag, "restart requested with no live server; starting one")
            ok = self.restart_child()
        else:
            log(self.tag, "restart requested by a client; replacing server pid=%s" % old_pid)
            ok = self.kill_child(old)
            if ok:
                
                
                
                
                end = time.time() + SPAWN_WAIT_SECONDS
                while time.time() < end:
                    cur = self.proc
                    if cur is not None and cur is not old and cur.poll() is None:
                        break
                    time.sleep(0.05)
                cur = self.proc
                ok = cur is not None and cur is not old and cur.poll() is None
        
        
        
        if ok and self.init_params is not None:
            end = time.time() + SPAWN_WAIT_SECONDS
            while time.time() < end and not self.initialized_sent:
                time.sleep(0.05)
        cur = self.proc
        return {"restarted": bool(ok), "was": old_pid,
                "pid": cur.pid if (ok and cur is not None) else None,
                "handshaked": bool(ok and (self.init_params is None
                                           or self.initialized_sent)),
                "warm": self.warm.is_set(), "workspace": self.workspace}

    def kill_child(self, proc):
        "End one server process. True once it is actually gone.\n\n        SIGTERM first for the same reason reap_orphans does it: JetBrains' server closes\n        RocksDB cleanly on it, and a SIGKILLed kotlin-lsp leaves the index LOCK for the\n        kernel to release -- which the replacement then fails to acquire, turning a repair\n        into the outage it was meant to fix."
        try:
            proc.terminate()
        except OSError:
            return proc.poll() is not None
        end = time.time() + TERM_GRACE
        while time.time() < end and proc.poll() is None:
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            end = time.time() + TERM_GRACE
            while time.time() < end and proc.poll() is None:
                time.sleep(0.1)
        return proc.poll() is not None

    def restart_child(self):
        'Bring a fresh server up under the existing clients. True if it came back.'
        now = time.time()
        with self.lock:
            self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW]
            if len(self.restarts) >= MAX_RESTARTS:
                return False
            self.restarts.append(now)
            
            
            
            inflight = list(self.pending.items())
            self.pending.clear()
            self.server_reqs.clear()
            
            reopen = sorted(self.doc_open_sent)
            self.doc_open_sent.clear()
            self.doc_refs.clear()
            self.doc_text.clear()
            self.initialized_sent = False
            self.progress.clear()
            params = self.init_params
        for _gid, (cid, orig_id, _m) in inflight:
            if isinstance(orig_id, str) and orig_id.startswith("__"):
                continue
            self.send_to(cid, {"jsonrpc": "2.0", "id": orig_id,
                               "error": {"code": -32099,
                                         "message": "language server restarted; retry"}})
        self.release_waiters("language server restarted")
        self.warm.clear()
        if not self.start_server():
            return False
        if params is None:
            
            self.warm.set()
            return True
        threading.Thread(target=self.reinitialize, args=(params, reopen),
                         daemon=True).start()
        return True

    def reinitialize(self, params, reopen):
        'Replay the handshake against the new server, then restore its view.\n\n        The clients cannot help here -- they already completed initialize once and will\n        never send it again -- so the daemon has to reproduce it from what it kept.'
        with self.lock:
            gid = self.next_gid
            self.next_gid += 1
            self.pending[gid] = ("__internal__", "__reinit__", "initialize")
        p = dict(params)
        p["processId"] = os.getpid()
        self.to_server({"jsonrpc": "2.0", "id": gid, "method": "initialize",
                        "params": p})
        if not self.reinit_done.wait(SPAWN_WAIT_SECONDS):
            log(self.tag, "restarted server never answered initialize")
            return
        self.reinit_done.clear()
        self.to_server({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        with self.lock:
            self.initialized_sent = True
        restored = 0
        skipped = 0
        for uri in reopen:
            
            
            
            if not self.doc_allowed(uri):
                continue
            path = uri[7:] if uri.startswith("file://") else None
            if not path or not os.path.exists(path):
                continue
            try:
                with open(path, "r", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if REPLAY_PAUSE:
                
                
                
                
                
                time.sleep(REPLAY_PAUSE)
            
            
            
            
            
            
            
            
            
            
            
            with self.lock:
                refs = self.doc_refs.setdefault(uri, set())
                already_open = bool(refs)
                
                
                refs.add(0)
                if not already_open:
                    self.doc_open_sent.add(uri)
                    self.doc_text[uri] = text
            if already_open:
                skipped += 1
                continue
            self.to_server({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                            "params": {"textDocument": {
                                "uri": uri, "languageId": lang_id(path),
                                "version": 1, "text": text}}})
            restored += 1
        log(self.tag, "restarted and re-handshaked; %d document(s) restored%s" %
            (restored, "" if not skipped
             else "; %d already reopened by a client" % skipped))
        self.seed_project()
        if "sourcekit" in self.key:
            threading.Thread(target=self.sourcekit_sync, daemon=True).start()
        elif self.key == "rust-analyzer":
            threading.Thread(target=self.status_backstop, daemon=True).start()

    def to_server(self, obj):
        
        
        
        
        if obj.get("method") and isinstance(obj.get("params"), (dict, list)):
            obj = dict(obj)
            obj["params"] = strip_nulls(obj["params"])
        try:
            stdin = self.proc.stdin if self.proc is not None else None
            if stdin is None:
                raise ValueError("no language server stdin")
            stdin.write(frame(obj))
            stdin.flush()
        except (OSError, ValueError, AttributeError):
            
            
            
            
            log(self.tag, "write to language server failed (%s); it is down"
                % obj.get("method") or "response")

    def pump_server(self, proc):
        'Read one server\'s stdout until it ends. Never shuts the daemon down.\n\n        Bound to the specific process it was started for, because a restart brings up a\n        new one with its own pump; and deliberately silent at EOF, because EOF here means\n        "this child died", which is watch_child\'s call to make. Calling self.shutdown()\n        here would close the client sockets before restart_child could run.'
        fh = proc.stdout
        while not self.dead.is_set():
            msg = read_message(fh)
            if msg is None:
                return
            try:
                self.from_server(msg)
            except Exception as exc:                     
                log(self.tag, "from_server error: %r" % (exc,))

    def from_server(self, msg):
        mid = msg.get("id")
        method = msg.get("method")

        
        if mid is not None and method is None:
            with self.lock:
                route = self.pending.pop(mid, None)
            if route is None:
                return
            cid, orig_id, req_method = route
            if orig_id == "__init__":
                self.finish_initialize(msg)
                return
            if orig_id == "__reinit__":
                self.reinit_done.set()
                return
            if orig_id == "__await__":
                
                with self.lock:
                    waiter = self.awaiting.pop(mid, None)
                if waiter is not None:
                    ev, box = waiter
                    box["result"] = msg.get("result")
                    box["error"] = msg.get("error")
                    ev.set()
                return
            if orig_id == "__sync__":
                
                
                self.warm.set()
                log(self.tag, "sourcekit: synchronize returned, index WARM")
                return
            out = dict(msg)
            out["id"] = orig_id
            if "result" in out:
                out["result"] = self.normalize_result(req_method, out["result"])
            if TRACE:
                log(self.tag + ".trace", "RESP -> client %s: %s"
                    % (cid, json.dumps(out)[:1200]))
            self.send_to(cid, out)
            
            
            
            if (req_method == "textDocument/references"
                    and isinstance(out.get("result"), list) and not out["result"]):
                self.canary_kick.set()
            return

        
        
        
        
        if mid is not None and method is not None:
            handled, result = self.answer_server_request(method, msg.get("params"))
            if handled:
                self.to_server({"jsonrpc": "2.0", "id": mid, "result": result})
                return
            cid = self.primary_cid()
            if cid is None:
                self.to_server({"jsonrpc": "2.0", "id": mid, "result": None})
                return
            with self.lock:
                self.server_reqs[mid] = cid
            self.send_to(cid, msg)
            return

        
        if method == "$/progress":
            self.track_progress(msg.get("params") or {})
        elif method == "experimental/serverStatus":
            
            self.track_server_status(msg.get("params") or {})
            return
        self.broadcast(msg)

    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    SELF_ANSWERED = {
        "client/registerCapability": None,
        "client/unregisterCapability": None,
        "window/workDoneProgress/create": None,
        "workspace/semanticTokens/refresh": None,
        "workspace/diagnostic/refresh": None,
        "workspace/inlayHint/refresh": None,
        "workspace/codeLens/refresh": None,
        "workspace/foldingRange/refresh": None,
    }

    
    
    
    
    
    
    
    NAV_SCHEMES_OK = ("file://",)

    def normalize_result(self, method, result):
        if method not in NAV_METHODS or not isinstance(result, list):
            return result
        out = []
        dropped = 0
        for item in result:
            if not isinstance(item, dict):
                out.append(item)
                continue
            loc = item.get("location")
            uri = None
            if isinstance(loc, dict):
                uri = loc.get("uri")
            elif isinstance(item.get("uri"), str):
                uri = item["uri"]
            elif isinstance(item.get("targetUri"), str):
                uri = item["targetUri"]
            if isinstance(uri, str) and not uri.startswith(self.NAV_SCHEMES_OK):
                dropped += 1
                continue
            out.append(item)
        if dropped:
            log(self.tag, "normalize: dropped %d/%d non-file result(s) from %s"
                % (dropped, len(result), method))
        return out

    def answer_server_request(self, method, params):
        "(handled, result). handled=False means 'pass it to a real client'."
        if method == "workspace/configuration":
            
            
            items = (params or {}).get("items") or []
            return True, [self.config_for(i) for i in items] or [{}]
        if method in self.SELF_ANSWERED:
            return True, self.SELF_ANSWERED[method]
        return False, None

    def config_for(self, item):
        "One workspace/configuration item's answer: this daemon's settings for the\n        section a server asks about, else {} (use your defaults)."
        if (self.key == "rust-analyzer" and isinstance(item, dict)
                and item.get("section") == "rust-analyzer"):
            return RUST_ANALYZER_CONFIG
        return {}

    def rust_analyzer_init(self, params):
        "Ask rust-analyzer for its ready signal and hand it this daemon's settings.\n\n        experimental/serverStatus is sent only to a client that advertises\n        experimental.serverStatusNotification; `quiescent: true` then says every pending\n        workspace load, build-script run and index pass has finished. The clients here are\n        lspd's own bridge, whose initializationOptions are written for another server, so\n        they are replaced rather than merged."
        caps = params.setdefault("capabilities", {})
        caps.setdefault("experimental", {})["serverStatusNotification"] = True
        params["initializationOptions"] = RUST_ANALYZER_CONFIG

    def track_server_status(self, params):
        "rust-analyzer's authoritative gate. quiescent means loading and indexing are\n        done, so a negative answer from here on is real; not quiescent closes the gate."
        if params.get("quiescent"):
            if params.get("health") not in (None, "ok"):
                log(self.tag, "serverStatus %s: %s"
                    % (params.get("health"), params.get("message") or ""))
            if not self.warm.is_set():
                self.warm.set()
                log(self.tag, "serverStatus quiescent, index WARM")
        elif self.warm.is_set():
            self.warm.clear()
            log(self.tag, "serverStatus busy, index COLD")

    def track_progress(self, params):
        token = params.get("token")
        val = params.get("value") or {}
        kind = val.get("kind")
        if kind == "begin":
            title = "%s %s" % (val.get("title") or "", val.get("message") or "")
            if INDEXING_RE.search(title):
                with self.lock:
                    self.progress[token] = title.strip()
                    if self.warm.is_set():
                        self.warm.clear()
                        log(self.tag, "index COLD (%s)" % title.strip())
        elif kind == "end":
            with self.lock:
                
                
                if (self.progress.pop(token, None) is not None and not self.progress
                        and self.key != "rust-analyzer"):
                    self.warm.set()
                    log(self.tag, "index WARM")

    

    def primary_cid(self):
        with self.lock:
            for cid in sorted(self.clients):
                if self.clients[cid].alive:
                    return cid
        return None

    def send_to(self, cid, obj):
        with self.lock:
            c = self.clients.get(cid)
        if c:
            c.send(obj)

    def broadcast(self, obj):
        with self.lock:
            targets = list(self.clients.values())
        for c in targets:
            c.send(obj)

    def serve_client(self, conn):
        with self.lock:
            cid = self.next_cid
            self.next_cid += 1
            client = Client(cid, conn)
            self.clients[cid] = client
        log(self.tag, "client %d attached (%d total)" % (cid, len(self.clients)))
        try:
            while not self.dead.is_set():
                msg = read_message(client.rfh)
                if msg is None:
                    break
                try:
                    self.from_client(cid, msg)
                except Exception as exc:
                    log(self.tag, "from_client error: %r" % (exc,))
        finally:
            self.detach(cid)

    def detach(self, cid):
        with self.lock:
            client = self.clients.pop(cid, None)
            
            
            drop = [u for u, s in self.doc_refs.items() if cid in s]
            for uri in drop:
                self.doc_refs[uri].discard(cid)
                if not self.doc_refs[uri]:
                    del self.doc_refs[uri]
                    if uri in self.doc_open_sent:
                        self.doc_open_sent.discard(uri)
                        self.doc_text.pop(uri, None)
                        self.to_server({"jsonrpc": "2.0",
                                        "method": "textDocument/didClose",
                                        "params": {"textDocument": {"uri": uri}}})
            
            
            orphaned = [sid for sid, c in self.server_reqs.items() if c == cid]
            for sid in orphaned:
                del self.server_reqs[sid]
                self.to_server({"jsonrpc": "2.0", "id": sid, "result": None})
            remaining = len(self.clients)
            if remaining == 0:
                self.last_empty = time.time()
        if client:
            client.close()
        log(self.tag, "client %d detached (%d left)" % (cid, remaining))

    def from_client(self, cid, msg):
        method = msg.get("method")
        mid = msg.get("id")
        if TRACE:
            log(self.tag + ".trace", "REQ  <- client %s: %s" % (cid, json.dumps(msg)[:1200]))

        
        if mid is not None and method is None:
            with self.lock:
                known = self.server_reqs.pop(mid, None)
            if known is not None:
                self.to_server(msg)
            return

        
        
        if method == "initialize":
            self.handle_initialize(cid, mid, msg)
            return

        if method == "initialized":
            with self.lock:
                first = not self.initialized_sent
                self.initialized_sent = True
            if first:
                self.to_server(msg)
            return

        
        
        
        if method == "$/lspd/resync":
            paths = (msg.get("params") or {}).get("paths")
            n = self.resync(paths if paths else None)
            if mid is not None:
                self.send_to(cid, {"jsonrpc": "2.0", "id": mid,
                                   "result": {"reseated": n, "workspace": self.workspace}})
            return

        
        
        
        
        if method == "$/lspd/restart":
            res = self.forced_restart()
            if mid is not None:
                self.send_to(cid, {"jsonrpc": "2.0", "id": mid, "result": res})
            return

        
        
        if method == "$/lspd/health":
            if mid is not None:
                self.send_to(cid, {"jsonrpc": "2.0", "id": mid,
                                   "result": self.health(asking_cid=cid)})
            return

        
        
        
        
        
        
        
        
        
        
        
        
        
        if method == "$/lspd/shutdown":
            with self.lock:
                others = len([c for c in self.clients if c != cid])
            res = {"key": self.key, "workspace": self.workspace,
                   "daemon_pid": os.getpid(), "code": CODE_AT_START,
                   "clients": others}
            if mid is not None:
                self.send_to(cid, {"jsonrpc": "2.0", "id": mid, "result": res})
            log(self.tag, "shutdown requested -- retiring this daemon (%d other client(s) "
                          "attached); the next attach respawns from %s"
                % (others, CODE_PATH))
            threading.Timer(0.3, self.shutdown).start()
            return

        
        
        
        if method == "shutdown":
            if mid is not None:
                self.send_to(cid, {"jsonrpc": "2.0", "id": mid, "result": None})
            return
        if method == "exit":
            return

        
        
        
        
        if method.startswith("textDocument/did"):
            uri = (((msg.get("params") or {}).get("textDocument")) or {}).get("uri")
            if not self.doc_allowed(uri):
                with self.lock:
                    first = uri not in self.docs_refused
                    self.docs_refused.add(uri)
                if first:
                    log(self.tag, "refused %s: %s is not a document this server parses"
                        % (method, uri))
                return

        if method in ("textDocument/didOpen", "textDocument/didClose"):
            self.handle_doc(cid, method, msg)
            return
        if method == "textDocument/didChange":
            self.note_change(msg)            

        
        
        
        
        if self.down.is_set() and not self.revive():
            if mid is not None:
                self.send_to(cid, {"jsonrpc": "2.0", "id": mid,
                                   "error": {"code": -32099,
                                             "message": "language server is down; retry"}})
            return

        if mid is None:
            self.to_server(msg)
            return

        
        self.sync_scan()

        
        if method in NAV_METHODS and not self.warm.is_set():
            threading.Thread(target=self.gated_forward, args=(cid, msg), daemon=True).start()
            return
        self.forward_request(cid, msg)

    def forward_request(self, cid, msg):
        with self.lock:
            gid = self.next_gid
            self.next_gid += 1
            self.pending[gid] = (cid, msg.get("id"), msg.get("method"))
        out = dict(msg)
        out["id"] = gid
        self.to_server(out)

    def gated_forward(self, cid, msg):
        "Hold a navigation request until the index is warm.\n\n        This turns the worst failure mode -- a confident 'not found' for a symbol that\n        exists -- into a slower correct answer. Bounded, because a hang is its own kind\n        of wrong: past WARM_WAIT the request goes through regardless."
        waited = self.warm.wait(WARM_WAIT_SECONDS)
        if not waited:
            log(self.tag, "warm gate expired after %ss; forwarding %s anyway"
                % (WARM_WAIT_SECONDS, msg.get("method")))
        self.forward_request(cid, msg)

    def handle_initialize(self, cid, mid, msg):
        
        
        if self.down.is_set():
            self.revive()
        with self.lock:
            if self.init_result is not None:
                cached = self.init_result
            elif self.init_inflight:
                cached = None
            else:
                self.init_inflight = True
                self.pending[self.next_gid] = (cid, "__init__", "initialize")
                gid = self.next_gid
                self.next_gid += 1
                out = dict(msg)
                out["id"] = gid
                if isinstance(out.get("params"), dict):
                    params = strip_nulls(out["params"])
                    out["params"] = params
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    if isinstance(params, dict):
                        params["processId"] = os.getpid()
                        if self.key == "rust-analyzer":
                            self.rust_analyzer_init(params)
                    self.init_params = params
                self.to_server(out)
                self._init_waiters = getattr(self, "_init_waiters", [])
                self._init_waiters.append((cid, mid))
                return
        if cached is not None:
            self.send_to(cid, {"jsonrpc": "2.0", "id": mid, "result": cached})
            return
        
        with self.lock:
            self._init_waiters = getattr(self, "_init_waiters", [])
            self._init_waiters.append((cid, mid))

    def finish_initialize(self, msg):
        with self.lock:
            self.init_result = msg.get("result")
            self.init_inflight = False
            waiters = getattr(self, "_init_waiters", [])
            self._init_waiters = []
        self.init_done.set()
        for cid, mid in waiters:
            out = {"jsonrpc": "2.0", "id": mid}
            if "error" in msg:
                out["error"] = msg["error"]
            else:
                out["result"] = self.init_result
            self.send_to(cid, out)
        log(self.tag, "initialize complete, %d client(s) released" % len(waiters))
        threading.Thread(target=self.seed_project, daemon=True).start()
        
        threading.Thread(target=self.sync_scan, daemon=True).start()
        
        
        if "sourcekit" in self.key:
            threading.Thread(target=self.sourcekit_sync, daemon=True).start()
        elif self.key == "rust-analyzer":
            threading.Thread(target=self.status_backstop, daemon=True).start()

    def find_seed(self, patterns, skip=()):
        'First real source file matching any pattern, breadth-first, skipping build\n        and dependency trees and any path in `skip`. Bounded so a huge repo cannot stall\n        startup.'
        import fnmatch
        best = None
        seen = 0
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in SEED_SKIP and not d.startswith(".")]
            for name in sorted(files):
                for pat in patterns:
                    if fnmatch.fnmatch(name, pat):
                        p = os.path.join(root, name)
                        if p in skip:
                            continue
                        
                        if os.sep + "src" + os.sep in p:
                            return p
                        best = best or p
            seen += 1
            if seen > 4000:
                break
        return best

    def open_held(self, path, langid):
        "Open a document on the daemon's own behalf and hold it for the daemon's life.\n\n        The seed uses it, and so does the canary when no open document has a usable\n        symbol. Returns (uri, already_open); already_open means a client had the document\n        open, so no didOpen was sent. Raises OSError when the file cannot be read."
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
        uri = "file://" + path
        with self.lock:
            
            
            refs = self.doc_refs.setdefault(uri, set())
            
            
            
            already_open = uri in self.doc_open_sent
            refs.add(0)
            if not already_open:
                self.doc_open_sent.add(uri)
                self.doc_text[uri] = text
            
            
            
            
            
            
            
            
            if not already_open:
                self.to_server({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                                "params": {"textDocument": {
                                    "uri": uri, "languageId": langid,
                                    "version": 1, "text": text}}})
        return uri, already_open

    def seed_project(self):
        'Force a lazy server to actually load the project before we answer anything.'
        spec = seed_spec(self.key)
        if not spec:
            
            
            
            
            self.warm.set()
            return
        patterns, langid = spec
        self.warm.clear()

        
        
        
        
        if self.key == "csharp-ls":
            try:
                slns = sorted(
                    os.path.join(self.workspace, f) for f in os.listdir(self.workspace)
                    if f.endswith(".sln") or f.endswith(".slnx"))
            except OSError:
                slns = []
            if slns:
                self.to_server({"jsonrpc": "2.0", "method": "solution/open",
                                "params": {"solution": "file://" + slns[0]}})
                log(self.tag, "sent solution/open for %s" % slns[0])
            else:
                log(self.tag, "no .sln at %s -- Roslyn will resolve nothing"
                    % self.workspace)

        try:
            path = self.find_seed(patterns)
            if not path:
                log(self.tag, "seed: no %s file found under %s" % (patterns, self.workspace))
                return
            already_open = self.open_held(path, langid)[1]
            if already_open:
                log(self.tag, "seed %s already open by a client; not re-opening" % path)
            else:
                log(self.tag, "seeded project with %s" % path)
            
            
            time.sleep(float(os.environ.get("LSPD_SEED_SETTLE", 2.5)))
        except OSError as exc:
            log(self.tag, "seed failed: %r" % (exc,))
        finally:
            with self.lock:
                cold = bool(self.progress)
            
            
            
            
            
            if (not cold and "sourcekit" not in self.key
                    and self.key != "rust-analyzer"):
                self.warm.set()

    def sourcekit_sync(self):
        "sourcekit/workspace/synchronize {index:true} blocks until background indexing\n        has finished. Upstream documents it as 'intended for automated\n        environments'. It is the only authoritative cold-vs-absent\n        answer available on any of the languages here."
        self.warm.clear()
        with self.lock:
            gid = self.next_gid
            self.next_gid += 1
            self.pending[gid] = ("__internal__", "__sync__",
                                 "sourcekit/workspace/synchronize")
        log(self.tag, "sourcekit: awaiting workspace/synchronize")
        self.to_server({"jsonrpc": "2.0", "id": gid,
                        "method": "sourcekit/workspace/synchronize",
                        "params": {"index": True}})
        
        
        time.sleep(WARM_WAIT_SECONDS)
        if not self.warm.is_set():
            self.warm.set()
            log(self.tag, "sourcekit: synchronize gate released by timeout")

    def status_backstop(self):
        "Release rust-analyzer's gate if serverStatus never reports quiescent, so a\n        server that never says so cannot hold navigation forever."
        time.sleep(WARM_WAIT_SECONDS)
        if not self.warm.is_set():
            self.warm.set()
            log(self.tag, "serverStatus gate released by timeout")

    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    

    def release_waiters(self, reason):
        'Fail the daemon\'s own in-flight questions.\n\n        The loops that fail client requests skip these deliberately -- they carry a "__"\n        id and have no client to answer -- so without this the canary blocks for its full\n        timeout every time the server it was questioning is replaced underneath it.'
        with self.lock:
            waiters = list(self.awaiting.values())
            self.awaiting.clear()
        for ev, box in waiters:
            box["error"] = {"code": -32099, "message": reason}
            ev.set()

    def canary_targets(self):
        'Documents the server has actually loaded, to ask questions about: the one the\n        last calibration used first, then every other open document in sorted order.'
        with self.canary_lock:
            want = self.canary["uri"]
        with self.lock:
            uris = sorted(self.doc_open_sent)
        if want in uris:
            uris.remove(want)
            uris.insert(0, want)
        return uris

    def symbol_positions(self, syms, out=None):
        'Candidate (name, position) probes from a documentSymbol result.\n\n        Both shapes the LSP permits are handled: DocumentSymbol (nested, selectionRange)\n        and SymbolInformation (flat, location.range). Declarations sort first -- a class\n        or a function is a stabler probe than a local whose range moves on every edit.'
        top = out is None
        out = [] if top else out
        if isinstance(syms, list):
            for s in syms:
                if not isinstance(s, dict):
                    continue
                rng = s.get("selectionRange") or s.get("range")
                if rng is None and isinstance(s.get("location"), dict):
                    rng = s["location"].get("range")
                start = (rng or {}).get("start")
                name = s.get("name")
                if isinstance(start, dict) and isinstance(name, str):
                    out.append((0 if s.get("kind") in CANARY_KINDS else 1, name,
                                {"line": start.get("line", 0),
                                 "character": start.get("character", 0)}))
                self.symbol_positions(s.get("children"), out)
        if not top:
            return out
        out.sort(key=lambda t: t[0])
        return [(name, pos) for _rank, name, pos in out]

    def ask_server(self, method, params, timeout, missing=None):
        "Ask the server something on the daemon's own behalf, and wait for the answer.\n\n        Returns `missing` when there is no answer (timeout, error, server down). Pass\n        NO_ANSWER to tell that apart from a null result.\n\n        Routed through the same pending table client requests use, so the reply arrives\n        in from_server like any other and no second reader of the pipe is needed."
        proc = self.proc
        if self.down.is_set() or proc is None or proc.poll() is not None:
            return missing
        ev = threading.Event()
        box = {}
        with self.lock:
            gid = self.next_gid
            self.next_gid += 1
            self.pending[gid] = ("__internal__", "__await__", method)
            self.awaiting[gid] = (ev, box)
        self.to_server({"jsonrpc": "2.0", "id": gid, "method": method, "params": params})
        if not ev.wait(timeout):
            with self.lock:
                self.pending.pop(gid, None)
                self.awaiting.pop(gid, None)
            return missing
        if box.get("error"):
            return missing
        return box.get("result")

    def canary_count(self, uri, pos, null_is_zero=False):
        "References at a position, declaration included. None means no answer at all,\n        which is watch_child's department and not this one's."
        res = self.ask_server("textDocument/references",
                              {"textDocument": {"uri": uri}, "position": pos,
                               "context": {"includeDeclaration": True}},
                              CANARY_TIMEOUT, missing=NO_ANSWER)
        if isinstance(res, list):
            return len(res)
        
        
        
        
        if null_is_zero and res is not NO_ANSWER:
            return 0
        return None

    def canary_calibrate(self):
        "Find a symbol this server answers for, and remember what it answered.\n\n        The baseline is measured, never configured. A per-project list of known-good\n        symbols would have to be kept in step with the code by hand, and a check that\n        needs curation is a check that rots into a false alarm nobody reads.\n\n        A document with no usable symbol (a docstring-only __init__.py, say) does not\n        end the search. Every open document is tried. When none answers, up to CANARY_CANDIDATES more seed\n        candidates are opened and held, counted over the daemon's life so later polls\n        never open more."
        state = None
        tried = set()
        for uri in self.canary_targets():
            got = self.canary_try(uri)
            if got is None:
                return False
            if got == "calibrated":
                return True
            tried.add(uri[len("file://"):] if uri.startswith("file://") else uri)
            state = got if state in (None, "no-symbols") else state
        spec = seed_spec(self.key)
        while spec and len(self.canary_opened) < CANARY_CANDIDATES:
            path = self.find_seed(spec[0], skip=tried)
            if not path:
                break
            tried.add(path)
            try:
                uri, _already_open = self.open_held(path, spec[1])
            except OSError:
                continue
            self.canary_opened.add(uri)
            log(self.tag, "canary: no open document has a usable symbol; opened %s" % path)
            got = self.canary_try(uri)
            if got is None:
                return False
            if got == "calibrated":
                return True
            state = got if state in (None, "no-symbols") else state
        if state is None:
            return False
        with self.canary_lock:
            self.canary["state"] = state
            self.canary["uri"] = None
        return False

    def name_positions(self, uri, cands):
        "Move each probe onto its symbol's name.\n\n        A flat SymbolInformation range starts where the declaration starts, on `def` or\n        `class`, and basedpyright answers references there with null while it answers on\n        the name. A DocumentSymbol selectionRange is already on the\n        name, so the search leaves it where it is."
        from urllib.parse import unquote
        
        
        with self.lock:
            text = self.doc_text.get(uri)
        if text is None:
            path = unquote(uri[len("file://"):]) if uri.startswith("file://") else uri
            try:
                with open(path, "r", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                return cands
        
        
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

        
        
        def index_of(row, units):
            n = 0
            for i, ch in enumerate(row):
                if n >= units:
                    return i
                n += 2 if ord(ch) > 0xFFFF else 1
            return len(row)

        def units_of(row, index):
            return sum(2 if ord(ch) > 0xFFFF else 1 for ch in row[:index])

        out = []
        for name, pos in cands:
            line, start = pos.get("line", 0), pos.get("character", 0)
            moved, on_decorator = pos, None
            
            
            
            for offset in range(NAME_SEARCH_LINES):
                if not 0 <= line + offset < len(lines):
                    break
                row = lines[line + offset]
                col = row.find(name, index_of(row, start) if offset == 0 else 0)
                if col < 0:
                    continue
                hit = {"line": line + offset, "character": units_of(row, col)}
                
                
                
                if row.lstrip().startswith("@"):
                    on_decorator = on_decorator or hit
                    continue
                moved = hit
                break
            if moved is pos and on_decorator is not None:
                moved = on_decorator
            out.append((name, moved))
        return out

    def canary_try(self, uri):
        'Calibrate off one document. Returns "calibrated", "no-usable-symbol",\n        "no-symbols", or None when the server gave no answer at all.'
        syms = self.ask_server("textDocument/documentSymbol",
                               {"textDocument": {"uri": uri}}, CANARY_TIMEOUT)
        cands = self.name_positions(uri, self.symbol_positions(syms)[:CANARY_CANDIDATES])
        for name, pos in cands:
            n = self.canary_count(uri, pos, null_is_zero=True)
            if n is None:
                return None
            if n > 0:
                with self.canary_lock:
                    self.canary.update(state="calibrated", uri=uri, position=pos,
                                       symbol=name, baseline=n, last=n,
                                       last_at=time.time(), strikes=0)
                log(self.tag, "canary: %s in %s answers %d reference(s); that is the "
                              "baseline" % (name, os.path.basename(uri), n))
                return "calibrated"
        return "no-usable-symbol" if cands else "no-symbols"

    def canary_check(self):
        now = time.time()
        with self.canary_lock:
            if now - self.canary["checked_at"] < CANARY_MIN_GAP:
                return
            self.canary["checked_at"] = now
            state = self.canary["state"]
            uri, pos = self.canary["uri"], self.canary["position"]
        if state != "calibrated":
            self.canary_calibrate()
            return
        n = self.canary_count(uri, pos)
        if n is None:
            return
        with self.canary_lock:
            self.canary["last"] = n
            self.canary["last_at"] = time.time()
            if n > 0:
                self.canary["strikes"] = 0
                self.canary["baseline"] = max(self.canary["baseline"], n)
                return
            self.canary["strikes"] += 1
            strikes, baseline = self.canary["strikes"], self.canary["baseline"]
            symbol = self.canary["symbol"]
            cooling = time.time() - self.canary["restarted_at"] < CANARY_COOLDOWN
        log(self.tag, "canary: %s answers 0 references against a baseline of %d "
                      "(strike %d of %d)" % (symbol, baseline, strikes, CANARY_STRIKES))
        if strikes < CANARY_STRIKES:
            return
        if cooling:
            log(self.tag, "canary: still empty, but this server was replaced less than "
                          "%ds ago -- not spinning on it" % CANARY_COOLDOWN)
            return
        log(self.tag, "canary: ANSWER QUALITY HAS COLLAPSED. The server is alive and "
                      "returning nothing; replacing it (audit policy).")
        res = self.forced_restart()
        with self.canary_lock:
            self.canary["restarts"] += 1
            self.canary["restarted_at"] = time.time()
            self.canary["strikes"] = 0
            
            
            self.canary["state"] = "recalibrating"
        log(self.tag, "canary: replacement %r" % (res,))

    def canary_watch(self):
        'Poll the assertion, and jump the queue when a client sees an empty answer.\n\n        The kick matters as much as the poll. The moment a session is handed an empty\n        references result is the moment "is that true?" is worth asking; waiting out the\n        poll interval means the session has already deleted something.'
        while not self.dead.is_set():
            self.canary_kick.wait(CANARY_POLL_SECONDS)
            self.canary_kick.clear()
            if self.dead.is_set():
                return
            if self.down.is_set() or not self.warm.is_set():
                continue
            try:
                self.canary_check()
            except Exception as exc:      
                log(self.tag, "canary error: %r" % (exc,))

    def health(self, asking_cid=None):
        'What this daemon knows about itself.\n\n        `clients` excludes whoever is asking. Every caller reaches this verb by opening\n        a connection, and that connection is a client like any other -- so an idle\n        daemon truthfully reported one attached client, and `--upgrade` refused to\n        retire a daemon nobody was using, naming its own socket as the session it was\n        protecting. The number\'s only consumer wants "sessions that lose their language\n        server if this daemon goes away", and a CLI command asking a question is not\n        one of them.'
        proc = self.proc
        with self.canary_lock:
            canary = dict(self.canary)
        with self.lock:
            clients = len([c for c in self.clients if c != asking_cid])
        return {"key": self.key, "workspace": self.workspace,
                "pid": proc.pid if (proc is not None and proc.poll() is None) else None,
                "warm": self.warm.is_set(), "down": self.down.is_set(),
                "clients": clients, "canary": canary,
                
                
                
                
                "code": CODE_AT_START, "daemon_pid": os.getpid(),
                "started_at": self.started_at}

    

    def git_head(self):
        '(HEAD sha, index mtime) for the workspace, or None when it is not a repo.\n\n        Both halves matter. HEAD moves on commit/checkout/rebase/fast-forward; the index\n        mtime moves on `git add`, and on the worktree bookkeeping that `git worktree\n        add/remove` performs. Either can change the files under us without a single\n        editor write.'
        try:
            sha = subprocess.run(
                ["git", "-C", self.workspace, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if sha.returncode != 0:
                return None
            head = sha.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        mtime = 0.0
        for rel in ("index",):
            try:
                mtime = max(mtime, os.path.getmtime(
                    os.path.join(self.workspace, ".git", rel)))
            except OSError:
                pass
        return (head, mtime)

    def changed_between(self, old_sha, new_sha):
        'Paths git says differ between two commits, as absolute paths.\n\n        Returns None when the range cannot be resolved -- an unrelated history, a sha that\n        has been gc\'d, a shallow clone. None means "I do not know what changed", which the\n        caller must treat as "assume everything open is suspect", never as "nothing changed".'
        if not old_sha or not new_sha or old_sha == new_sha:
            return []
        try:
            out = subprocess.run(
                ["git", "-C", self.workspace, "diff", "--name-only", old_sha, new_sha],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode != 0:
                return None
        except (OSError, subprocess.SubprocessError):
            return None
        return [os.path.join(self.workspace, line)
                for line in out.stdout.splitlines() if line]

    def resync(self, paths=None):
        "Tell the server the disk moved, and re-seat any document it holds in memory.\n\n        Two halves, and skipping either leaves the fault this exists to fix:\n\n        1. `workspace/didChangeWatchedFiles` is the notification LSP defines for exactly\n           this -- files changed by something that is not the editor. It is what pushes a\n           server to re-read its workspace symbol index for those paths.\n        2. A document the server has open is served from its in-memory buffer, which no\n           watched-files notification touches. That buffer is what went stale on a\n           fast-forward: hover answered for the symbol one line off, because the text the\n           server held was the pre-merge text. Only a didClose/didOpen pair with the\n           current bytes re-seats it.\n\n        The refcounts are deliberately left alone. `doc_refs` records which clients\n        believe a document is open, and none of them has changed its mind; only the\n        server's copy is being replaced. `doc_open_sent` is maintained so the\n        close-only-what-we-opened rule in handle_doc still holds -- a file that has been\n        deleted is closed and forgotten here, and a later client didClose then correctly\n        forwards nothing rather than asking the server to stop tracking what it no longer\n        has. Getting that backwards is what aborts Roslyn (exit 134)."
        if self.down.is_set() or self.dead.is_set():
            return 0
        wanted = None
        if paths is not None:
            wanted = {os.path.realpath(p) for p in paths}

        
        changes = []
        for p in sorted(wanted) if wanted else []:
            uri = "file://" + p
            if not self.doc_allowed(uri):
                continue
            
            changes.append({"uri": uri, "type": 2 if os.path.exists(p) else 3})
        if changes:
            self.to_server({"jsonrpc": "2.0",
                            "method": "workspace/didChangeWatchedFiles",
                            "params": {"changes": changes}})

        
        reseated = self.reseat(wanted)
        if changes or reseated:
            log(self.tag, "resync: %d watched-file change(s), %d document(s) re-seated"
                % (len(changes), reseated))
        return reseated

    def reseat(self, wanted=None):
        'Replace open documents with disk bytes while preserving wire order.\n\n        Client refcounts are unchanged. The state check, change and corresponding\n        didClose/didOpen messages share self.lock with handle_doc and open_held.'
        with self.lock:
            open_uris = sorted(self.doc_open_sent)
        reseated = 0
        for uri in open_uris:
            path = uri[len("file://"):]
            if wanted is not None and os.path.realpath(path) not in wanted:
                continue
            try:
                with open(path, "r", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                text = None
            with self.lock:
                if uri not in self.doc_open_sent:
                    continue
                if text is None:
                    self.doc_open_sent.discard(uri)
                    self.doc_text.pop(uri, None)
                    self.to_server({"jsonrpc": "2.0", "method": "textDocument/didClose",
                                    "params": {"textDocument": {"uri": uri}}})
                else:
                    self.doc_text[uri] = text
                    self.to_server({"jsonrpc": "2.0", "method": "textDocument/didClose",
                                    "params": {"textDocument": {"uri": uri}}})
                    self.to_server({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                                    "params": {"textDocument": {
                                        "uri": uri, "languageId": self.langid(),
                                        "version": int(time.time()), "text": text}}})
                reseated += 1
        return reseated

    def scan_tree(self):
        '{path: (mtime_ns, size)} for every file under the workspace a query could read.'
        seen = {}
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in SYNC_SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if os.path.splitext(name)[1].lower() in SYNC_SKIP_EXTS:
                    continue
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                seen[path] = (st.st_mtime_ns, st.st_size)
        return seen

    def sync_scan(self):
        "Bring the server level with the disk before a request is answered.\n\n        An agent writes files with its harness's Edit tool, never through the language\n        server, so without this a reference query answers from the text the server last\n        saw. The first scan is the baseline; each later one diffs against the last and\n        sends workspace/didChangeWatchedFiles (1 created, 2 changed, 3 deleted) plus a\n        re-seat of any open document that changed. Concurrent clients serialize on\n        sync_lock, and the throttle means a change is announced once, not once per client."
        if self.down.is_set() or self.dead.is_set():
            return
        with self.sync_lock:
            if time.time() - self.sync_at < SYNC_THROTTLE:
                return
            seen = self.scan_tree()
            prior, self.sync_seen = self.sync_seen, seen
            self.sync_at = time.time()
            if prior is None:
                return
            created = sorted(p for p in seen if p not in prior)
            deleted = sorted(p for p in prior if p not in seen)
            changed = sorted(p for p in seen if p in prior and seen[p] != prior[p])
            changes = [{"uri": "file://" + p, "type": kind}
                       for kind, group in ((1, created), (2, changed), (3, deleted))
                       for p in group if self.doc_allowed("file://" + p)]
            if changes:
                self.to_server({"jsonrpc": "2.0",
                                "method": "workspace/didChangeWatchedFiles",
                                "params": {"changes": changes}})
            reseated = 0
            if changed or deleted:
                reseated = self.reseat({os.path.realpath(p) for p in changed + deleted})
            if changes or reseated:
                log(self.tag, "sync: %d watched-file change(s), %d document(s) re-seated"
                    % (len(changes), reseated))

    def git_watch(self):
        'Notice a git-driven change to the workspace and resync off it.\n\n        Polling, not a filesystem watcher, and deliberately: the state that matters is\n        two cheap stats and a rev-parse, the event we care about is seconds-scale, and a\n        watcher would mean a dependency plus a whole class of missed-event bugs of its own\n        on the platform where this must not fail.'
        while not self.dead.is_set():
            time.sleep(GIT_POLL_SECONDS)
            if self.down.is_set():
                continue
            
            
            
            try:
                self.manifest_check()
            except Exception as exc:      
                log(self.tag, "manifest_check error: %r" % (exc,))
            try:
                state = self.git_head()
                if state is None:
                    continue
                with self.lock:
                    prev = self.git_state
                    self.git_state = state
                if prev is None or prev == state:
                    continue
                
                
                
                changed = self.changed_between(prev[0], state[0])
                log(self.tag, "git state moved %s -> %s (%s)"
                    % (prev[0][:8], state[0][:8],
                       "%d file(s)" % len(changed) if changed is not None else "range unresolvable"))
                self.resync(changed)
            except Exception as exc:      
                log(self.tag, "git_watch error: %r" % (exc,))

    def manifest_check(self):
        'Replace the server when the set of project manifests it started with changes.\n\n        The fault this closes: rust-analyzer finds its Cargo projects once, at startup,\n        and never looks again. When a crate moves, the daemon keeps its startup project\n        model and every symbol in the moved crate answers "not found". git_watch cannot\n        see a move inside an untracked nested repo, because the outer HEAD and index do\n        not change, and resyncing files does not make the server load a crate it does\n        not know about. Only a fresh process rediscovers.\n\n        So the daemon records the set at each start (start_server) and compares it here,\n        on git_watch\'s cadence. A change of any kind -- a manifest appearing, vanishing or\n        moving -- replaces the server through forced_restart, the path the canary uses,\n        and start_server records the new set. The restart is charged to the restart\n        budget, not granted outside it: with none left the change is logged once and\n        retried on a later poll, after the window has refilled.'
        if self.key not in MANIFEST_WATCH_KEYS or self.manifests is None:
            return
        now = cargo_manifests(self.workspace)
        if now is None or now == self.manifests:
            self.manifest_deferred = None
            return
        with self.lock:
            self.restarts = [t for t in self.restarts if time.time() - t < RESTART_WINDOW]
            budget = len(self.restarts) < MAX_RESTARTS
        gone = sorted(self.manifests - now)
        new = sorted(now - self.manifests)
        if not budget:
            if self.manifest_deferred != now:
                self.manifest_deferred = now
                log(self.tag, "manifests changed (gone %s, new %s) but the restart budget "
                              "is spent; retrying when it refills" % (gone, new))
            return
        self.manifest_deferred = None
        log(self.tag, "manifests changed (gone %s, new %s): the server's project model "
                      "is stale; replacing it" % (gone, new))
        res = self.forced_restart(keep_budget=True)
        log(self.tag, "manifest restart %r" % (res,))

    def langid(self):
        "The languageId to re-open a document under. The seed's own id, which is the\n        only one this daemon ever asserts; servers key parsing off the extension anyway."
        return (seed_spec(self.key) or (None, "plaintext"))[1]

    def doc_allowed(self, uri):
        'False for a document this server has no parser for. See DOC_EXTS.'
        exts = DOC_EXTS.get(self.key)
        if not exts or not isinstance(uri, str):
            return True
        
        
        if not uri.startswith("file://"):
            return True
        return os.path.splitext(uri)[1].lower() in exts

    def note_change(self, msg):
        'Keep doc_text in step with a didChange the server is about to receive.\n\n        A whole-document change (no `range`, which is all the bridge sends) replaces the\n        text. A ranged one drops it rather than re-applying the edit here: the canary then\n        reads disk, which is what it did before doc_text existed.'
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri")
        changes = params.get("contentChanges")
        with self.lock:
            if uri not in self.doc_open_sent or changes == []:
                return                       
            if (isinstance(changes, list) and changes
                    and all(isinstance(c, dict) and "range" not in c
                            and isinstance(c.get("text"), str) for c in changes)):
                self.doc_text[uri] = changes[-1]["text"]
            else:
                self.doc_text.pop(uri, None)

    def handle_doc(self, cid, method, msg):
        uri = (((msg.get("params") or {}).get("textDocument")) or {}).get("uri")
        if not uri:
            return
        with self.lock:
            refs = self.doc_refs.setdefault(uri, set())
            if method == "textDocument/didOpen":
                
                
                
                
                forward = uri not in self.doc_open_sent
                refs.add(cid)
                if forward:
                    self.doc_open_sent.add(uri)
                    text = ((msg.get("params") or {}).get("textDocument") or {}).get("text")
                    if isinstance(text, str):
                        self.doc_text[uri] = text
            else:
                refs.discard(cid)
                forward = not refs
                if forward:
                    self.doc_refs.pop(uri, None)
                    
                    forward = uri in self.doc_open_sent
                    self.doc_open_sent.discard(uri)
                    self.doc_text.pop(uri, None)
            if forward:
                self.to_server(msg)

    

    def shutdown(self):
        if self.dead.is_set():
            return
        self.dead.set()
        with self.lock:
            clients = list(self.clients.values())
            self.clients.clear()
        for c in clients:
            c.close()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                for _ in range(20):
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.2)
                if self.proc.poll() is None:
                    self.proc.kill()
            except OSError:
                pass

    def reaper(self, path):
        while not self.dead.is_set():
            time.sleep(5)
            with self.lock:
                empty = len(self.clients) == 0
                since = time.time() - self.last_empty
            if empty and since > IDLE_EXIT_SECONDS:
                log(self.tag, "idle %.0fs with no clients -- exiting" % since)
                self.shutdown()
                break
        try:
            os.unlink(path)
        except OSError:
            pass

    def run(self, path):
        if not self.start_server():
            return 127
        try:
            os.unlink(path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(64)
        srv.settimeout(1.0)
        threading.Thread(target=self.reaper, args=(path,), daemon=True).start()
        
        
        self.git_state = self.git_head()
        threading.Thread(target=self.git_watch, daemon=True).start()
        if CANARY_ON:
            threading.Thread(target=self.canary_watch, daemon=True).start()
        log(self.tag, "listening on %s" % path)
        while not self.dead.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.serve_client, args=(conn,), daemon=True).start()
        try:
            srv.close()
            os.unlink(path)
        except OSError:
            pass
        return 0






def try_connect(path):
    if not os.path.exists(path):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        return s
    except OSError:
        s.close()
        return None


def spawn_daemon(key, workspace, argv, path):
    "Connect-or-spawn, lockfile-guarded.\n\n    The lock is held across the spawn. Probing a lock and releasing it before the\n    spawn is check-then-act: two contenders both see 'free' and both proceed."
    os.makedirs(RUN, exist_ok=True)
    
    os.makedirs(LOGS, exist_ok=True)
    lockp = path + ".spawnlock"
    fd = os.open(lockp, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX)
        s = try_connect(path)          
        if s:
            return s
        out = open(os.path.join(LOGS, key_digest(key, workspace) + ".daemon.log"), "ab", 0)
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--daemon",
             "--key", key, "--workspace", workspace, "--"] + argv,
            stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            start_new_session=True, close_fds=True)
        deadline = time.time() + SPAWN_WAIT_SECONDS
        while time.time() < deadline:
            s = try_connect(path)
            if s:
                return s
            time.sleep(0.2)
        return None
    finally:
        try:
            fcntl.lockf(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


def attach(key, workspace, argv):
    "A stateless byte pump. Everything clever lives in the daemon; this end stays dumb\n    deliberately, because it runs in the bridge's process and anything it gets wrong takes\n    the session's code intelligence with it."
    path = sock_path(key, workspace)
    s = try_connect(path) or spawn_daemon(key, workspace, argv, path)
    if s is None:
        sys.stderr.write("lspd: could not reach or start a daemon for %s at %s\n"
                         % (key, workspace))
        return 127

    stdin_fd = sys.stdin.buffer.fileno()
    sock_fd = s.fileno()
    out = sys.stdout.buffer
    try:
        while True:
            r, _, _ = select.select([stdin_fd, sock_fd], [], [])
            if stdin_fd in r:
                data = os.read(stdin_fd, 65536)
                if not data:
                    break
                s.sendall(data)
            if sock_fd in r:
                data = s.recv(65536)
                if not data:
                    break
                out.write(data)
                out.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            s.close()
        except OSError:
            pass
    return 0


def health_of(path):
    '(state, health-dict-or-None) for one daemon socket.'
    s = try_connect(path)
    if not s:
        return "STALE", None
    out = None
    try:
        rfh = s.makefile("rb")
        s.sendall(frame({"jsonrpc": "2.0", "id": 1, "method": "$/lspd/health",
                         "params": {}}))
        s.settimeout(10)
        for _ in range(8):
            msg = read_message(rfh)
            if msg is None:
                break
            if msg.get("id") == 1:
                if not msg.get("error"):
                    out = msg.get("result") or {}
                break
    except (OSError, ValueError):
        out = None
    finally:
        try:
            s.close()
        except OSError:
            pass
    
    
    if isinstance(out, dict) and (out.get("down") or out.get("pid") is None):
        return "DOWN", out
    return "LIVE", out


def code_line(health, key=None):
    'Says nothing at all when the daemon is current, because a status command whose\n    every row carries a line about code identity teaches the reader to skip the\n    column, and this is the column that has to be believed the one time it differs.\n\n    A daemon predating the health verb cannot answer at all; that is itself proof it\n    is stale, and it is the loudest thing on the row.'
    fix = "run `lspd.py --upgrade --key %s`" % (key or "<key>")
    if health is None:
        return "code=STALE (predates the health verb) -- " + fix
    ran = health.get("code")
    now = CODE_AT_START
    
    
    if ran is None:
        return "code=STALE (predates the code field) -- " + fix
    if not isinstance(ran, dict) or not isinstance(now, dict):
        
        
        
        
        return "code=unknown"
    
    
    
    if ran.get("protocol") is None:
        return ("code=STALE (predates the protocol field; running %s) -- %s"
                % (ran.get("sha"), fix))
    if ran.get("protocol") == now.get("protocol"):
        return ""
    return ("code=STALE (protocol %s running, %s on disk; build %s vs %s) -- %s"
            % (ran.get("protocol"), now.get("protocol"), ran.get("sha"), now.get("sha"), fix))


def canary_line(health, key=None):
    'One phrase about answer quality.'
    if health is None:
        return ("canary=unknown (this daemon predates the health verb -- retire it with "
                "`lspd.py --upgrade --key %s` and the next attach starts one that has it)"
                % (key or "<key>"))
    c = health.get("canary") or {}
    state = c.get("state", "?")
    age = ""
    if c.get("last_at"):
        age = " age=%ds" % int(max(0, time.time() - c["last_at"]))
    tail = " probe=%s baseline=%s last=%s restarts=%d%s" % (
        c.get("symbol"), c.get("baseline"), c.get("last"), c.get("restarts", 0), age)
    if state == "calibrated" and (c.get("last") or 0) > 0:
        return "canary=ok" + tail
    if state == "calibrated":
        return "canary=EMPTY -- THIS SERVER'S ANSWERS ARE NOT EVIDENCE" + tail
    return "canary=%s%s" % (state, tail)


def status(want_key=None):
    "`lspd.py --status [--key K]`. Exit 1 while a listed daemon is DOWN.\n\n    --key scopes the rows, the exit code and the litter cleanup to one key, matched\n    whole (a key is never a prefix of another's rows). Unscoped, the exit code is a\n    fact about the whole host, so a check on one daemon fails whenever any other daemon\n    on the machine is down."
    if not os.path.isdir(RUN):
        print("no daemons")
        return 0
    rows = [f for f in sorted(os.listdir(RUN)) if f.endswith(".sock")]
    if want_key:
        
        rows = [f for f in rows
                if (f[:-5].rsplit("-", 1)[0] if "-" in f[:-5] else f[:-5]) == want_key]
        if not rows:
            print("no daemon for %s" % want_key)
            return 0
    if not rows:
        print("no daemons")
        return 0
    rc = 0
    for f in rows:
        p = os.path.join(RUN, f)
        state, health = health_of(p)
        if state == "STALE":
            
            
            for junk in (p, p + ".spawnlock"):
                try:
                    os.unlink(junk)
                except OSError:
                    pass
            print("%-6s %s" % (state, f[:-5]))
            continue
        
        
        name = f[:-5]
        key = name.rsplit("-", 1)[0] if "-" in name else name
        code = code_line(health, key)
        if state == "DOWN":
            
            
            
            print("%-6s %-30s %s" % (state, name,
                                     "the daemon is listening but its LANGUAGE SERVER is "
                                     "not running -- every symbol question fails; see "
                                     + os.path.join(LOGS, name + ".log")))
            if code:
                print("%-6s %-30s %s" % ("", "", code))
            rc = 1
            continue
        
        
        
        
        
        n = health.get("clients") if isinstance(health, dict) else None
        who = "" if n is None else " clients=%d" % n
        print("%-6s %-30s %s%s" % (state, name, canary_line(health, key), who))
        
        
        
        
        
        if code:
            print("%-6s %-30s %s" % ("", "", code))
    return rc


def resync_all(paths, key=None):
    "Force a resync on live daemons. `lspd.py --resync [--key K] [paths...]`.\n\n    With no --key, every socket is asked, so the caller need not know a\n    (key, workspace) digest: a daemon whose workspace does not contain the named paths\n    does almost nothing with the message, and the sockets are few.\n\n    --key narrows it to one server's daemons. A caller who knows the answers are wrong\n    for one project need not poke the others, and a test can talk to its own mock\n    daemon without touching the machine's live ones.\n\n    Normally unnecessary: the daemon watches the workspace's git state itself. This is\n    the manual handle for the cases it cannot see -- a rebuild that rewrote generated\n    sources, a dependency reinstall, a checkout in a workspace that is not a git repo."
    if not os.path.isdir(RUN):
        print("no daemons")
        return 0
    socks = [f for f in sorted(os.listdir(RUN)) if f.endswith(".sock")]
    if key:
        socks = [f for f in socks if f.startswith(key + "-")]
    if not socks:
        print("no daemons")
        return 0
    rc = 0
    for f in socks:
        s = try_connect(os.path.join(RUN, f))
        if not s:
            continue
        try:
            rfh = s.makefile("rb")
            s.sendall(frame({"jsonrpc": "2.0", "id": 1, "method": "$/lspd/resync",
                             "params": {"paths": paths}}))
            s.settimeout(30)
            
            
            
            reply = None
            for _ in range(8):
                msg = read_message(rfh)
                if msg is None:
                    break
                if msg.get("id") == 1:
                    reply = msg
                    break
            if reply is None:
                print("%-6s %s  no reply to the resync request" % ("ERR", f[:-5]))
                rc = 1
            elif reply.get("error"):
                detail = (reply["error"] or {}).get("message", "unknown error")
                
                
                
                if "unknown method" in detail or "InvalidRequest" in detail:
                    detail += ("  (this daemon predates the resync verb -- it will "
                               "have it after its next start; do NOT kill it while "
                               "a session is attached, that costs the session its "
                               "LSP outright)")
                print("%-6s %s  %s" % ("ERR", f[:-5], detail))
                rc = 1
            else:
                result = reply.get("result") or {}
                print("%-6s %s  reseated=%d"
                      % ("OK", result.get("workspace", f[:-5]),
                         result.get("reseated", 0)))
        except (OSError, ValueError) as exc:
            print("%-6s %s  %r" % ("ERR", f[:-5], exc))
            rc = 1
        finally:
            try:
                s.close()
            except OSError:
                pass
    return rc


def one_socket(key, workspace=None):
    '(socket-filename, None) for the single daemon named, or (None, rc) with the\n    reason already printed.\n\n    Shared by --restart and --upgrade because both are single-daemon-by-construction:\n    each costs an index rebuild for every session attached to that daemon, so neither\n    may sweep, and both must refuse an ambiguous key rather than pick one.'
    if not os.path.isdir(RUN):
        print("no daemons")
        return None, 1
    if workspace:
        socks = [key_digest(key, workspace) + ".sock"]
        if not os.path.exists(os.path.join(RUN, socks[0])):
            print("no daemon for %s in %s" % (key, workspace))
            return None, 1
    else:
        socks = [f for f in sorted(os.listdir(RUN))
                 if f.endswith(".sock") and f.startswith(key + "-")]
    if not socks:
        print("no daemon for %s" % key)
        return None, 1
    if len(socks) > 1:
        print("%d daemons for %s -- name one with --workspace:" % (len(socks), key))
        for f in socks:
            print("  %s" % f[:-5])
        return None, 1
    return socks[0], None


def upgrade_all(key, workspace=None, force=False):
    'The gap this fills: see the code-identity note at CODE_PATH. A pkill is\n    indistinguishable from a crash and cannot report attached sessions.\n    Observations guarded: #323, #347.\n\n    Why not respawn it here: the next `--attach` already does\n    that, under a spawnlock that serializes competing starters. Respawning from this\n    command would be a second, unlocked path to the same thing, and it would start a\n    daemon with no client attached -- which the idle reaper then kills an hour later,\n    having paid a cold index for nothing.\n\n    Refuses when other clients are attached, because retiring the daemon costs each of\n    them its code intelligence mid-session and they get no warning; --force is for\n    when that is understood and wanted anyway.'
    f, rc = one_socket(key, workspace)
    if f is None:
        return rc
    path = os.path.join(RUN, f)
    state, health = health_of(path)
    if state == "STALE":
        
        
        for junk in (path, path + ".spawnlock"):
            try:
                os.unlink(junk)
            except OSError:
                pass
        print("%-6s %s  socket was stale (no daemon listening) -- removed it; "
              "the next attach starts a fresh one" % ("OK", f[:-5]))
        return 0
    running = (health or {}).get("code") if isinstance(health, dict) else None
    current = CODE_AT_START
    if (not force and isinstance(running, dict) and isinstance(current, dict)
            and running.get("protocol") is not None
            and running.get("protocol") == current.get("protocol")):
        print("%-6s %s  already on protocol %s (build %s) -- nothing to do; --force "
              "retires it anyway" % ("OK", f[:-5], running.get("protocol"), running.get("sha")))
        return 0
    attached = (health or {}).get("clients") if isinstance(health, dict) else None
    if attached and attached > 0 and not force:
        print("%-6s %s  %d client(s) are attached RIGHT NOW"
              % ("ERR", f[:-5], attached))
        print("       Retiring it takes their language server away mid-session, and a "
              "session that loses its LSP must stop rather than fall back to grep.")
        print("       Wait for them to detach, or say --force if that cost is understood.")
        return 1

    s = try_connect(path)
    if not s:
        print("%-6s %s  socket vanished between the health check and the request"
              % ("OK", f[:-5]))
        return 0
    reply = None
    try:
        rfh = s.makefile("rb")
        s.sendall(frame({"jsonrpc": "2.0", "id": 1, "method": "$/lspd/shutdown",
                         "params": {}}))
        s.settimeout(30)
        
        
        
        for _ in range(200):
            msg = read_message(rfh)
            if msg is None:
                break
            if msg.get("id") == 1 and "method" not in msg:
                reply = msg
                break
    except (OSError, ValueError) as exc:
        print("%-6s %s  %r" % ("ERR", f[:-5], exc))
        return 1
    finally:
        try:
            s.close()
        except OSError:
            pass

    if reply is None or reply.get("error"):
        detail = ((reply or {}).get("error") or {}).get("message", "no reply")
        
        
        
        
        low = detail.lower()
        if (reply is None or "$/lspd/shutdown" in detail
                or "unknown method" in low or "invalidrequest" in low
                or "method not found" in low or "no handler" in low
                or "not supported" in low or "language server is down" in low):
            
            
            print("%-6s %s  this daemon predates the shutdown verb, so it cannot be "
                  "asked to retire" % ("ERR", f[:-5]))
            print("       It has to be killed, and it is safe to do so ONLY when no "
                  "session is attached -- which this daemon is too old to report, so "
                  "check that no session has this project open before running:")
            print("         pkill -f 'lspd.py .*--key %s'" % key)
            print("       The next attach then starts a daemon that HAS this verb, and "
                  "this is the last time it will need doing for that daemon.")
            return 1
        print("%-6s %s  %s" % ("ERR", f[:-5], detail))
        return 1

    
    
    
    r = reply.get("result") or {}
    deadline = time.time() + 20
    while time.time() < deadline:
        if not try_connect(path):
            was = (r.get("code") or {}).get("sha") if isinstance(r.get("code"), dict) else None
            now = current.get("sha") if isinstance(current, dict) else None
            print("%-6s %s  retired daemon pid %s (%s) -- the next attach starts one on %s"
                  % ("OK", f[:-5], r.get("daemon_pid"), was, now))
            return 0
        time.sleep(0.2)
    print("%-6s %s  it accepted the request but is still listening after 20s"
          % ("ERR", f[:-5]))
    print("       Its child language server may be refusing to die; see %s"
          % os.path.join(LOGS, f[:-5] + ".log"))
    return 1


def restart_all(key, workspace=None):
    'Unlike --resync, this requires --key and never sweeps. A resync costs a daemon a few\n    document re-opens; a restart costs it a cold index -- for Kotlin, a Gradle import\n    measured in tens of seconds -- and every other session attached to that daemon pays\n    it too. A verb that repairs one project must not be able to stall three.\n\n    With several daemons under one key (one per workspace) and no --workspace, it prints\n    them and does nothing, for the same reason.'
    f, rc = one_socket(key, workspace)
    if f is None:
        return rc
    s = try_connect(os.path.join(RUN, f))
    if not s:
        
        
        for junk in (os.path.join(RUN, f), os.path.join(RUN, f) + ".spawnlock"):
            try:
                os.unlink(junk)
            except OSError:
                pass
        print("%-6s %s  socket was stale (no daemon listening) -- removed it; the next "
              "attach starts a fresh daemon" % ("OK", f[:-5]))
        return 0
    try:
        rfh = s.makefile("rb")
        s.sendall(frame({"jsonrpc": "2.0", "id": 1, "method": "$/lspd/restart",
                         "params": {}}))
        
        
        
        s.settimeout(SPAWN_WAIT_SECONDS * 2 + 30)
        reply = None
        
        
        
        
        for _ in range(200):
            msg = read_message(rfh)
            if msg is None:
                break
            if msg.get("id") == 1 and "method" not in msg:
                reply = msg
                break
        if reply is None:
            print("%-6s %s  no reply to the restart request" % ("ERR", f[:-5]))
            return 1
        if reply.get("error"):
            detail = (reply["error"] or {}).get("message", "unknown error")
            
            
            
            
            
            if ("unknown method" in detail or "InvalidRequest" in detail
                    or "language server is down" in detail):
                detail += ("  (this daemon predates the restart verb -- it will have it "
                           "after its next start; do NOT kill it while a session is "
                           "attached, that costs the session its LSP outright)")
            print("%-6s %s  %s" % ("ERR", f[:-5], detail))
            return 1
        r = reply.get("result") or {}
        if not r.get("restarted"):
            
            
            
            if r.get("was"):
                print("%-6s %s  pid %s -> (none)  THE REPLACEMENT NEVER ANSWERED initialize"
                      % ("FAIL", r.get("workspace", f[:-5]), r.get("was")))
                print("       The old server is GONE -- a restart kills it first -- so this "
                      "project has NO code intelligence now.")
                print("       The server itself is what is broken; restarting again will not "
                      "help. Read %s for the child's exit code, and run the server command "
                      "by hand to see what it prints."
                      % os.path.join(LOGS, f[:-5] + ".log"))
                return 1
            print("%-6s %s  the server did not come back -- see %s"
                  % ("ERR", r.get("workspace", f[:-5]), os.path.join(LOGS, f[:-5] + ".log")))
            return 1
        
        
        
        
        if not r.get("handshaked"):
            print("%-6s %s  pid %s -> %s  THE REPLACEMENT NEVER ANSWERED initialize"
                  % ("FAIL", r.get("workspace", f[:-5]), r.get("was"), r.get("pid")))
            print("       The old server is GONE -- a restart kills it first -- so this "
                  "project has NO code intelligence now.")
            print("       The server itself is what is broken; restarting again will not "
                  "help. Read %s for the child's exit code, and run the server command "
                  "by hand to see what it prints."
                  % os.path.join(LOGS, f[:-5] + ".log"))
            return 1
        print("%-6s %s  pid %s -> %s  handshaked=%s warm=%s"
              % ("OK", r.get("workspace", f[:-5]), r.get("was"), r.get("pid"),
                 "yes" if r.get("handshaked") else "no",
                 "yes" if r.get("warm") else "no"))
        return 0
    except (OSError, ValueError) as exc:
        print("%-6s %s  %r" % ("ERR", f[:-5], exc))
        return 1
    finally:
        try:
            s.close()
        except OSError:
            pass



















MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_INFO = {"name": "MCP Language Server", "version": "v0.0.2"}

MCP_LOST = "language server is down: the lspd daemon connection closed; retry"


def _mcp_tool(name, description, properties, required):
    return {"annotations": {"destructiveHint": True, "openWorldHint": True},
            "description": description, "name": name,
            "inputSchema": {"properties": properties, "required": required,
                            "type": "object"}}


MCP_TOOLS = [
    _mcp_tool("definition",
              "Read the source code definition of a symbol (function, type, constant, etc.) "
              "from the codebase. Returns the complete implementation code where the symbol "
              "is defined.",
              {"symbolName": {"description": "The name of the symbol whose definition you "
                                             "want to find (e.g. 'mypackage.MyFunction', "
                                             "'MyType.MyMethod')", "type": "string"}},
              ["symbolName"]),
    _mcp_tool("diagnostics",
              "Get diagnostic information for a specific file from the language server.",
              {"contextLines": {"default": False,
                                "description": "Lines to include around each diagnostic.",
                                "type": "boolean"},
               "filePath": {"description": "The path to the file to get diagnostics for",
                            "type": "string"},
               "showLineNumbers": {"default": True,
                                   "description": "If true, adds line numbers to the output",
                                   "type": "boolean"}},
              ["filePath"]),
    _mcp_tool("edit_file", "Apply multiple text edits to a file.",
              {"edits": {"description": "List of edits to apply", "type": "array",
                         "items": {"type": "object", "required": ["startLine", "endLine"],
                                   "properties": {
                                       "endLine": {"description": "End line to replace, "
                                                                  "inclusive, one-indexed",
                                                   "type": "number"},
                                       "newText": {"description": "Replacement text. Replace "
                                                                  "with the new text. Leave "
                                                                  "blank to remove lines.",
                                                   "type": "string"},
                                       "startLine": {"description": "Start line to replace, "
                                                                    "inclusive, one-indexed",
                                                     "type": "number"}}}},
               "filePath": {"description": "Path to the file to edit", "type": "string"}},
              ["edits", "filePath"]),
    _mcp_tool("hover",
              "Get hover information (type, documentation) for a symbol at the specified "
              "position.",
              {"column": {"description": "The column number where the hover is requested "
                                         "(1-indexed)", "type": "number"},
               "filePath": {"description": "The path to the file to get hover information for",
                            "type": "string"},
               "line": {"description": "The line number where the hover is requested "
                                       "(1-indexed)", "type": "number"}},
              ["filePath", "line", "column"]),
    _mcp_tool("references",
              "Find all usages and references of a symbol throughout the codebase. Returns a "
              "list of all files and locations where the symbol appears.",
              {"symbolName": {"description": "The name of the symbol to search for (e.g. "
                                             "'mypackage.MyFunction', 'MyType')",
                              "type": "string"}},
              ["symbolName"]),
    _mcp_tool("rename_symbol",
              "Rename a symbol (variable, function, class, etc.) at the specified position "
              "and update all references throughout the codebase.",
              {"column": {"description": "The column number where the symbol is located "
                                         "(1-indexed)", "type": "number"},
               "filePath": {"description": "The path to the file containing the symbol to "
                                           "rename", "type": "string"},
               "line": {"description": "The line number where the symbol is located "
                                       "(1-indexed)", "type": "number"},
               "newName": {"description": "The new name for the symbol", "type": "string"}},
              ["filePath", "line", "column", "newName"]),
]


GO_SYMBOL_KINDS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class", 6: "Method",
    7: "Property", 8: "Field", 9: "Constructor", 10: "Enum", 11: "Interface",
    12: "Function", 13: "Variable", 14: "Constant", 15: "String", 16: "Number",
    17: "Boolean", 18: "Array", 19: "Object", 20: "Key", 21: "Null", 22: "EnumMember",
    23: "Struct", 24: "Event", 25: "Operator", 26: "TypeParameter"}


GO_LANGUAGE_IDS = {
    ".abap": "abap", ".bat": "bat", ".bib": "bibtex", ".bibtex": "bibtex",
    ".clj": "clojure", ".coffee": "coffeescript", ".c": "c", ".cpp": "cpp", ".cxx": "cpp",
    ".cc": "cpp", ".c++": "cpp", ".cs": "csharp", ".css": "css", ".d": "d",
    ".pas": "pascal", ".pascal": "pascal", ".diff": "diff", ".patch": "diff",
    ".dart": "dart", ".dockerfile": "dockerfile", ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang", ".hrl": "erlang", ".fs": "fsharp", ".fsi": "fsharp", ".fsx": "fsharp",
    ".fsscript": "fsharp", ".gitcommit": "git-commit", ".gitrebase": "rebase", ".go": "go",
    ".groovy": "groovy", ".hbs": "handlebars", ".handlebars": "handlebars",
    ".hs": "haskell", ".html": "html", ".htm": "html", ".ini": "ini", ".java": "java",
    ".js": "javascript", ".jsx": "javascriptreact", ".json": "json", ".tex": "latex",
    ".latex": "latex", ".less": "less", ".lua": "lua", ".makefile": "makefile",
    ".md": "markdown", ".markdown": "markdown", ".m": "objective-c",
    ".mm": "objective-cpp", ".pl": "perl", ".pm": "perl6", ".php": "php",
    ".ps1": "powershell", ".psm1": "powershell", ".pug": "jade", ".jade": "jade",
    ".py": "python", ".r": "r", ".cshtml": "razor", ".razor": "razor", ".rb": "ruby",
    ".rs": "rust", ".scss": "scss", ".sass": "sass", ".scala": "scala",
    ".shader": "shaderlab", ".sh": "shellscript", ".bash": "shellscript",
    ".zsh": "shellscript", ".ksh": "shellscript", ".sql": "sql", ".swift": "swift",
    ".ts": "typescript", ".tsx": "typescriptreact", ".xml": "xml", ".xsl": "xsl",
    ".yaml": "yaml", ".yml": "yaml"}


class GoError(Exception):
    'An error whose text is the Go bridge\'s. Callers wrap it the way the Go code wrapped\n    its errors, so the chain of "failed to ...: " prefixes comes out the same.'


class MCPDown(GoError):
    'The daemon is gone or unreachable. Never swallowed on the way up: a tool that\n    silently fell back on one would answer as if the server had found nothing.'


class _Reconnected(Exception):
    'A notification was pinned to a connection that has since been replaced.'


def go_language_id(uri):
    base = uri.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return GO_LANGUAGE_IDS.get(base[dot:].lower() if dot >= 0 else "", "")


def _go_os_error(op, path, exc):
    import errno
    text = {errno.ENOENT: "no such file or directory", errno.EACCES: "permission denied",
            errno.EISDIR: "is a directory", errno.ENOTDIR: "not a directory"}.get(
        exc.errno, (exc.strerror or str(exc)).lower())
    return GoError("%s %s: %s" % (op, path, text))


def go_read_file(path):
    "os.ReadFile, with Go's error text."
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except IsADirectoryError as exc:
        raise _go_os_error("read", path, exc)
    except OSError as exc:
        raise _go_os_error("open", path, exc)


def _go_lines(data):
    return data.decode("utf-8", errors="replace").split("\n")


def _go_float(f):
    "fmt's %v for a float64: shortest digits, exponent form below 1e-4 or from 1e6."
    import decimal
    if f != f:
        return "NaN"
    if f in (float("inf"), float("-inf")):
        return "+Inf" if f > 0 else "-Inf"
    if f == 0:
        return "0"
    sign, digits, exp = decimal.Decimal(repr(abs(f))).normalize().as_tuple()
    digits = "".join(str(d) for d in digits)
    dp = len(digits) + int(exp)
    prefix = "-" if f < 0 else ""
    if dp - 1 < -4 or dp - 1 >= 6:
        mant = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        e = dp - 1
        return "%s%se%s%02d" % (prefix, mant, "-" if e < 0 else "+", abs(e))
    if dp <= 0:
        return prefix + "0." + "0" * -dp + digits
    if dp >= len(digits):
        return prefix + digits + "0" * (dp - len(digits))
    return prefix + digits[:dp] + "." + digits[dp:]


def go_v(value):
    "fmt's %v for a JSON value decoded into interface{}."
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _go_float(float(value))
    return json.dumps(value)


def _u1(n):
    "A uint32 position plus one, wrapping as Go's does."
    return ((n or 0) + 1) & 0xFFFFFFFF


def _pos(p):
    p = p if isinstance(p, dict) else {}
    return {"line": p.get("line") or 0, "character": p.get("character") or 0}


def _rng(r):
    r = r if isinstance(r, dict) else {}
    return {"start": _pos(r.get("start")), "end": _pos(r.get("end"))}







def _u32(v):
    return v is None or (type(v) is int and 0 <= v < 1 << 32)


def _strict(obj, fields):
    if obj is None:
        return True
    if not isinstance(obj, dict) or any(k not in fields for k in obj):
        return False
    return all(obj[k] is None or check(obj[k]) for k, check in fields.items() if k in obj)


def _is_str(v):
    return isinstance(v, str)


def _is_bool(v):
    return isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_u32s(v):
    return isinstance(v, list) and all(_u32(x) for x in v)


def _is_pos(v):
    return _strict(v, {"line": _u32, "character": _u32})


def _is_range(v):
    return _strict(v, {"start": _is_pos, "end": _is_pos})


def _is_location(v):
    return _strict(v, {"uri": _is_str, "range": _is_range})


_SI_FIELDS = {"deprecated": _is_bool, "location": _is_location, "name": _is_str,
              "kind": _u32, "tags": _is_u32s, "containerName": _is_str}
_WS_FIELDS = {"location": lambda v: _is_location(v) or _strict(v, {"uri": _is_str}),
              "data": lambda v: True, "name": _is_str, "kind": _u32, "tags": _is_u32s,
              "containerName": _is_str, "score": _is_num}


def _is_docsym(v):
    return _strict(v, {"name": _is_str, "detail": _is_str, "kind": _u32, "tags": _is_u32s,
                       "deprecated": _is_bool, "range": _is_range,
                       "selectionRange": _is_range,
                       "children": lambda c: isinstance(c, list)
                       and all(_is_docsym(x) for x in c)})


def decode_workspace_symbols(result):
    if result is None:
        return []
    if isinstance(result, list):
        if all(_strict(x, _SI_FIELDS) for x in result):
            return [("si", x or {}) for x in result]
        if all(_strict(x, _WS_FIELDS) for x in result):
            return [("ws", x or {}) for x in result]
    raise GoError("failed to unmarshal result: unmarshal failed to match one of "
                  "[[]SymbolInformation []WorkspaceSymbol]")


def decode_document_symbols(result):
    if result is None:
        return []
    if isinstance(result, list):
        if all(_is_docsym(x) for x in result):
            return [("ds", x or {}) for x in result]
        if all(_strict(x, _SI_FIELDS) for x in result):
            return [("si", x or {}) for x in result]
    raise GoError("failed to unmarshal result: unmarshal failed to match one of "
                  "[[]DocumentSymbol []SymbolInformation]")


def _symbol_range(shape, sym):
    if shape == "ds":
        return _rng(sym.get("range"))
    return _rng((sym.get("location") or {}).get("range"))


def _symbol_location(sym):
    loc = sym.get("location") if isinstance(sym.get("location"), dict) else {}
    return {"uri": loc.get("uri") or "", "range": _rng(loc.get("range"))}


def _contains(r, p):
    s, e = r["start"], r["end"]
    if s["line"] > p["line"] or e["line"] < p["line"]:
        return False
    if s["line"] == p["line"] and s["character"] > p["character"]:
        return False
    if e["line"] == p["line"] and e["character"] <= p["character"]:
        return False
    return True


def _more_specific(r, depth, best, best_depth):
    if best_depth < 0 or depth != best_depth:
        return depth > best_depth
    rl = (r["end"]["line"] - r["start"]["line"]) & 0xFFFFFFFF
    bl = (best["end"]["line"] - best["start"]["line"]) & 0xFFFFFFFF
    if rl != bl:
        return rl < bl
    return (r["end"]["character"] - r["start"]["character"]
            < best["end"]["character"] - best["start"]["character"])


def innermost_range(symbols, pos):
    "The range of the most specific symbol enclosing pos: deepest nesting, then smallest.\n\n    Go patch 4. Taking the first enclosing symbol, parent before children, returns a\n    whole class for a method. Nesting decides before size because some servers report a\n    child range that runs past its parent's end, and inside that overhang the child is\n    still the answer."
    best, best_depth = None, -1

    def walk(syms, depth):
        nonlocal best, best_depth
        for shape, sym in syms:
            r = _symbol_range(shape, sym)
            if _contains(r, pos) and _more_specific(r, depth, best, best_depth):
                best, best_depth = r, depth
            if shape == "ds" and sym.get("children"):
                walk([("ds", c or {}) for c in sym["children"]], depth + 1)

    walk(symbols, 0)
    return best


def add_line_numbers(text, start):
    lines = text.split("\n")
    width = len(str(start + len(lines)))
    return "".join("%s|%s\n" % (str(start + i).rjust(width), line)
                   for i, line in enumerate(lines))


def convert_lines_to_ranges(show, total):
    nums = sorted(n for n in show if 0 <= n < total)
    ranges = []
    for n in nums:
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return ranges


def format_lines_with_ranges(lines, ranges):
    out, last = [], -1
    for start, end in ranges:
        if last != -1 and start > last + 1:
            out.append("...\n")
        out.append(add_line_numbers("\n".join(lines[start:end + 1]), start + 1))
        last = end
    return "".join(out)


def _go_path_unescape(s):
    'url.PathUnescape: %XX decoded, a malformed escape refused.'
    from urllib.parse import unquote_to_bytes
    for m in re.finditer(r"%", s):
        seq = s[m.start():m.start() + 3]
        if not re.fullmatch(r"%[0-9A-Fa-f]{2}", seq):
            raise GoError("invalid URL escape %s" % json.dumps(seq))
    return os.fsdecode(unquote_to_bytes(s))


def _uri_path(uri):
    'DocumentUri.Path(): the unescaped path of a file URI.'
    if uri.startswith("file://"):
        from urllib.parse import urlsplit, unquote
        return unquote(urlsplit(uri).path)
    return uri


def _ranges_overlap(a, b):
    if a["start"]["line"] > b["end"]["line"] or b["start"]["line"] > a["end"]["line"]:
        return False
    if a["start"]["line"] == b["end"]["line"] and a["start"]["character"] > b["end"]["character"]:
        return False
    if b["start"]["line"] == a["end"]["line"] and b["start"]["character"] > a["end"]["character"]:
        return False
    return True


def _apply_text_edit(lines, edit, ending):
    'utilities.ApplyTextEdit, on byte lines, because Go slices a line by byte offset.'
    r = edit["range"]
    sl, el = r["start"]["line"], r["end"]["line"]
    sc, ec = r["start"]["character"], r["end"]["character"]
    if sl < 0 or sl >= len(lines):
        raise GoError("invalid start line: %d" % sl)
    if el < 0 or el >= len(lines):
        el = len(lines) - 1
    result = list(lines[:sl])
    head = lines[sl]
    if sc < 0 or sc > len(head):
        sc = len(head)
    prefix = head[:sc]
    tail = lines[el]
    if ec < 0 or ec > len(tail):
        ec = len(tail)
    suffix = tail[ec:]
    new = (edit.get("newText") or "").encode("utf-8")
    if not new:
        if prefix + suffix != b"":
            result.append(prefix + suffix)
    else:
        parts = new.split(b"\n")
        if len(parts) == 1:
            result.append(prefix + parts[0] + suffix)
        else:
            result.append(prefix + parts[0])
            result.extend(parts[1:-1])
            if el == sl or suffix or el < len(lines) - 1:
                result.append(parts[-1] + suffix)
            else:
                result.append(parts[-1])
    if el + 1 < len(lines):
        result.extend(lines[el + 1:])
    return result


def apply_text_edits(uri, edits):
    'utilities.ApplyTextEdits: line endings kept, overlapping edits refused.'
    path = uri[len("file://"):] if uri.startswith("file://") else uri
    try:
        content = go_read_file(path)
    except GoError as exc:
        raise GoError("failed to read file: %s" % exc)
    ending = b"\r\n" if b"\r\n" in content else b"\n"
    ends_with_newline = len(content) > 0 and content.endswith(ending)
    lines = content.split(ending)
    for i in range(len(edits)):
        for j in range(i + 1, len(edits)):
            if _ranges_overlap(edits[i]["range"], edits[j]["range"]):
                raise GoError("overlapping edits detected between edit %d and %d" % (i, j))
    ordered = sorted(edits, key=lambda e: (e["range"]["start"]["line"],
                                           e["range"]["start"]["character"]), reverse=True)
    for edit in ordered:
        try:
            lines = _apply_text_edit(lines, edit, ending)
        except GoError as exc:
            raise GoError("failed to apply edit: %s" % exc)
    data = ending.join(lines)
    if ends_with_newline and not data.endswith(ending):
        data += ending
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        raise GoError("failed to write file: %s" % _go_os_error("open", path, exc))


def _as_text_edit(e):
    'Or_TextDocumentEdit_edits_Elem.AsTextEdit: TextEdit or AnnotatedTextEdit, else None.'
    rng = {"range": _is_range, "annotationId": _is_str}
    if _strict(e, dict(rng, newText=_is_str)) and isinstance(e, dict):
        return {"range": _rng(e.get("range")), "newText": e.get("newText") or ""}
    return None


def _document_change_kind(change):
    if isinstance(change, dict) and change.get("kind") in ("create", "rename", "delete"):
        return change["kind"]
    return "edit"


def apply_workspace_edit(edit):
    'utilities.ApplyWorkspaceEdit: `changes`, then `documentChanges` in order.'
    edit = edit if isinstance(edit, dict) else {}
    for uri, edits in (edit.get("changes") or {}).items():
        try:
            apply_text_edits(uri, [{"range": _rng(e.get("range")),
                                    "newText": e.get("newText") or ""}
                                   for e in edits or [] if isinstance(e, dict)])
        except GoError as exc:
            raise GoError("failed to apply text edits: %s" % exc)
    for change in edit.get("documentChanges") or []:
        try:
            _apply_document_change(change)
        except GoError as exc:
            raise GoError("failed to apply document change: %s" % exc)


def _apply_document_change(change):
    kind = _document_change_kind(change)
    options = change.get("options") if isinstance(change.get("options"), dict) else None
    if kind == "create":
        path = (change.get("uri") or "")[len("file://"):]
        if options and not options.get("overwrite") and options.get("ignoreIfExists") \
                and os.path.exists(path):
            return
        try:
            open(path, "wb").close()
        except OSError as exc:
            raise GoError("failed to create file: %s" % _go_os_error("open", path, exc))
    elif kind == "delete":
        path = (change.get("uri") or "")[len("file://"):]
        try:
            if options and options.get("recursive"):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as exc:
            raise GoError("failed to delete file: %s" % _go_os_error("remove", path, exc))
    elif kind == "rename":
        old = (change.get("oldUri") or "")[len("file://"):]
        new = (change.get("newUri") or "")[len("file://"):]
        if options and not options.get("overwrite") and os.path.exists(new):
            raise GoError("target file already exists and overwrite is not allowed: %s" % new)
        try:
            os.rename(old, new)
        except OSError as exc:
            raise GoError("failed to rename file: rename %s %s: %s"
                          % (old, new, str(_go_os_error("", "", exc)).split(": ", 1)[-1]))
    else:
        doc = change.get("textDocument") if isinstance(change, dict) else None
        edits = []
        for e in (change.get("edits") if isinstance(change, dict) else None) or []:
            te = _as_text_edit(e)
            if te is None:
                raise GoError("invalid edit type: unknown text edit type: "
                              "protocol.SnippetTextEdit")
            edits.append(te)
        apply_text_edits(((doc or {}).get("uri") or ""), edits)


class MCPLink:
    "This front end's one connection to the daemon, as an ordinary LSP client."

    def __init__(self, key, workspace, argv):
        self.key, self.workspace, self.argv = key, workspace, argv
        self.path = sock_path(key, workspace)
        self.lock = threading.Lock()
        
        
        
        
        
        self.wlock = threading.Lock()
        self.connect_lock = threading.Lock()
        self.sock = None
        self.gen = 0
        self.ready = False
        self.next_id = 0
        self.pending = {}
        self.open_files = {}
        self.diagnostics = {}

    def warm(self):
        try:
            self.ensure()
        except GoError:
            pass

    def ensure(self):
        'Connected and initialized, reconnecting after a drop, or MCPDown.'
        with self.connect_lock:
            with self.lock:
                if self.sock is not None and self.ready:
                    return
                have = self.sock is not None
            if not have:
                s = try_connect(self.path) or spawn_daemon(self.key, self.workspace,
                                                           self.argv, self.path)
                if s is None:
                    raise MCPDown("language server is down: could not reach or start the "
                                  "lspd daemon for %s at %s; retry"
                                  % (self.key, self.workspace))
                with self.lock:
                    self.gen += 1
                    self.sock, self.ready, gen = s, False, self.gen
                    self.open_files.clear()
                threading.Thread(target=self._read, args=(s, gen), daemon=True).start()
            try:
                self._call("initialize", self._init_params())
            except MCPDown:
                raise
            except GoError as exc:
                raise MCPDown("language server is down: initialize failed: %s" % exc)
            self.notify("initialized", {})
            with self.lock:
                self.ready = True

    def _init_params(self):
        ws = self.workspace
        return {
            "processId": os.getpid(),
            "clientInfo": {"name": "mcp-language-server", "version": "0.1.0"},
            "rootPath": ws, "rootUri": "file://" + ws,
            "workspaceFolders": [{"uri": "file://" + ws, "name": ws}],
            "capabilities": {
                "workspace": {"configuration": True,
                              "didChangeConfiguration": {"dynamicRegistration": True},
                              "didChangeWatchedFiles": {"dynamicRegistration": True,
                                                        "relativePatternSupport": True}},
                "textDocument": {
                    "synchronization": {"dynamicRegistration": True, "didSave": True},
                    "completion": {"completionItem": {}},
                    "documentSymbol": {},
                    "codeAction": {"codeActionLiteralSupport": {
                        "codeActionKind": {"valueSet": []}}},
                    "codeLens": {"dynamicRegistration": True},
                    "publishDiagnostics": {"versionSupport": True},
                    "semanticTokens": {"requests": {}, "tokenTypes": [],
                                       "tokenModifiers": [], "formats": []}},
                "window": {}},
            "initializationOptions": {"codelenses": {
                "generate": True, "regenerate_cgo": True, "test": True, "tidy": True,
                "upgrade_dependency": True, "vendor": True, "vulncheck": False}},
        }

    def _write(self, s, obj):
        data = frame(obj)
        with self.wlock:
            s.sendall(data)

    def _call(self, method, params):
        ev, box = threading.Event(), {}
        with self.lock:
            s, gen = self.sock, self.gen
            if s is None:
                raise MCPDown(MCP_LOST)
            self.next_id += 1
            mid = self.next_id
            self.pending[mid] = (ev, box, gen)
        try:
            self._write(s, {"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        except OSError:
            self._lost(s, gen)
        ev.wait()
        if box.get("lost"):
            raise MCPDown(MCP_LOST)
        err = box.get("error")
        if err is not None:
            if not isinstance(err, dict):
                err = {"message": str(err)}
            raise GoError("request failed: %s (code: %s)"
                          % (err.get("message", ""), err.get("code", 0)))
        return box.get("result")

    def call(self, method, params):
        self.ensure()
        return self._call(method, params)

    def notify(self, method, params, gen=None):
        'Send a notification; given `gen`, only on that connection (else _Reconnected).'
        with self.lock:
            s, now = self.sock, self.gen
        if gen is not None and now != gen:
            raise _Reconnected()
        if s is None:
            raise MCPDown(MCP_LOST)
        gen = now
        try:
            self._write(s, {"jsonrpc": "2.0", "method": method, "params": params})
        except OSError:
            self._lost(s, gen)
            raise MCPDown(MCP_LOST)

    def _read(self, s, gen):
        try:
            rfh = s.makefile("rb")
            while True:
                msg = read_message(rfh)
                if msg is None:
                    break
                if isinstance(msg, dict):
                    self._dispatch(s, gen, msg)
        except (OSError, ValueError):
            pass
        self._lost(s, gen)

    def _dispatch(self, s, gen, msg):
        mid, method = msg.get("id"), msg.get("method")
        if method is None and mid is not None:
            with self.lock:
                entry = self.pending.pop(mid, None)
            if entry is not None:
                ev, box, _ = entry
                if msg.get("error") is not None:
                    box["error"] = msg["error"]
                else:
                    box["result"] = msg.get("result")
                ev.set()
            return
        if method is not None and mid is not None:
            threading.Thread(target=self._answer, args=(s, gen, mid, method,
                                                        msg.get("params")),
                             daemon=True).start()
            return
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params")
            if isinstance(params, dict) and isinstance(params.get("uri"), str):
                with self.lock:
                    self.diagnostics[params["uri"]] = params.get("diagnostics") or []

    def _answer(self, s, gen, mid, method, params):
        'A request the daemon routed to this client, answered as the Go bridge did.'
        out = {"jsonrpc": "2.0", "id": mid}
        if method == "workspace/applyEdit":
            try:
                apply_workspace_edit((params or {}).get("edit") if isinstance(params, dict)
                                     else None)
                out["result"] = {"applied": True}
            except GoError as exc:
                out["result"] = {"applied": False, "failureReason": str(exc)}
        elif method == "workspace/configuration":
            out["result"] = [{}]
        elif method == "client/registerCapability":
            out["result"] = None
        else:
            out["error"] = {"code": -32601, "message": "method not found: %s" % method}
        try:
            self._write(s, out)
        except OSError:
            self._lost(s, gen)

    def _lost(self, s, gen):
        with self.lock:
            if self.sock is s and self.gen == gen:
                self.sock, self.ready = None, False
            lost = [(k, v) for k, v in self.pending.items() if v[2] == gen]
            for k, _ in lost:
                del self.pending[k]
        for _, (ev, box, _) in lost:
            box["lost"] = True
            ev.set()
        try:
            s.close()
        except OSError:
            pass

    def open_file(self, filepath):
        'lsp.Client.OpenFile, with Go patch 2: the document is reserved under the lock\n        that checks it, so two concurrent calls on one unopened file send one didOpen.\n        Roslyn aborts on a duplicate (exit 134). The reservation is rolled back when the\n        open does not happen.'
        uri = "file://%s" % filepath
        self.ensure()
        with self.lock:
            if uri in self.open_files:
                return
            self.open_files[uri] = 1
            gen = self.gen

        def unreserve():
            with self.lock:
                if self.gen == gen:
                    self.open_files.pop(uri, None)

        try:
            content = go_read_file(filepath)
        except GoError as exc:
            unreserve()
            raise GoError("error reading file: %s" % exc)
        try:
            self.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": go_language_id(uri), "version": 1,
                "text": content.decode("utf-8", errors="replace")}}, gen)
        except _Reconnected:
            
            
            
            return self.open_file(filepath)
        except GoError:
            unreserve()
            raise


class MCPTools:
    'The six tools, each the Go function of the same job (internal/tools/*.go).'

    def __init__(self, link):
        self.link = link

    def unbuilt(self):
        "swift_unbuilt's sentence for this front end's workspace, or None."
        if self.link.key != "sourcekit-lsp":
            return None
        return swift_unbuilt(self.link.workspace)

    @staticmethod
    def context_lines(default):
        value = os.environ.get("LSP_CONTEXT_LINES", "")
        if re.fullmatch(r"[+-]?[0-9]+", value) and int(value) >= 0:
            return int(value)
        return default

    def symbols(self, query):
        try:
            return decode_workspace_symbols(self.link.call("workspace/symbol",
                                                           {"query": query}))
        except MCPDown:
            raise
        except GoError as exc:
            raise GoError("failed to fetch symbol: %s" % exc)

    def full_definition(self, loc):
        "GetFullDefinition: the innermost symbol's whole lines, and its range."
        uri = loc.get("uri") or ""
        try:
            syms = decode_document_symbols(self.link.call(
                "textDocument/documentSymbol", {"textDocument": {"uri": uri}}))
        except MCPDown:
            raise
        except GoError as exc:
            raise GoError("failed to get document symbols: %s" % exc)
        rng = innermost_range(syms, _rng(loc.get("range"))["start"])
        if rng is None:
            raise GoError("symbol not found")
        try:
            path = _go_path_unescape(uri[len("file://"):] if uri.startswith("file://") else uri)
        except GoError as exc:
            raise GoError("failed to unescape URI: %s" % exc)
        try:
            lines = _go_lines(go_read_file(path))
        except GoError as exc:
            raise GoError("failed to read file: %s" % exc)
        start = {"line": rng["start"]["line"], "character": 0}
        end = dict(rng["end"])
        if end["line"] >= len(lines):
            raise GoError("line number out of range")
        trimmed = lines[end["line"]].strip()
        
        
        if trimmed and trimmed[-1] in "([{<":
            pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
            stack = [trimmed[-1]]
            n, done = end["line"] + 1, False
            while n < len(lines) and not done:
                offset = 0
                for ch in lines[n]:
                    if ch in "([{<":
                        stack.append(ch)
                    elif ch in pairs and stack and stack[-1] == pairs[ch]:
                        stack.pop()
                        if not stack:
                            end = {"line": n, "character": offset + 1}
                            done = True
                            break
                    offset += len(ch.encode("utf-8"))
                n += 1
        if end["line"] >= len(lines):
            raise GoError("end line out of range")
        return ("\n".join(lines[start["line"]:end["line"] + 1]),
                {"uri": uri, "range": {"start": start, "end": end}})

    def lines_to_display(self, locations, total, context):
        show = set()
        for loc in locations:
            ref = _rng(loc.get("range"))["start"]["line"]
            try:
                _, container = self.full_definition(loc)
            except MCPDown:
                raise
            except GoError:
                show.add(ref)
                show.update(i for i in range(ref - context, ref + context + 1)
                            if 0 <= i < total)
                continue
            first = container["range"]["start"]["line"]
            last = container["range"]["end"]["line"]
            show.add(first)
            show.add(ref)
            show.update(i for i in range(ref - context, ref + context + 1)
                        if 0 <= i < total and first <= i <= last)
        return show

    def definition(self, query):
        out = []
        for shape, sym in self.symbols(query):
            name = sym.get("name") or ""
            kind = container = ""
            if shape == "si":
                kind = "Kind: %s\n" % GO_SYMBOL_KINDS.get(sym.get("kind") or 0, "")
                if sym.get("containerName"):
                    container = "Container Name: %s\n" % sym["containerName"]
                if "." in query:
                    if name != query:
                        continue
                elif (sym.get("kind") or 0) == 6:
                    if not (name.endswith("::" + query) or name.endswith("." + query)
                            or name == query):
                        continue
                elif name != query:
                    continue
            elif name != query:
                continue
            loc = _symbol_location(sym)
            try:
                self.link.open_file(_uri_path(loc["uri"]))
                text, full = self.full_definition(loc)
            except MCPDown:
                raise
            except GoError:
                continue
            r = full["range"]
            out.append("---\n\nSymbol: %s\nFile: %s\n%s%sRange: L%d:C%d - L%d:C%d\n\n%s\n" % (
                name, full["uri"][len("file://"):] if full["uri"].startswith("file://")
                else full["uri"], kind, container, _u1(r["start"]["line"]),
                _u1(r["start"]["character"]), _u1(r["end"]["line"]),
                _u1(r["end"]["character"]), add_line_numbers(text, r["start"]["line"] + 1)))
        return "".join(out) if out else "%s not found" % query

    def references(self, query):
        context = self.context_lines(5)
        out = []
        for shape, sym in self.symbols(query):
            name = sym.get("name") or ""
            if "." in query:
                if name != query and name != query.split(".")[-1]:
                    continue
            elif name != query:
                continue
            loc = _symbol_location(sym)
            try:
                self.link.open_file(_uri_path(loc["uri"]))
            except MCPDown:
                raise
            except GoError:
                continue
            try:
                refs = self.link.call("textDocument/references", {
                    "textDocument": {"uri": loc["uri"]}, "position": loc["range"]["start"],
                    "context": {"includeDeclaration": False}})
            except MCPDown:
                raise
            except GoError as exc:
                raise GoError("failed to get references: %s" % exc)
            if refs is None:
                refs = []
            if not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs):
                raise GoError("failed to get references: failed to unmarshal result: json: "
                              "cannot unmarshal into Go value of type []protocol.Location")
            by_file = {}
            for ref in refs:
                by_file.setdefault(ref.get("uri") or "", []).append(ref)
            for uri in sorted(by_file):
                file_refs = by_file[uri]
                path = uri[len("file://"):] if uri.startswith("file://") else uri
                info = "---\n\n%s\nReferences in File: %d\n" % (path, len(file_refs))
                try:
                    lines = _go_lines(go_read_file(path))
                except GoError as exc:
                    out.append(info + "\nError reading file: %s" % exc)
                    continue
                at = ["L%d:C%d" % (_u1(_rng(r.get("range"))["start"]["line"]),
                                   _u1(_rng(r.get("range"))["start"]["character"]))
                      for r in file_refs]
                ranges = convert_lines_to_ranges(
                    self.lines_to_display(file_refs, len(lines), context), len(lines))
                text = info + ("At: " + ", ".join(at) + "\n" if at else "")
                out.append(text + "\n" + format_lines_with_ranges(lines, ranges))
        return "\n".join(out) if out else "No references found for symbol: %s" % query

    def hover(self, path, line, column):
        try:
            self.link.open_file(path)
        except MCPDown:
            raise
        except GoError as exc:
            raise GoError("could not open file: %s" % exc)
        pos = {"line": (line - 1) & 0xFFFFFFFF, "character": (column - 1) & 0xFFFFFFFF}
        try:
            result = self.link.call("textDocument/hover", {
                "textDocument": {"uri": "file://" + path}, "position": pos})
        except MCPDown:
            raise
        except GoError as exc:
            raise GoError("failed to get hover information: %s" % exc)
        value = ""
        if result is not None:
            contents = result.get("contents") if isinstance(result, dict) else None
            if not isinstance(result, dict) or (contents is not None
                                                and not isinstance(contents, dict)):
                raise GoError("failed to get hover information: failed to unmarshal result: "
                              "json: cannot unmarshal into Go struct field Hover.contents of "
                              "type protocol.MarkupContent")
            if isinstance(contents, dict) and isinstance(contents.get("value"), str):
                value = contents["value"]
        if value:
            return value
        text = ""
        try:
            lines = _go_lines(go_read_file(path))
            if pos["line"] + 1 < len(lines):
                text = lines[pos["line"]] + "\n"
        except GoError:
            pass
        return "No hover information available for this position on the following line:\n" + text

    def diagnostics(self, path, context, show_line_numbers):
        context = self.context_lines(context)
        try:
            self.link.open_file(path)
        except MCPDown:
            raise
        except GoError as exc:
            raise GoError("could not open file: %s" % exc)
        
        time.sleep(3)
        uri = "file://" + path
        try:
            self.link.call("textDocument/diagnostic", {"textDocument": {"uri": uri}})
        except MCPDown:
            raise
        except GoError:
            pass
        with self.link.lock:
            diags = [d if isinstance(d, dict) else {}
                     for d in self.link.diagnostics.get(uri) or []]
        if not diags:
            return "No diagnostics found for " + path
        info = "%s\nDiagnostics in File: %d\n" % (path, len(diags))
        summaries, locations = [], []
        for d in diags:
            start = _rng(d.get("range"))["start"]
            summary = "%s at L%d:C%d: %s" % (
                {1: "ERROR", 2: "WARNING", 3: "INFO", 4: "HINT"}.get(d.get("severity") or 0,
                                                                     "UNKNOWN"),
                _u1(start["line"]), _u1(start["character"]), d.get("message") or "")
            code = d.get("code")
            if d.get("source"):
                summary += " (Source: %s" % d["source"]
                if code is not None:
                    summary += ", Code: %s" % go_v(code)
                summary += ")"
            elif code is not None:
                summary += " (Code: %s)" % go_v(code)
            summaries.append(summary)
            locations.append({"uri": uri, "range": d.get("range")})
        try:
            lines = _go_lines(go_read_file(path))
        except GoError as exc:
            return info + "\nError reading file: %s" % exc
        if context > 0:
            show = self.lines_to_display(locations, len(lines), context)
        else:
            show = {_rng(d.get("range"))["start"]["line"] for d in diags}
        result = info + "\n".join(summaries) + "\n"
        if show_line_numbers:
            result += "\n" + format_lines_with_ranges(
                lines, convert_lines_to_ranges(show, len(lines)))
        return result

    def rename(self, path, line, column, new_name):
        try:
            self.link.open_file(path)
        except MCPDown:
            raise
        except GoError as exc:
            raise GoError("could not open file: %s" % exc)
        pos = {"line": (line - 1) & 0xFFFFFFFF, "character": (column - 1) & 0xFFFFFFFF}
        try:
            edit = self.link.call("textDocument/rename", {
                "textDocument": {"uri": "file://" + path}, "position": pos,
                "newName": new_name})
        except MCPDown:
            raise
        except GoError as exc:
            raise GoError("failed to rename symbol: %s" % exc)
        edit = edit if isinstance(edit, dict) else {}
        changes, files = 0, 0
        listed = []
        if isinstance(edit.get("changes"), dict):
            files = len(edit["changes"])
            for uri, edits in edit["changes"].items():
                edits = [e for e in edits or [] if isinstance(e, dict)]
                changes += len(edits)
                listed.append((uri, ", ".join(
                    "L%d:C%d" % (_u1(_rng(e.get("range"))["start"]["line"]),
                                 _u1(_rng(e.get("range"))["start"]["character"]))
                    for e in edits)))
        for change in edit.get("documentChanges") or []:
            if _document_change_kind(change) != "edit":
                continue
            edits = change.get("edits") or []
            parts = []
            for i, e in enumerate(edits):
                te = _as_text_edit(e)
                if te is not None:
                    parts.append("L%d:C%d" % (_u1(te["range"]["start"]["line"]),
                                              _u1(te["range"]["start"]["character"])))
                    if i != len(edits) - 1:
                        parts.append(", ")
            listed.append((((change.get("textDocument") or {}).get("uri") or ""),
                           "".join(parts)))
            files += 1
            changes += len(edits)
        listed.sort(key=lambda c: c[0])
        try:
            apply_workspace_edit(edit)
        except GoError as exc:
            raise GoError("failed to apply changes: %s" % exc)
        if files == 0 or changes == 0:
            return "Failed to rename symbol. 0 occurrences found."
        return ("Successfully renamed symbol to '%s'.\nUpdated %d occurrences across %d "
                "files:\n%s" % (new_name, changes, files,
                                "".join("%s: %s\n" % c for c in listed)))

    def edit_file(self, path, edits):
        try:
            
            
            if not self.unbuilt():
                self.link.open_file(path)
        except MCPDown:
            raise
        except GoError as exc:
            raise GoError("could not open file: %s" % exc)
        removed = added = 0
        for start, end, text in sorted(edits, key=lambda e: e[0]):
            removed += end - start + 1
            added += (text.count("\n") + 1) if text else 0
        text_edits = []
        for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
            try:
                rng = self._line_range(start, end, path)
            except GoError as exc:
                raise GoError("invalid position: %s" % exc)
            text_edits.append({"range": rng, "newText": text})
        try:
            apply_workspace_edit({"changes": {path: text_edits}})
        except GoError as exc:
            raise GoError("failed to apply text edits: %s" % exc)
        return ("Successfully applied text edits. %d lines removed, %d lines added."
                % (removed, added))

    @staticmethod
    def _line_range(start_line, end_line, path):
        'getRange: whole lines, or the end of the last line for a start past EOF.'
        try:
            content = go_read_file(path)
        except GoError as exc:
            raise GoError("failed to read file: %s" % exc)
        ending = b"\r\n" if b"\r\n" in content else b"\n"
        lines = content.split(ending)
        if start_line < 1:
            raise GoError("start line must be >= 1, got %d" % start_line)
        first, last = start_line - 1, end_line - 1
        if first >= len(lines):
            n = len(lines) - 1
            if n >= 0 and lines[n] == b"":
                n -= 1
            n = max(n, 0)
            pos = {"line": n, "character": len(lines[n])}
            return {"start": pos, "end": dict(pos)}
        if last >= len(lines):
            last = len(lines) - 1
        if last < 0:
            raise RuntimeError("runtime error: index out of range [%d]" % last)
        return {"start": {"line": first, "character": 0},
                "end": {"line": last, "character": len(lines[last])}}


def _mcp_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _mcp_definition(tools, a):
    if not isinstance(a.get("symbolName"), str):
        return "symbolName must be a string", True
    try:
        return tools.definition(a["symbolName"]), False
    except GoError as exc:
        return "failed to get definition: %s" % exc, True


def _mcp_references(tools, a):
    if not isinstance(a.get("symbolName"), str):
        return "symbolName must be a string", True
    try:
        return tools.references(a["symbolName"]), False
    except GoError as exc:
        return "failed to find references: %s" % exc, True


def _mcp_diagnostics(tools, a):
    if not isinstance(a.get("filePath"), str):
        return "filePath must be a string", True
    
    show = a.get("showLineNumbers")
    try:
        return tools.diagnostics(a["filePath"], 5, show if isinstance(show, bool) else True), False
    except GoError as exc:
        return "failed to get diagnostics: %s" % exc, True


def _mcp_hover(tools, a):
    if not isinstance(a.get("filePath"), str):
        return "filePath must be a string", True
    if not _mcp_number(a.get("line")):
        return "line must be a number", True
    if not _mcp_number(a.get("column")):
        return "column must be a number", True
    try:
        return tools.hover(a["filePath"], int(a["line"]), int(a["column"])), False
    except GoError as exc:
        return "failed to get hover information: %s" % exc, True


def _mcp_rename(tools, a):
    if not isinstance(a.get("filePath"), str):
        return "filePath must be a string", True
    if not isinstance(a.get("newName"), str):
        return "newName must be a string", True
    if not _mcp_number(a.get("line")):
        return "line must be a number", True
    if not _mcp_number(a.get("column")):
        return "column must be a number", True
    try:
        return tools.rename(a["filePath"], int(a["line"]), int(a["column"]),
                            a["newName"]), False
    except GoError as exc:
        return "failed to rename symbol: %s" % exc, True


def _mcp_edit_file(tools, a):
    if not isinstance(a.get("filePath"), str):
        return "filePath must be a string", True
    if "edits" not in a:
        return "edits is required", True
    if not isinstance(a["edits"], list):
        return "edits must be an array", True
    edits = []
    for e in a["edits"]:
        if not isinstance(e, dict):
            return "each edit must be an object", True
        if not _mcp_number(e.get("startLine")):
            return "startLine must be a number", True
        if not _mcp_number(e.get("endLine")):
            return "endLine must be a number", True
        text = e.get("newText")
        edits.append((int(e["startLine"]), int(e["endLine"]),
                      text if isinstance(text, str) else ""))
    try:
        return tools.edit_file(a["filePath"], edits), False
    except GoError as exc:
        return "failed to apply edits: %s" % exc, True


MCP_HANDLERS = {"definition": _mcp_definition, "references": _mcp_references,
                "diagnostics": _mcp_diagnostics, "hover": _mcp_hover,
                "rename_symbol": _mcp_rename, "edit_file": _mcp_edit_file}


def mcp_serve(tools):
    'Newline-delimited JSON-RPC on stdio, answered as mcp-go v0.25 answered it.'
    out_lock = threading.Lock()
    stdout = sys.stdout.buffer

    def emit(obj):
        data = json.dumps(obj).encode() + b"\n"
        with out_lock:
            try:
                stdout.write(data)
                stdout.flush()
            except (OSError, ValueError):
                pass

    def reply(mid, result):
        emit({"jsonrpc": "2.0", "id": mid, "result": result})

    def fail(mid, code, message):
        emit({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

    def call_tool(mid, params):
        name = params.get("name")
        handler = MCP_HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            fail(mid, -32602, "tool '%s' not found: tool not found"
                 % (name if isinstance(name, str) else ""))
            return
        args = params.get("arguments")
        
        
        gate = tools.unbuilt() if name != "edit_file" else None
        if gate:
            reply(mid, {"content": [{"type": "text", "text": gate}], "isError": True})
            return
        try:
            text, is_error = handler(tools, args if isinstance(args, dict) else {})
        except Exception as exc:
            fail(mid, -32603, "panic recovered in %s tool handler: %s" % (name, exc))
            return
        result: dict = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        reply(mid, result)

    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            fail(None, -32700, "Parse error")
            continue
        mid, method = msg.get("id"), msg.get("method")
        if msg.get("jsonrpc") != "2.0":
            fail(mid, -32600, "Invalid JSON-RPC version")
            continue
        if mid is None or method is None:
            continue              
        if method == "initialize":
            reply(mid, {"protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {"logging": {}, "tools": {}},
                        "serverInfo": dict(MCP_SERVER_INFO)})
        elif method in ("ping", "logging/setLevel"):
            reply(mid, {})
        elif method == "tools/list":
            reply(mid, {"tools": MCP_TOOLS})
        elif method == "tools/call":
            params = msg.get("params")
            threading.Thread(target=call_tool,
                             args=(mid, params if isinstance(params, dict) else {}),
                             daemon=True).start()
        else:
            fail(mid, -32601, "Method %s not found" % method)




def mcp_server_command(key):
    'The language server a key runs, as lsp-mcp.sh defaulted it.'
    if key == "kotlin-lsp":
        
        
        
        
        
        
        
        ils = os.path.join(HOME, ".local", "opt", "intellij-server", "current")
        cmd = [os.path.join(ils, "bin", "intellij-server"), "--stdio", "--data-sharing",
               "none"]
        try:
            with open(os.path.join(ils, "EULA.txt"), "rb") as fh:
                cmd += ["--eula", hashlib.sha256(fh.read()).hexdigest()[:16]]
        except OSError:
            pass
        return cmd
    return {"tsgo": ["tsgo", "--lsp", "--stdio"],
            "basedpyright": ["basedpyright-langserver", "--stdio"],
            "typescript-language-server": ["typescript-language-server", "--stdio"],
            "csharp-ls": ["csharp-ls"],
            "rust-analyzer": ["rust-analyzer"],
            "sourcekit-lsp": ["sourcekit-lsp"]}.get(key, [key])


def mcp_resolve_workspace():
    'The main checkout, never a worktree: a worktree is a different absolute path, so a\n    cold index to every server and, for Kotlin, a corrupted shared cache\n    (Kotlin/kotlin-lsp#178). AGENT_LSP_WORKSPACE overrides.'
    override = os.environ.get("AGENT_LSP_WORKSPACE", "")
    if override:
        return override
    cwd = os.getcwd()
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], capture_output=True, text=True,
                                timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        common = ""
    if not common:
        return cwd
    main = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    return main if os.path.isdir(main) else cwd


def _build_server_field(config, name):
    'One string field of buildServer.json, or "".\n\n    Parsed as JSON, so a duplicated key keeps its last value as every JSON reader does.\n    A line matcher that joins every match would turn two "build_root" lines into one\n    path that no directory has (test-lsp-no-compiler-flags-edges.py). A file that is\n    not JSON is read by line, first matching line only.'
    try:
        with open(config, errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        value = data.get(name)
        return value if isinstance(value, str) else ""
    pat = re.compile(r'.*"%s"[ \t\f\v\r]*:[ \t\f\v\r]*"([^"]*)".*' % re.escape(name))
    for line in text.split("\n"):
        m = pat.match(line)
        if m:
            return m.group(1)
    return ""


def swift_build_command(projects, scheme):
    'The one build that leaves xcode-build-server compiler flags to derive.'
    scheme = scheme or os.path.basename(projects[0])[:-len(".xcodeproj")]
    return ("xcodebuild build -project %s -scheme '%s' -destination "
            "'generic/platform=iOS Simulator'" % (os.path.basename(projects[0]), scheme))


def swift_unbuilt(ws):
    'The build-once sentence when `ws` has no compiler flags for sourcekit-lsp, else None.\n\n    No silent not-found. xcode-build-server derives every file\'s flags from the newest\n    .xcactivitylog, so on a project that was never built sourcekit-lsp answers "not\n    found" for every symbol. Every navigation answer is this sentence in that state,\n    the canary records it, and lsp-failure-tripwire stops the session.\n    Checked per call and cheap (a glob and one small read), so a build clears it without\n    restarting the front end. test-lsp-no-compiler-flags.py.'
    import glob
    projects = sorted(glob.glob(os.path.join(ws, "*.xcodeproj")))
    config = os.path.join(ws, "buildServer.json")
    if not projects or not os.path.exists(config):
        return None
    root = _build_server_field(config, "build_root")
    if not root or not os.path.isdir(root):
        return None
    if glob.glob(os.path.join(root, "Logs", "Build", "*.xcactivitylog")):
        return None
    fix = swift_build_command(projects, _build_server_field(config, "scheme"))
    return ("no compiler flags: build %s once (%s), then lspd.py --restart --key sourcekit-lsp "
            "--workspace %s. buildServer.json binds xcode-build-server to %s, which holds no "
            "build logs, so sourcekit-lsp has no compiler flags and would answer 'not found' "
            "for every symbol."
            % (os.path.basename(projects[0])[:-len(".xcodeproj")], fix, ws, root))


def mcp_swift_binding(ws, key):
    "Keep sourcekit-lsp's build-server binding honest before the server starts."
    import glob
    projects = sorted(glob.glob(os.path.join(ws, "*.xcodeproj")))
    
    
    if projects and not os.environ.get("DEVELOPER_DIR"):
        try:
            selected = subprocess.run(["xcode-select", "-p"], capture_output=True,
                                      text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            selected = ""
        if "CommandLineTools" in selected:
            apps = (os.environ.get("LSPD_XCODE_APPS")
                    or "/Applications/Xcode-beta.app:/Applications/Xcode.app")
            for app in apps.split(":"):
                dev = os.path.join(app, "Contents", "Developer")
                if app and os.access(os.path.join(dev, "usr", "bin", "xcodebuild"), os.X_OK):
                    os.environ["DEVELOPER_DIR"] = dev
                    break
    if not projects:
        return
    if not shutil.which("xcode-build-server"):
        sys.stderr.write("lspd --mcp: %s has an .xcodeproj but xcode-build-server is not "
                         "installed (brew install xcode-build-server).\n" % ws)
        return
    config = os.path.join(ws, "buildServer.json")

    def field(name):
        return _build_server_field(config, name)

    
    
    
    exists = os.path.exists(config)
    root = field("build_root") if exists else ""
    import glob as _glob
    logs = bool(root) and bool(_glob.glob(os.path.join(root, "Logs", "Build",
                                                       "*.xcactivitylog")))
    if not exists or not os.path.isdir(root):
        scheme = field("scheme") if exists else ""
        sys.stderr.write("lspd --mcp: buildServer.json missing or pointing at a vanished "
                         "DerivedData root, rebinding.\n")
        sys.stderr.flush()
        cmd = (["xcode-build-server", "config", "-project"]
               + ["./" + os.path.basename(p) for p in projects]
               + (["-scheme", scheme] if scheme else []))
        try:
            rc = subprocess.run(cmd, cwd=ws, stdout=sys.stderr, stderr=sys.stderr,
                                env=dict(os.environ, PWD=ws), timeout=120).returncode
        except (OSError, subprocess.SubprocessError):
            rc = 1
        if rc != 0:
            sys.stderr.write("lspd --mcp: rebind failed; app-target symbols will not "
                             "resolve.\n")
    elif not logs:
        
        
        
        fix = swift_build_command(projects, field("scheme"))
        sys.stderr.write("lspd --mcp: buildServer.json points at a DerivedData root with NO "
                         "build logs, so every symbol will answer 'not found'.\n"
                         "  Rebinding cannot help; there are no flags to derive. Build "
                         "once: %s\n" % fix)
        state = os.path.join(hp.state_dir(HOME), "health", "lsp")
        rec = {"ts": int(time.time()), "cwd": ws, "server": key, "ok": False,
               "detail": ("buildServer.json is bound to a DerivedData root that contains NO "
                          "build logs, so xcode-build-server has no compiler flags to hand "
                          "the language server and EVERY symbol resolves to 'not found'. A "
                          "rebind cannot fix this - the project has never been built (or "
                          "was cleaned) on this machine. Build it once: " + fix)}
        try:
            os.makedirs(state, exist_ok=True)
            path = os.path.join(state, "%s-%s.json" % (ws.replace("/", "-"), key))
            with open(path + ".tmp", "w") as fh:
                json.dump(rec, fh, indent=2)
            os.replace(path + ".tmp", path)
        except OSError:
            pass


def mcp_preflight(key, workspace=None, rest=None):
    '(plan, 0), or (None, 127) with the reason on stderr.'
    
    
    path = os.environ.get("PATH", "")
    
    
    homebrew = os.environ.get("LSPD_HOMEBREW_BIN") or "/opt/homebrew/bin"
    for d in (os.path.join(HOME, ".local", "bin"), os.path.join(HOME, ".dotnet", "tools"),
              homebrew):
        if ":%s:" % d not in ":%s:" % path and os.path.isdir(d):
            path = d + ":" + path
    
    
    
    if key == "csharp-ls":
        for root in ("/usr/local/share/dotnet", os.path.join(HOME, ".dotnet")):
            if os.path.isdir(os.path.join(root, "shared", "Microsoft.NETCore.App")):
                path = root + ":" + path
                break
    os.environ["PATH"] = path
    ws = workspace or mcp_resolve_workspace()
    argv = list(rest or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    
    
    if not argv:
        argv = mcp_server_command(key)
        if not shutil.which(argv[0], path=path):
            msg = ["lspd --mcp: language server '%s' is not on PATH, so there is no code "
                   "intelligence here." % argv[0]]
            if argv[0] == "sourcekit-lsp":
                msg.append("  Ships with Xcode and the Command Line Tools; Mac-only.")
            elif argv[0] == "tsgo":
                msg.append("  Install:  python3 ~/.agent-context/global/scripts/"
                           "node-tools-sync.py")
            elif argv[0] == "basedpyright-langserver":
                msg.append("  Install:  uv tool install basedpyright")
            elif argv[0] == "rust-analyzer":
                msg.append("  Install:  brew install rust-analyzer (Mac); on Linux, chezmoi "
                           "apply fetches the upstream release.")
            elif argv[0].endswith("/intellij-server"):
                msg += ["  Install:  python3 ~/.agent-context/global/scripts/"
                        "refresh-intellij-server.py",
                        "  It pulls a live build from the VS Code marketplace extension",
                        "  JetBrains.intellij-server. If it exits 3, a new EULA is waiting on "
                        "user:",
                        "  read ~/.local/opt/intellij-server/EULA-CHANGED-READ-ME.txt and ASK "
                        "him."]
            sys.stderr.write("\n".join(msg) + "\n")
            return None, 127
    if key == "sourcekit-lsp":
        mcp_swift_binding(ws, key)
    return {"key": key, "workspace": ws, "command": argv, "path": path,
            "developer_dir": os.environ.get("DEVELOPER_DIR")}, 0


def _mcp_watch_parent():
    "Exit when the harness does. Claude Desktop does not kill an MCP server's children\n    (the Go bridge's own note), and a front end with no parent serves nobody."
    parent = os.getppid()
    while True:
        time.sleep(1)
        now = os.getppid()
        if now != parent and (now == 1 or parent == 1):
            os._exit(0)


def mcp_main(args):
    if not args.key:
        sys.stderr.write("lspd --mcp: --key is required\n")
        return 2
    plan, rc = mcp_preflight(args.key, args.workspace, args.rest)
    if plan is None:
        return rc
    if args.preflight:
        print(json.dumps(plan))
        return 0
    try:
        os.chdir(plan["workspace"])
    except OSError as exc:
        sys.stderr.write("lspd --mcp: workspace %s: %s\n" % (plan["workspace"], exc))
        return 127
    link = MCPLink(args.key, plan["workspace"], plan["command"])
    
    
    if not (args.key == "sourcekit-lsp" and swift_unbuilt(plan["workspace"])):
        threading.Thread(target=link.warm, daemon=True).start()
    threading.Thread(target=_mcp_watch_parent, daemon=True).start()
    mcp_serve(MCPTools(link))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attach", action="store_true")
    ap.add_argument("--mcp", action="store_true",
                    help="serve the six mcp__*-lsp__* tools on stdio as a daemon client")
    ap.add_argument("--preflight", action="store_true",
                    help="with --mcp: print the launch plan as JSON and exit")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--resync", action="store_true")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--upgrade", action="store_true",
                    help="retire a daemon so the next attach respawns it on the "
                         "lspd.py now on disk (a daemon is pinned to its spawn build)")
    ap.add_argument("--force", action="store_true",
                    help="with --upgrade: retire it even with sessions attached")
    ap.add_argument("--key")
    ap.add_argument("--workspace")
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    if args.mcp:
        return mcp_main(args)

    if args.status:
        return status(args.key)

    if args.resync:
        return resync_all([os.path.abspath(p) for p in args.rest if p != "--"],
                          args.key)

    if args.upgrade:
        if not args.key:
            ap.error("--upgrade requires --key (and --workspace when a server has "
                     "daemons for more than one workspace): retiring a daemon costs "
                     "a cold index, so it is never swept across daemons")
        return upgrade_all(args.key, args.workspace, args.force)

    if args.restart:
        if not args.key:
            ap.error("--restart requires --key (and --workspace when a server has "
                     "daemons for more than one workspace): a restart costs a cold "
                     "index, so it is never swept across daemons")
        return restart_all(args.key, args.workspace)

    argv = [a for a in args.rest if a != "--"]
    if not args.key or not args.workspace or not argv:
        ap.error("--key, --workspace and a `-- <server> [args]` command are required")

    os.makedirs(LOGS, exist_ok=True)
    if args.daemon:
        d = Daemon(args.key, args.workspace, argv)
        return d.run(sock_path(args.key, args.workspace))
    return attach(args.key, args.workspace, argv)


if __name__ == "__main__":
    sys.exit(main())
