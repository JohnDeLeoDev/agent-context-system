#!/usr/bin/env python3
'Test battery for `lspd.py --mcp`, the MCP front end that replaced the Go bridge.\n\nRun: python3 test-lspd-mcp.py          (--dump-goldens prints the captured answers)'
import base64
import hashlib
import importlib.util
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
LSPD = os.path.join(HERE, "lspd.py")
MOCK = os.path.join(HERE, "mockls.py")
TRIPWIRE = os.path.join(os.path.dirname(HERE), "hooks", "lsp-failure-tripwire.py")


KEY = "m%d" % os.getpid()     
TMPDIRS = []
REAP_PATTERNS = ["lspd.py --daemon --key " + KEY]



SETTLE = 1.5

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if detail and not cond else ""))


def reap():
    for pat in REAP_PATTERNS:
        subprocess.run(["pkill", "-f", pat], capture_output=True)
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)





FILES = {
    "pkg/__init__.py": "",
    "pkg/shapes.py": (
        "import math\n"
        "\n"
        "\n"
        "class Shape:\n"
        '    """A shape."""\n'
        "\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
        "\n"
        "    def area(self):\n"
        "        return 0\n"
        "\n"
        "    def describe(self):\n"
        '        return "%s has area %s" % (self.name, self.area())\n'
        "\n"
        "\n"
        "def make_shape(name):\n"
        "    return Shape(name)\n"
        "\n"
        "\n"
        "CONSTANTS = {\n"
        '    "pi": math.pi,\n'
        '    "e": math.e,\n'
        "}\n"
    ),
    "pkg/nested.py": (
        "class Outer:\n"
        "    def inner(self):\n"
        "        pass\n"
        "\n"
        "    VALUE = 1\n"
    ),
    "pkg/edge.py": (
        "def wider():\n"
        "    pass\n"
        "\n"
        "def narrow():\n"
        "    pass\n"
        "    pass\n"
    ),
    "app/main.py": (
        "from pkg.shapes import Shape, make_shape\n"
        "\n"
        "\n"
        "def run():\n"
        '    """Build two shapes."""\n'
        '    first = Shape("a")\n'
        "    total = first.area()\n"
        '    second = make_shape("b")\n'
        "    return total, second\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    run()\n"
    ),
    "app/util.py": (
        "from pkg import shapes\n"
        'DEFAULT = shapes.Shape("unit")\n'
        + "".join("# filler %d\n" % i for i in range(2, 15))
        + 'LAST = shapes.Shape("last")\n'
    ),
    "app/broken.py": (
        "import os\n"
        "\n"
        "\n"
        "def check():\n"
        "    return x\n"
        "\n"
        "\n"
        "def other():\n"
        "    if False:\n"
        "        return 1\n"
        "    return 2\n"
    ),
    "app/clean.py": "VALUE = 1\n",
    "notes.txt": "alpha\nbeta\ngamma\ndelta\n",
    "crlf.txt": "one\r\ntwo\r\nthree\r\n",
}


def R(sl, sc, el, ec):
    return {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}}


def U(rel):
    return "file://{WS}/" + rel


def SI(name, kind, rel, rng, container=None):
    out = {"name": name, "kind": kind, "location": {"uri": U(rel), "range": R(*rng)}}
    if container:
        out["containerName"] = container
    return out


def DS(name, kind, rng, sel, children=None):
    out = {"name": name, "kind": kind, "range": R(*rng), "selectionRange": R(*sel)}
    if children:
        out["children"] = children
    return out


def LOC(rel, rng):
    return {"uri": U(rel), "range": R(*rng)}


def ERR(code, message):
    return {"$error": {"code": code, "message": message}}


SHAPES, NESTED, EDGE = "pkg/shapes.py", "pkg/nested.py", "pkg/edge.py"
MAIN, UTIL, BROKEN, CLEAN = "app/main.py", "app/util.py", "app/broken.py", "app/clean.py"

SCENARIO = {
    "results": {
        "workspace/symbol": {"$by": "query", "default": [], "cases": {
            "Shape": [SI("Shape", 5, SHAPES, (3, 6, 3, 11)),
                      SI("ShapeFactory", 5, SHAPES, (3, 6, 3, 11))],
            "area": [SI("area", 6, SHAPES, (9, 8, 9, 12), "Shape"),
                     SI("area_total", 12, SHAPES, (9, 8, 9, 12))],
            "Shape.area": [SI("area", 6, SHAPES, (9, 8, 9, 12), "Shape")],
            "Shape.describe": [SI("describe", 6, SHAPES, (12, 8, 12, 16), "Shape"),
                               SI("Shape.describe", 6, SHAPES, (12, 8, 12, 16))],
            "describe": [SI("Shape.describe", 6, SHAPES, (12, 8, 12, 16)),
                         SI("describe_all", 12, SHAPES, (12, 8, 12, 16))],
            "make_shape": [{"name": "make_shape", "kind": 12,
                            "location": {"uri": U(SHAPES)}}],
            "CONSTANTS": [SI("CONSTANTS", 14, SHAPES, (20, 0, 20, 9))],
            "Missing": [],
            "Ghost": [SI("Ghost", 5, "pkg/ghost.py", (0, 0, 0, 5))],
            "inner": [SI("inner", 6, NESTED, (1, 8, 1, 13), "Outer")],
            "narrow": [SI("narrow", 12, EDGE, (3, 4, 3, 10))],
            "Nulls": None,
            "Boom": ERR(-32603, "boom"),
        }},
        "textDocument/documentSymbol": {"$by": "doc", "default": [], "cases": {
            "shapes.py": [
                DS("Shape", 5, (3, 0, 13, 58), (3, 6, 3, 11), [
                    DS("__init__", 9, (6, 4, 7, 24), (6, 8, 6, 16)),
                    DS("area", 6, (9, 4, 10, 16), (9, 8, 9, 12)),
                    DS("describe", 6, (12, 4, 13, 58), (12, 8, 12, 16)),
                ]),
                DS("make_shape", 12, (16, 0, 17, 22), (16, 4, 16, 14)),
                DS("CONSTANTS", 14, (20, 0, 20, 13), (20, 0, 20, 9)),
            ],
            
            "nested.py": [DS("Outer", 5, (0, 0, 2, 12), (0, 6, 0, 11), [
                DS("inner", 6, (1, 4, 4, 13), (1, 8, 1, 13))])],
            
            "edge.py": [SI("wider", 12, EDGE, (0, 0, 3, 10)),
                        SI("narrow", 12, EDGE, (3, 0, 5, 8))],
            "main.py": [DS("run", 12, (3, 0, 8, 24), (3, 4, 3, 7))],
            "broken.py": [DS("check", 12, (3, 0, 4, 12), (3, 4, 3, 9)),
                          DS("other", 12, (7, 0, 10, 12), (7, 4, 7, 9))],
        }},
        "textDocument/references": {"$by": "pos", "default": [], "cases": {
            "shapes.py:3:6": [LOC(MAIN, (0, 23, 0, 28)), LOC(MAIN, (5, 12, 5, 17)),
                              LOC(UTIL, (1, 17, 1, 22)), LOC(UTIL, (15, 14, 15, 19)),
                              LOC(SHAPES, (17, 11, 17, 16)), LOC("app/gone.py", (0, 0, 0, 1))],
            "shapes.py:9:8": [LOC(MAIN, (6, 18, 6, 22))],
            "shapes.py:20:0": [],
            "nested.py:1:8": None,
            "edge.py:3:4": ERR(-32603, "boom"),
        }},
        "textDocument/hover": {"$by": "pos", "default": None, "cases": {
            "main.py:5:12": {"contents": {"kind": "markdown",
                                          "value": "```python\nclass Shape(name: str)\n```\n---\nA shape."}},
            "main.py:4:4": {"contents": {"kind": "plaintext", "value": ""}},
            "shapes.py:9:8": ERR(-32800, "cancelled"),
        }},
        "textDocument/rename": {"$by": "pos", "default": {}, "cases": {
            "shapes.py:3:6": {"changes": {
                U(SHAPES): [{"range": R(3, 6, 3, 11), "newText": "Polygon"},
                            {"range": R(17, 11, 17, 16), "newText": "Polygon"}],
                U(MAIN): [{"range": R(0, 23, 0, 28), "newText": "Polygon"},
                          {"range": R(5, 12, 5, 17), "newText": "Polygon"}],
            }},
            "main.py:3:4": {"documentChanges": [{
                "textDocument": {"uri": U(MAIN), "version": 1},
                "edits": [{"range": R(3, 4, 3, 7), "newText": "start"},
                          {"range": R(12, 4, 12, 7), "newText": "start"}]}]},
            "clean.py:0:0": {"changes": {
                U(CLEAN): [{"range": R(0, 0, 0, 5), "newText": "A"},
                           {"range": R(0, 2, 0, 6), "newText": "B"}]}},
            "edge.py:3:4": ERR(-32602, "cannot rename here"),
        }},
    },
    "on": {
        "textDocument/didOpen": {"$by": "doc", "default": [], "cases": {
            "broken.py": [{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                           "params": {"uri": "{URI}", "diagnostics": [
                {"range": R(4, 11, 4, 12), "severity": 1, "source": "basedpyright",
                 "code": "reportUndefinedVariable", "message": '"x" is not defined'},
                {"range": R(0, 7, 0, 9), "severity": 2, "code": 42,
                 "message": 'Import "os" is not accessed'},
                {"range": R(9, 8, 9, 16), "severity": 4, "message": "Code is unreachable"},
                {"range": R(10, 4, 10, 10), "message": "no severity"},
            ]}}],
            "clean.py": [{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                          "params": {"uri": "{URI}", "diagnostics": []}}],
        }},
    },
}


def call(name, tool, args, snapshot=False):
    return {"name": name, "method": "tools/call",
            "params": {"name": tool, "arguments": args}, "snapshot": snapshot}


INIT = {"name": "initialize", "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "golden", "version": "1"}}}
INITIALIZED = {"name": "initialized", "method": "notifications/initialized", "notify": True}

READS = [
    {"name": "tools/list", "method": "tools/list", "params": {}},
    {"name": "ping", "method": "ping"},
    {"name": "ping string id", "method": "ping", "id": "abc"},
    {"name": "unknown method", "method": "foo/bar", "params": {}},
    {"name": "malformed line", "raw": "{not json"},
    call("unknown tool", "nope", {}),
    {"name": "call without arguments", "method": "tools/call", "params": {"name": "definition"}},
] + [call("definition " + q, "definition", {"symbolName": q}) for q in (
    "Shape", "area", "Shape.area", "Shape.describe", "describe", "make_shape", "CONSTANTS",
    "Missing", "Ghost", "inner", "narrow", "Nulls", "Boom")] + [
    call("definition no symbolName", "definition", {}),
] + [call("references " + q, "references", {"symbolName": q}) for q in (
    "Shape", "area", "Shape.area", "CONSTANTS", "inner", "narrow", "Missing", "Nulls", "Boom")] + [
    call("references wrong type", "references", {"symbolName": 5}),
    call("hover markdown", "hover", {"filePath": "{WS}/app/main.py", "line": 6, "column": 13}),
    call("hover empty value", "hover", {"filePath": "{WS}/app/main.py", "line": 5, "column": 5}),
    call("hover null past end", "hover", {"filePath": "{WS}/app/main.py", "line": 31, "column": 1}),
    call("hover error", "hover", {"filePath": "{WS}/pkg/shapes.py", "line": 10, "column": 9}),
    call("hover relative path", "hover", {"filePath": "app/main.py", "line": 6, "column": 13}),
    call("hover missing file", "hover", {"filePath": "{WS}/app/nope.py", "line": 1, "column": 1}),
    call("hover line string", "hover", {"filePath": "{WS}/app/main.py", "line": "6", "column": 1}),
    call("hover no column", "hover", {"filePath": "{WS}/app/main.py", "line": 6}),
    call("diagnostics broken", "diagnostics", {"filePath": "{WS}/app/broken.py"}),
    call("diagnostics no line numbers", "diagnostics",
         {"filePath": "{WS}/app/broken.py", "showLineNumbers": False}),
    call("diagnostics contextLines ignored", "diagnostics",
         {"filePath": "{WS}/app/broken.py", "contextLines": True}),
    call("diagnostics clean", "diagnostics", {"filePath": "{WS}/app/clean.py"}),
    call("diagnostics missing file", "diagnostics", {"filePath": "{WS}/app/nope.py"}),
    call("diagnostics no filePath", "diagnostics", {}),
]

WRITES = [
    call("rename changes", "rename_symbol",
         {"filePath": "{WS}/pkg/shapes.py", "line": 4, "column": 7, "newName": "Polygon"}, True),
    call("rename documentChanges", "rename_symbol",
         {"filePath": "{WS}/app/main.py", "line": 4, "column": 5, "newName": "start"}, True),
    call("rename nothing", "rename_symbol",
         {"filePath": "{WS}/app/main.py", "line": 1, "column": 1, "newName": "x"}, True),
    call("rename overlapping", "rename_symbol",
         {"filePath": "{WS}/app/clean.py", "line": 1, "column": 1, "newName": "x"}, True),
    call("rename error", "rename_symbol",
         {"filePath": "{WS}/pkg/edge.py", "line": 4, "column": 5, "newName": "x"}, True),
    call("rename no newName", "rename_symbol",
         {"filePath": "{WS}/pkg/edge.py", "line": 4, "column": 5}),
    call("edit replace and delete", "edit_file", {"filePath": "{WS}/notes.txt", "edits": [
        {"startLine": 2, "endLine": 3, "newText": "B\nC"},
        {"startLine": 4, "endLine": 4, "newText": ""}]}, True),
    call("edit past end", "edit_file", {"filePath": "{WS}/notes.txt", "edits": [
        {"startLine": 99, "endLine": 99, "newText": "omega"}]}, True),
    call("edit start zero", "edit_file", {"filePath": "{WS}/notes.txt", "edits": [
        {"startLine": 0, "endLine": 1, "newText": "z"}]}, True),
    call("edit crlf", "edit_file", {"filePath": "{WS}/crlf.txt", "edits": [
        {"startLine": 1, "endLine": 1, "newText": "ONE"}]}, True),
    call("edit relative path", "edit_file", {"filePath": "notes.txt", "edits": [
        {"startLine": 1, "endLine": 1, "newText": "ALPHA"}]}, True),
    call("edit missing file", "edit_file", {"filePath": "{WS}/missing.txt", "edits": [
        {"startLine": 1, "endLine": 1, "newText": "x"}]}, True),
    call("edit edits not array", "edit_file", {"filePath": "{WS}/notes.txt", "edits": "nope"}),
    call("edit edit not object", "edit_file", {"filePath": "{WS}/notes.txt", "edits": ["x"]}),
    call("edit no startLine", "edit_file", {"filePath": "{WS}/notes.txt", "edits": [{"endLine": 1}]}),
    call("edit endLine string", "edit_file",
         {"filePath": "{WS}/notes.txt", "edits": [{"startLine": 1, "endLine": "1"}]}),
    call("edit no filePath", "edit_file", {"edits": []}),
    call("edit no edits", "edit_file", {"filePath": "{WS}/notes.txt"}),
]

SESSIONS = [
    {"name": "main", "env": {}, "cases": [INIT, INITIALIZED] + READS + WRITES},
    {"name": "context1", "env": {"LSP_CONTEXT_LINES": "1"}, "cases": [
        INIT, INITIALIZED,
        call("references Shape, context 1", "references", {"symbolName": "Shape"}),
        call("diagnostics broken, context 1", "diagnostics", {"filePath": "{WS}/app/broken.py"})]},
    {"name": "context0", "env": {"LSP_CONTEXT_LINES": "0"}, "cases": [
        INIT, INITIALIZED,
        call("references Shape, context 0", "references", {"symbolName": "Shape"}),
        call("diagnostics broken, context 0", "diagnostics", {"filePath": "{WS}/app/broken.py"})]},
    {"name": "handshake", "env": {}, "cases": [
        {"name": "tools/list before initialize", "method": "tools/list", "params": {}},
        {"name": "initialize unknown version", "method": "initialize", "params": {
            "protocolVersion": "1999-01-01", "capabilities": {},
            "clientInfo": {"name": "golden", "version": "1"}}},
        {"name": "ping after", "method": "ping"}]},
]







GOLDENS_BLOB = """
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
"""


def goldens():
    return json.loads(zlib.decompress(base64.b64decode("".join(GOLDENS_BLOB.split()))))



TAGS = {
    "tools/list": " [c1 same tools, schemas, descriptions]",
    "diagnostics contextLines ignored": " [c1 quirk kept]",
    "definition area": " [c8 a method, not its class]",
    "definition inner": " [c8 a child past its parent's end]",
    "definition narrow": " [c8 overlapping siblings: the smaller]",
    "unknown method": " [c12 -32601]",
    "malformed line": " [c12 parse error]",
}

GOLD_HOVER = "```python\nclass Shape(name: str)\n```\n---\nA shape."




def mk_home():
    h = os.path.realpath(tempfile.mkdtemp(prefix="l"))
    TMPDIRS.append(h)
    return h


def build_ws(root):
    for rel, text in FILES.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", newline="") as fh:
            fh.write(text)
    return os.path.realpath(root)


def server_wrapper(home):
    "mockls behind a name of its own. The daemon's reap_orphans kills parentless\n    processes whose argv[0] is the server's name, so a mock must never be named python."
    p = os.path.join(home, "bin", "mockls-srv")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, MOCK))
    os.chmod(p, 0o755)
    return p


def base_env(home, **extra):
    env = dict(os.environ)
    for k in ("AGENT_LSP_WORKSPACE", "LSP_CONTEXT_LINES", "DEVELOPER_DIR"):
        env.pop(k, None)
    env.update({"HOME": home, "LSPD_IDLE_EXIT": "60", "LSPD_CANARY": "0",
                "LSPD_SEED_SETTLE": "0.2", "LSPD_GIT_POLL": "3600"})
    env.update(extra)
    return env


def scenario_env(home, **extra):
    scen = os.path.join(home, "scenario.json")
    with open(scen, "w") as fh:
        json.dump(SCENARIO, fh)
    return base_env(home, MOCKLS_SCENARIO=scen, MOCKLS_JOURNAL=os.path.join(home, "journal"),
                    **extra)


def mcp_argv(key, ws, server, *extra):
    return [sys.executable, LSPD, "--mcp", "--key", key, "--workspace", ws] + list(extra) + [
        "--", server]


def journal(home):
    try:
        with open(os.path.join(home, "journal")) as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []


def subst(obj, ws):
    return json.loads(json.dumps(obj).replace("{WS}", ws))


def text_of(reply):
    try:
        return reply["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None


def is_error(reply):
    try:
        return reply["result"].get("isError") is True
    except (KeyError, AttributeError, TypeError):
        return False


def short(obj, n=300):
    s = json.dumps(obj) if not isinstance(obj, str) else obj
    return s if len(s) <= n else s[:n] + "..."


class Mcp:
    'One `lspd.py --mcp` process, driven over newline-delimited JSON-RPC on stdio.'

    def __init__(self, argv, env, cwd):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, cwd=cwd, bufsize=0)
        self.cv = threading.Condition()
        self.replies, self.unsolicited, self.bad, self.stderr = {}, [], [], []
        self.eof = False
        self.answered = 0
        self.next_id = 1
        threading.Thread(target=self._out, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _out(self):
        assert self.p.stdout is not None
        for line in self.p.stdout:
            try:
                obj = json.loads(line)
            except ValueError:
                self.bad.append(line[:200])
                continue
            with self.cv:
                if isinstance(obj, dict) and "id" in obj and ("result" in obj or "error" in obj):
                    self.replies.setdefault(json.dumps(obj["id"]), []).append(obj)
                    self.answered += 1
                else:
                    self.unsolicited.append(obj)
                self.cv.notify_all()
        with self.cv:
            self.eof = True
            self.cv.notify_all()

    def _err(self):
        assert self.p.stderr is not None
        for line in self.p.stderr:
            self.stderr.append(line.decode(errors="replace"))

    def write(self, raw):
        try:
            assert self.p.stdin is not None
            self.p.stdin.write(raw)
            self.p.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def send(self, method, params=None, mid=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            if mid is None:
                mid = self.next_id
                self.next_id += 1
            msg["id"] = mid
        self.write(json.dumps(msg).encode() + b"\n")
        return mid

    def wait(self, mid, timeout=60.0):
        key = json.dumps(mid)
        deadline = time.time() + timeout
        with self.cv:
            while True:
                if self.replies.get(key):
                    return self.replies[key].pop(0)
                left = deadline - time.time()
                if left <= 0 or self.eof:
                    return None
                self.cv.wait(left)

    def call(self, method, params=None, timeout=60.0):
        return self.wait(self.send(method, params), timeout)

    def tool(self, name, args, timeout=60.0):
        return self.call("tools/call", {"name": name, "arguments": args}, timeout)

    def handshake(self):
        r = self.call("initialize", INIT["params"], 30)
        self.send("notifications/initialized", notify=True)
        return r

    def close(self, timeout=10.0):
        try:
            assert self.p.stdin is not None
            self.p.stdin.close()
        except OSError:
            pass
        try:
            return self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
            return None

    def why(self):
        return "rc=%s stderr=%s" % (self.p.poll(), short("".join(self.stderr)[-400:], 400))


class FakeDaemon:
    'A unix socket where the daemon\'s would be. It parses every frame STRICTLY, so an\n    interleaved write shows up as a bad frame instead of a lucky parse, and answers\n    initialize (optionally late), hover ("hover <basename>") and everything else with null.'

    def __init__(self, home, key, ws, init_delay=0.0, drop_on=None):
        run = os.path.join(home, ".local", "state", "agent-context", "lsp", "run")
        os.makedirs(run, exist_ok=True)
        digest = hashlib.sha1(os.path.realpath(ws).encode()).hexdigest()[:16]
        self.path = os.path.join(run, "%s-%s.sock" % (key, digest))
        self.frames, self.bad, self.conns = [], [], 0
        self.init_delay, self.drop_on = init_delay, drop_on
        self.lock = threading.Lock()
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with self.lock:
                self.conns += 1
                n = self.conns
            threading.Thread(target=self._serve, args=(conn, n), daemon=True).start()

    def _reply(self, conn, wlock, mid, result):
        raw = json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}).encode()
        with wlock:
            try:
                conn.sendall(b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
            except OSError:
                pass

    def _serve(self, conn, n):
        rfh = conn.makefile("rb")
        wlock = threading.Lock()
        try:
            while True:
                head = rfh.readline()
                if not head:
                    return
                m = re.match(rb"Content-Length: (\d+)\r\n$", head)
                if not m or rfh.readline() != b"\r\n":
                    self.bad.append("conn %d: bad header %r" % (n, head[:60]))
                    return
                length = int(m.group(1))
                body = rfh.read(length)
                try:
                    msg = json.loads(body)
                except ValueError:
                    self.bad.append("conn %d: body is not JSON: %r" % (n, body[:60]))
                    return
                with self.lock:
                    self.frames.append((n, msg))
                method, mid = msg.get("method"), msg.get("id")
                if self.drop_on and method == self.drop_on and n == 1:
                    conn.shutdown(socket.SHUT_RDWR)
                    return
                if mid is None or method is None:
                    continue
                if method == "initialize":
                    threading.Timer(self.init_delay, self._reply,
                                    (conn, wlock, mid, {"capabilities": {}})).start()
                elif method == "textDocument/hover":
                    uri = ((msg.get("params") or {}).get("textDocument") or {}).get("uri", "")
                    self._reply(conn, wlock, mid, {"contents": {
                        "kind": "markdown", "value": "hover " + os.path.basename(uri)}})
                else:
                    self._reply(conn, wlock, mid, None)
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def sent(self, method, conn=None):
        with self.lock:
            return [m for c, m in self.frames
                    if m.get("method") == method and (conn is None or c == conn)]

    def close(self):
        try:
            self.srv.close()
            os.unlink(self.path)
        except OSError:
            pass


def changed_files(ws):
    out = {}
    for root, _dirs, files in os.walk(ws):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, ws)
            with open(p, "r", newline="") as fh:
                text = fh.read()
            if FILES.get(rel) != text:
                out[rel] = text
    return out


def poll(fn, timeout, step=0.2):
    deadline = time.time() + timeout
    while True:
        v = fn()
        if v or time.time() >= deadline:
            return v
        time.sleep(step)


def load_dead_regex():
    try:
        spec = importlib.util.spec_from_file_location("lsp_failure_tripwire", TRIPWIRE)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "DEAD", None)
    except Exception:
        return None




def golden_replay():
    print("\n[1] golden replay: the Go bridge's answers, byte for byte")
    gold = {s["name"]: s["rows"] for s in goldens()}
    for i, sess in enumerate(SESSIONS):
        rows = gold.get(sess["name"])
        if rows is None or len(rows) != len(sess["cases"]):
            check("golden %s: fixture and goldens have the same cases" % sess["name"], False)
            continue
        home = mk_home()
        ws = build_ws(os.path.join(home, "ws"))
        env = scenario_env(home, **sess["env"])
        m = Mcp(mcp_argv("%sg%d" % (KEY, i), ws, server_wrapper(home)), env, ws)
        next_id = 1
        try:
            for case, (gname, greply, gfiles) in zip(sess["cases"], rows):
                if gname != case["name"]:
                    check("golden %s: case order matches the capture" % sess["name"], False,
                          "%r vs %r" % (gname, case["name"]))
                    break
                if case.get("notify"):
                    m.send(case["method"], subst(case.get("params"), ws), notify=True)
                    continue
                if "raw" in case:
                    m.write(case["raw"].encode() + b"\n")
                    reply = m.wait(None, 30)
                else:
                    mid = case.get("id", next_id)
                    next_id += 1
                    params = subst(case["params"], ws) if "params" in case else None
                    m.send(case["method"], params, mid=mid)
                    reply = m.wait(mid, 60)
                got = json.loads(json.dumps(reply).replace(ws, "{WS}")) if reply else None
                check("golden %s: %s%s" % (sess["name"], gname, TAGS.get(gname, "")),
                      got == greply, "got %s | want %s | %s"
                      % (short(got), short(greply), m.why() if got is None else ""))
                if gfiles is not None:
                    files = changed_files(ws)
                    check("golden %s: %s leaves the files as the Go bridge did"
                          % (sess["name"], gname), files == gfiles,
                          "got %s | want %s" % (short(files), short(gfiles)))
        finally:
            m.close()
        check("golden %s: stdout carried nothing but JSON-RPC replies [c12]" % sess["name"],
              m.answered > 0 and not m.bad and not m.unsolicited,
              "bad=%s unsolicited=%s" % (short(m.bad), short(m.unsolicited)))




def preflight():
    print("\n[2] launch checks folded in from lsp-mcp.sh [c5]")
    home = mk_home()
    stub = os.path.join(home, "stubs")
    os.makedirs(stub)
    for d in (".local/bin", ".dotnet/tools"):
        os.makedirs(os.path.join(home, d))
    
    
    brew_bin = os.path.join(home, "homebrew-bin")
    os.makedirs(brew_bin)
    base_path = stub + ":/usr/bin:/bin"

    def mkstub(name, body="exit 0"):
        p = os.path.join(stub, name)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o755)

    def rmstub(name):
        try:
            os.unlink(os.path.join(stub, name))
        except OSError:
            pass

    def run(key, cwd, env_extra=None, args=()):
        env = dict(os.environ)
        for k in ("AGENT_LSP_WORKSPACE", "DEVELOPER_DIR", "LSPD_XCODE_APPS"):
            env.pop(k, None)
        env.update({"HOME": home, "PATH": base_path, "GIT_CEILING_DIRECTORIES": home,
                    "LSPD_HOMEBREW_BIN": brew_bin})
        env.update(env_extra or {})
        p = subprocess.run([sys.executable, LSPD, "--mcp", "--key", key, "--preflight"]
                           + list(args), cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=60)
        plan = None
        if p.returncode == 0:
            try:
                plan = json.loads(p.stdout)
            except ValueError:
                plan = None
        return p.returncode, plan or {}, p.stderr

    def guaranteed(path):
        for d in (os.path.join(home, ".local", "bin"), os.path.join(home, ".dotnet", "tools"),
                  brew_bin):
            if ":%s:" % d not in ":%s:" % path and os.path.isdir(d):
                path = d + ":" + path
        return path

    plain = os.path.join(home, "plain")
    os.makedirs(plain)

    
    for key, want in (("tsgo", ["tsgo", "--lsp", "--stdio"]),
                      ("basedpyright", ["basedpyright-langserver", "--stdio"]),
                      ("typescript-language-server", ["typescript-language-server", "--stdio"]),
                      ("csharp-ls", ["csharp-ls"]),
                      ("sourcekit-lsp", ["sourcekit-lsp"]),
                      ("zz-ls", ["zz-ls"])):
        mkstub(want[0])
        rc, plan, err = run(key, plain)
        check("%s: rc 0 and the server command lsp-mcp.sh used" % key,
              rc == 0 and plan.get("command") == want, "rc=%s plan=%s err=%s"
              % (rc, short(plan), short(err)))

    rc, plan, err = run("zz-ls", plain, args=["--", "/abs/server", "--flag"])
    check("an explicit -- command overrides the key's",
          rc == 0 and plan.get("command") == ["/abs/server", "--flag"],
          "rc=%s plan=%s err=%s" % (rc, short(plan), short(err)))

    
    ils = os.path.join(home, ".local", "opt", "intellij-server", "current")
    os.makedirs(os.path.join(ils, "bin"))
    exe = os.path.join(ils, "bin", "intellij-server")
    with open(exe, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(exe, 0o755)
    with open(os.path.join(ils, "EULA.txt"), "wb") as fh:
        fh.write(b"JetBrains EULA v1\n")
    eula = hashlib.sha256(b"JetBrains EULA v1\n").hexdigest()[:16]
    rc, plan, err = run("kotlin-lsp", plain)
    check("kotlin-lsp: intellij-server with --eula from the EULA's sha256",
          rc == 0 and plan.get("command") == [exe, "--stdio", "--data-sharing", "none",
                                              "--eula", eula],
          "rc=%s plan=%s err=%s" % (rc, short(plan), short(err)))
    os.unlink(os.path.join(ils, "EULA.txt"))
    rc, plan, err = run("kotlin-lsp", plain)
    check("kotlin-lsp: no EULA.txt, no --eula",
          rc == 0 and plan.get("command") == [exe, "--stdio", "--data-sharing", "none"],
          "rc=%s plan=%s" % (rc, short(plan)))
    os.unlink(exe)
    rc, plan, err = run("kotlin-lsp", plain)
    check("kotlin-lsp missing: exit 127 naming refresh-intellij-server.py",
          rc == 127 and "is not on PATH" in err and "refresh-intellij-server.py" in err,
          "rc=%s err=%s" % (rc, short(err)))

    
    rmstub("basedpyright-langserver")
    rc, plan, err = run("basedpyright", plain)
    check("basedpyright missing: exit 127 with the install hint",
          rc == 127 and "basedpyright-langserver" in err and "is not on PATH" in err
          and "uv tool install basedpyright" in err, "rc=%s err=%s" % (rc, short(err)))
    rmstub("zz-ls")
    rc, plan, err = run("zz-ls", plain)
    check("an unknown key's missing server: exit 127",
          rc == 127 and "'zz-ls' is not on PATH" in err, "rc=%s err=%s" % (rc, short(err)))

    
    rc, plan, err = run("tsgo", plain)
    check("PATH gains ~/.local/bin, ~/.dotnet/tools and /opt/homebrew/bin, in lsp-mcp.sh's order",
          plan.get("path") == guaranteed(base_path),
          "got %s want %s" % (plan.get("path"), guaranteed(base_path)))
    dotnet = None
    for root in ("/usr/local/share/dotnet", os.path.join(home, ".dotnet")):
        if os.path.isdir(os.path.join(root, "shared", "Microsoft.NETCore.App")):
            dotnet = root
            break
    if dotnet is None:
        os.makedirs(os.path.join(home, ".dotnet", "shared", "Microsoft.NETCore.App"))
        dotnet = os.path.join(home, ".dotnet")
    rc, plan, err = run("csharp-ls", plain)
    check("csharp-ls: the first .NET root with shared/Microsoft.NETCore.App leads PATH",
          plan.get("path") == dotnet + ":" + guaranteed(base_path),
          "got %s" % plan.get("path"))

    
    rc, plan, err = run("tsgo", plain)
    check("outside git: the workspace is the cwd", plan.get("workspace") == plain,
          "got %s" % plan.get("workspace"))
    repo, wt = os.path.join(home, "repo"), os.path.join(home, "wt")
    os.makedirs(repo)
    with open(os.path.join(repo, "a.txt"), "w") as fh:
        fh.write("a\n")
    genv = dict(os.environ, HOME=home, GIT_CEILING_DIRECTORIES=home)
    
    key = os.path.join(home, "fixture-signing-key")
    if not os.path.exists(key):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key],
                       capture_output=True, stdin=subprocess.DEVNULL)
        if os.path.exists(key):
            os.chmod(key, 0o600)  
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"],
                 ["worktree", "add", "-q", wt, "-b", "wt"]):
        subprocess.run(["git", "-c", "gpg.format=ssh", "-c", "gpg.ssh.program=ssh-keygen",
                        "-c", "user.signingkey=" + key, "-c", "commit.gpgsign=true",
                        "-c", "user.name=t", "-c", "user.email=t@t"] + args, cwd=repo, env=genv,
                       capture_output=True)
    rc, plan, err = run("tsgo", wt)
    check("from a linked worktree: the workspace is the MAIN checkout",
          plan.get("workspace") == repo, "got %s" % plan.get("workspace"))
    rc, plan, err = run("tsgo", wt, {"AGENT_LSP_WORKSPACE": plain})
    check("AGENT_LSP_WORKSPACE overrides", plan.get("workspace") == plain,
          "got %s" % plan.get("workspace"))
    rc, plan, err = run("tsgo", wt, args=["--workspace", home])
    check("--workspace overrides", plan.get("workspace") == home,
          "got %s" % plan.get("workspace"))

    
    sw = os.path.join(home, "swiftws")
    os.makedirs(os.path.join(sw, "Foo.xcodeproj"))
    xlog = os.path.join(home, "xbs.log")
    mkstub("xcode-build-server", 'echo "$PWD $*" >> "%s"' % xlog)
    mkstub("xcode-select", "echo /Library/Developer/CommandLineTools")
    mkstub("sourcekit-lsp")
    xapp = os.path.join(home, "Xcode.app")
    dev = os.path.join(xapp, "Contents", "Developer")
    os.makedirs(os.path.join(dev, "usr", "bin"))
    with open(os.path.join(dev, "usr", "bin", "xcodebuild"), "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(os.path.join(dev, "usr", "bin", "xcodebuild"), 0o755)
    xenv = {"LSPD_XCODE_APPS": xapp}

    def xcalls():
        try:
            with open(xlog) as fh:
                return [line.rstrip("\n") for line in fh]
        except OSError:
            return []

    rc, plan, err = run("sourcekit-lsp", sw, xenv)
    check("Swift: xcode-select on the Command Line Tools sets DEVELOPER_DIR to an Xcode",
          plan.get("developer_dir") == dev, "got %s" % plan.get("developer_dir"))
    check("Swift: no buildServer.json rebinds with xcode-build-server config",
          xcalls() == ["%s config -project ./Foo.xcodeproj" % sw] and "rebinding" in err,
          "calls=%s err=%s" % (xcalls(), short(err)))
    with open(os.path.join(sw, "buildServer.json"), "w") as fh:
        json.dump({"build_root": os.path.join(home, "gone"), "scheme": "App"}, fh)
    rc, plan, err = run("sourcekit-lsp", sw, xenv)
    check("Swift: a vanished build_root rebinds with its scheme",
          xcalls()[1:] == ["%s config -project ./Foo.xcodeproj -scheme App" % sw],
          "calls=%s" % xcalls())
    dd = os.path.join(home, "dd")
    os.makedirs(dd)
    with open(os.path.join(sw, "buildServer.json"), "w") as fh:
        json.dump({"build_root": dd, "scheme": "App"}, fh)
    rec_path = os.path.join(home, ".local", "state", "agent-context", "health", "lsp",
                            sw.replace("/", "-") + "-sourcekit-lsp.json")
    rc, plan, err = run("sourcekit-lsp", sw, xenv)
    rec = {}
    try:
        with open(rec_path) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        pass
    fix = ("xcodebuild build -project Foo.xcodeproj -scheme 'App' "
           "-destination 'generic/platform=iOS Simulator'")
    check("Swift: a build_root with no build logs is not rebound", len(xcalls()) == 2,
          "calls=%s" % xcalls())
    check("Swift: ... and records a health finding carrying the build command",
          rec.get("ok") is False and rec.get("cwd") == sw and rec.get("server") == "sourcekit-lsp"
          and fix in (rec.get("detail") or "") and "NO build logs" in err,
          "rec=%s err=%s" % (short(rec), short(err)))
    os.makedirs(os.path.join(dd, "Logs", "Build"))
    open(os.path.join(dd, "Logs", "Build", "a.xcactivitylog"), "w").close()
    if os.path.exists(rec_path):
        os.unlink(rec_path)
    rc, plan, err = run("sourcekit-lsp", sw, xenv)
    check("Swift: build logs present: no rebind, no finding",
          len(xcalls()) == 2 and not os.path.exists(rec_path), "calls=%s" % xcalls())
    rc, plan, err = run("sourcekit-lsp", sw, dict(xenv, DEVELOPER_DIR="/preset"))
    check("Swift: a DEVELOPER_DIR already set is kept", plan.get("developer_dir") == "/preset",
          "got %s" % plan.get("developer_dir"))
    mkstub("xcode-select", "echo /Applications/Xcode.app/Contents/Developer")
    rc, plan, err = run("sourcekit-lsp", sw, xenv)
    check("Swift: xcode-select already on an Xcode leaves DEVELOPER_DIR unset",
          rc == 0 and "developer_dir" in plan and plan["developer_dir"] is None, "got %s" % plan.get("developer_dir"))
    rmstub("xcode-build-server")
    rc, plan, err = run("sourcekit-lsp", sw, xenv)
    check("Swift: an .xcodeproj without xcode-build-server says so",
          rc == 0 and "xcode-build-server is not installed" in err, "rc=%s err=%s"
          % (rc, short(err)))




def concurrency():
    print("\n[3] one writer at a time, one didOpen per document [c6, c7]")
    home = mk_home()
    ws = os.path.join(home, "ws")
    os.makedirs(ws)
    with open(os.path.join(ws, "big.py"), "w") as fh:
        fh.write("x = 1\n" * 300000)
    names = ["big.py"] + ["s%02d.py" % i for i in range(19)]
    for name in names[1:] + ["same.py"]:
        with open(os.path.join(ws, name), "w") as fh:
            fh.write("y = 2\n")
    key = KEY + "w"
    fake = FakeDaemon(home, key, ws)
    m = Mcp(mcp_argv(key, ws, server_wrapper(home)), base_env(home), ws)
    try:
        m.handshake()
        pending = [(name, m.send("tools/call", {"name": "hover", "arguments": {
            "filePath": os.path.join(ws, name), "line": 1, "column": 1}})) for name in names]
        wrong = [(name, text_of(r)) for name, r in ((n, m.wait(i, 60)) for n, i in pending)
                 if text_of(r) != "hover " + name]
        check("[c6] 20 concurrent calls, a 1.8 MB didOpen among them: every one answered",
              not wrong, "wrong=%s %s" % (short(wrong), m.why()))
        check("[c6] every frame the daemon received parsed", bool(fake.frames) and not fake.bad,
              "frames=%d bad=%s" % (len(fake.frames), short(fake.bad)))
        check("[c6] the daemon got all 20 didOpen and 20 hover frames",
              len(fake.sent("textDocument/didOpen")) == 20
              and len(fake.sent("textDocument/hover")) == 20,
              "didOpen=%d hover=%d" % (len(fake.sent("textDocument/didOpen")),
                                       len(fake.sent("textDocument/hover"))))
        same = os.path.join(ws, "same.py")
        ids = [m.send("tools/call", {"name": "hover", "arguments": {
            "filePath": same, "line": 1, "column": 1}}) for _ in range(10)]
        replies = [m.wait(i, 30) for i in ids]
        check("[c7] 10 concurrent calls on one unopened file: all answered",
              all(text_of(r) == "hover same.py" for r in replies), m.why())
        opens = [f for f in fake.sent("textDocument/didOpen")
                 if f["params"]["textDocument"]["uri"].endswith("/same.py")]
        check("[c7] exactly one didOpen reached the daemon for it", len(opens) == 1,
              "got %d" % len(opens))
    finally:
        m.close()
        fake.close()




def solution_open():
    print("\n[4] solution/open: once per server start, from the daemon only [c9]")
    home = mk_home()
    ws = os.path.join(home, "sln")
    os.makedirs(ws)
    for name, text in (("App.sln", ""), ("Program.cs", "class P {}\n")):
        with open(os.path.join(ws, name), "w") as fh:
            fh.write(text)
    fake = FakeDaemon(home, "csharp-ls", ws)
    m = Mcp(mcp_argv("csharp-ls", ws, server_wrapper(home)), base_env(home), ws)
    try:
        m.handshake()
        r = m.tool("hover", {"filePath": os.path.join(ws, "Program.cs"), "line": 1, "column": 1})
        check("csharp-ls front end answers through the daemon socket",
              text_of(r) == "hover Program.cs", m.why())
        check("[c9] the front end itself never sends solution/open or project/open",
              bool(fake.sent("initialize")) and not fake.sent("solution/open")
              and not fake.sent("project/open"))
    finally:
        m.close()
        fake.close()

    home = mk_home()
    ws = os.path.join(home, "sln")
    os.makedirs(ws)
    for name, text in (("App.sln", ""), ("Program.cs", "class P {}\n")):
        with open(os.path.join(ws, name), "w") as fh:
            fh.write(text)
    REAP_PATTERNS.append("lspd.py --daemon --key csharp-ls --workspace %s" % ws)
    env = base_env(home, MOCKLS_JOURNAL=os.path.join(home, "journal"))
    m = Mcp(mcp_argv("csharp-ls", ws, server_wrapper(home)), env, ws)

    def opens():
        return [e for e in journal(home) if e["kind"] == "solution/open"]

    try:
        m.handshake()
        m.tool("hover", {"filePath": os.path.join(ws, "Program.cs"), "line": 1, "column": 1})
        poll(opens, 20)
        time.sleep(1.5)
        first = opens()
        check("[c9] the daemon sends solution/open once when its server starts",
              len(first) == 1, "got %d; %s" % (len(first), m.why()))
        check("[c9] ... naming the workspace's .sln",
              bool(first) and all(e["payload"]["params"]["solution"] == "file://" + os.path.join(ws, "App.sln")
                  for e in first), short(first))
        subprocess.run([sys.executable, LSPD, "--restart", "--key", "csharp-ls",
                        "--workspace", ws], env=env, capture_output=True, timeout=120)
        poll(lambda: len(opens()) >= 2, 30)
        time.sleep(1.5)
        check("[c9] once more after an in-place restart, and no more",
              len(opens()) == 2, "got %d" % len(opens()))
    finally:
        m.close()




def lost_daemon():
    print("\n[5] a dropped daemon connection answers, then reconnects [c10]")
    home = mk_home()
    ws = os.path.join(home, "ws")
    os.makedirs(ws)
    for name in ("a.py", "b.py"):
        with open(os.path.join(ws, name), "w") as fh:
            fh.write("z = 3\n")
    key = KEY + "d"
    fake = FakeDaemon(home, key, ws, drop_on="textDocument/hover")
    m = Mcp(mcp_argv(key, ws, server_wrapper(home)), base_env(home), ws)
    dead = load_dead_regex()
    try:
        m.handshake()
        t0 = time.time()
        r = m.tool("hover", {"filePath": os.path.join(ws, "a.py"), "line": 1, "column": 1}, 30)
        took = time.time() - t0
        check("[c10] a call pending when the connection drops is answered within 5 s",
              r is not None and took < 5, "took %.1fs; %s" % (took, m.why()))
        check("[c10] ... as a tool error", is_error(r), short(r))
        check("[c10] ... in words lsp-failure-tripwire arms on",
              dead is not None and bool(text_of(r)) and bool(dead.search(text_of(r) or "")),
              "text=%r dead_regex=%s" % (text_of(r), dead is not None))
        t0 = time.time()
        r2 = m.tool("hover", {"filePath": os.path.join(ws, "b.py"), "line": 1, "column": 1}, 40)
        check("[c10] the next call reconnects and is answered",
              text_of(r2) == "hover b.py", "got %s after %.1fs" % (short(r2), time.time() - t0))
        check("[c10] the reconnect initializes again", len(fake.sent("initialize", conn=2)) == 1,
              "initialize on conn 2: %d" % len(fake.sent("initialize", conn=2)))
    finally:
        m.close()
        fake.close()




def handshake_and_eof():
    print("\n[6] the MCP handshake does not wait on a cold server; EOF leaves the daemon [c12]")
    home = mk_home()
    ws = os.path.join(home, "ws")
    os.makedirs(ws)
    with open(os.path.join(ws, "c.py"), "w") as fh:
        fh.write("c = 4\n")
    key = KEY + "c"
    fake = FakeDaemon(home, key, ws, init_delay=4.0)
    m = Mcp(mcp_argv(key, ws, server_wrapper(home)), base_env(home), ws)
    try:
        t0 = time.time()
        r = m.call("initialize", INIT["params"], 10)
        took = time.time() - t0
        check("[c12] initialize answers in under 1 s while the server is still initializing",
              r is not None and "result" in r and took < 1.0, "took %.2fs; %s" % (took, m.why()))
        m.send("notifications/initialized", notify=True)
        t0 = time.time()
        r = m.call("tools/list", {}, 10)
        took = time.time() - t0
        check("[c12] tools/list answers in under 1 s too", r is not None and took < 1.0,
              "took %.2fs" % took)
        r = m.tool("hover", {"filePath": os.path.join(ws, "c.py"), "line": 1, "column": 1}, 30)
        check("[c12] a tool call waits for the server, then answers",
              text_of(r) == "hover c.py", short(r))
    finally:
        m.close()
        fake.close()

    home = mk_home()
    ws = build_ws(os.path.join(home, "ws"))
    env = scenario_env(home)
    key = KEY + "e"
    srv = server_wrapper(home)
    a = Mcp(mcp_argv(key, ws, srv), env, ws)
    a.handshake()
    ra = a.tool("hover", {"filePath": os.path.join(ws, "app", "main.py"), "line": 6, "column": 13})
    check("[c12] a front end is answered by a real daemon", text_of(ra) == GOLD_HOVER,
          "%s %s" % (short(ra), a.why()))
    t0 = time.time()
    rc = a.close(timeout=10)
    took = time.time() - t0
    check("[c12] stdin EOF: the front end exits 0 within 5 s", rc == 0 and took < 5,
          "rc=%s took %.1fs" % (rc, took))
    b = Mcp(mcp_argv(key, ws, srv), env, ws)
    try:
        b.handshake()
        rb = b.tool("hover", {"filePath": os.path.join(ws, "app", "main.py"), "line": 6,
                              "column": 13})
        check("[c12] the daemon kept serving: the next front end is answered",
              text_of(rb) == GOLD_HOVER, short(rb))
        out = subprocess.run(["pgrep", "-f", "lspd.py --daemon --key %s " % key],
                             capture_output=True, text=True).stdout.split()
        check("[c12] still exactly one daemon for the key", len(out) == 1, "pids=%s" % out)
    finally:
        b.close()




def sync_at_query():
    print("\n[7] file sync at query time [c19]")
    home = mk_home()
    ws = build_ws(os.path.join(home, "ws"))
    env = scenario_env(home)
    key = KEY + "s"
    srv = server_wrapper(home)
    main = os.path.join(ws, "app", "main.py")
    a = Mcp(mcp_argv(key, ws, srv), env, ws)
    b = None

    def since(mark):
        return journal(home)[mark:]

    def before_first(entries, method):
        for i, e in enumerate(entries):
            if e["kind"] == method:
                return entries[:i], True
        return entries, False

    def changes(entries):
        out = []
        for e in entries:
            if e["kind"] == "workspace/didChangeWatchedFiles":
                out.extend(e["payload"].get("params", {}).get("changes") or [])
        return out

    def carries(entry, name, needle):
        p = entry["payload"].get("params") or {}
        uri = (p.get("textDocument") or {}).get("uri", "")
        if not uri.endswith("/" + name):
            return False
        if entry["kind"] == "textDocument/didOpen":
            return needle in ((p.get("textDocument") or {}).get("text") or "")
        if entry["kind"] == "textDocument/didChange":
            return any(needle in (c.get("text") or "") for c in p.get("contentChanges") or [])
        return False

    try:
        a.handshake()
        a.tool("hover", {"filePath": main, "line": 6, "column": 13})
        with open(main, "a") as fh:
            fh.write("# changed on disk\n")
        time.sleep(SETTLE)
        mark = len(journal(home))
        a.tool("hover", {"filePath": main, "line": 6, "column": 13})
        pre, seen = before_first(since(mark), "textDocument/hover")
        check("[c19] an open file changed on disk reaches the server before the next query",
              seen and any(carries(e, "main.py", "# changed on disk") for e in pre),
              "saw hover=%s; before it: %s" % (seen, short([e["kind"] for e in pre])))

        mark = len(journal(home))
        with open(os.path.join(ws, "app", "new_mod.py"), "w") as fh:
            fh.write("Z = 1\n")
        time.sleep(2.5)
        idle = [c for c in changes(since(mark)) if c["uri"].endswith("/app/new_mod.py")]
        check("[c19] nothing is scanned while idle", seen and not idle, short(idle))
        a.tool("definition", {"symbolName": "Shape"})
        pre, seen = before_first(since(mark), "workspace/symbol")
        check("[c19] a created file is announced (type 1) before the next query",
              seen and any(c["uri"].endswith("/app/new_mod.py") and c["type"] == 1
                           for c in changes(pre)), short(changes(pre)))

        os.remove(os.path.join(ws, "app", "clean.py"))
        with open(os.path.join(ws, "pkg", "edge.py"), "a") as fh:
            fh.write("# edited\n")
        time.sleep(SETTLE)
        mark = len(journal(home))
        a.tool("references", {"symbolName": "Missing"})
        pre, seen = before_first(since(mark), "workspace/symbol")
        got = changes(pre)
        check("[c19] a deleted file is announced (type 3)",
              seen and any(c["uri"].endswith("/app/clean.py") and c["type"] == 3 for c in got),
              short(got))
        check("[c19] a changed file nobody has open is announced (type 2)",
              seen and any(c["uri"].endswith("/pkg/edge.py") and c["type"] == 2 for c in got),
              short(got))

        b = Mcp(mcp_argv(key, ws, srv), env, ws)
        b.handshake()
        with open(os.path.join(ws, "app", "twice.py"), "w") as fh:
            fh.write("T = 2\n")
        time.sleep(SETTLE)
        mark = len(journal(home))
        a.tool("hover", {"filePath": main, "line": 6, "column": 13})
        b.tool("hover", {"filePath": main, "line": 6, "column": 13})
        n = len([c for c in changes(since(mark)) if c["uri"].endswith("/app/twice.py")])
        check("[c19] two clients, one change: announced exactly once", n == 1, "got %d" % n)
    finally:
        a.close()
        if b is not None:
            b.close()


def main():
    if "--dump-goldens" in sys.argv[1:]:
        print(json.dumps(goldens(), indent=1))
        return 0
    sections = (golden_replay, preflight, concurrency, solution_open, lost_daemon,
                handshake_and_eof, sync_at_query)
    try:
        for section in sections:
            try:
                section()
            except Exception as exc:
                check("%s ran to completion" % section.__name__, False,
                      "%s: %s" % (type(exc).__name__, exc))
    finally:
        reap()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
