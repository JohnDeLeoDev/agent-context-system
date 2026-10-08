#!/usr/bin/env python3
'Battery: an unbuilt Swift project fails loudly, never as a silent "not found".\n\nEvery case runs against a throwaway HOME with mockls behind its own name, so no real\ndaemon, language server or health record is touched.\n\nRun: python3 test-lsp-no-compiler-flags.py'
import glob
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LSPD = os.path.join(HERE, "lspd.py")
CANARY = os.path.join(HERE, "lsp-canary.py")
MOCK = os.path.join(HERE, "mockls.py")

PASS, FAIL = [], []
HOMES = []
DAEMONS = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if detail and not cond else ""), flush=True)


def short(v, n=240):
    s = v if isinstance(v, str) else json.dumps(v)
    return s if len(s) <= n else s[:n] + "..."


def fix_for(ws):
    return ("no compiler flags: build Foo once (xcodebuild build -project Foo.xcodeproj "
            "-scheme 'App' -destination 'generic/platform=iOS Simulator'), then lspd.py "
            "--restart --key sourcekit-lsp --workspace %s" % ws)


def mk_home():
    
    
    home = os.path.realpath(tempfile.mkdtemp(prefix="nf-", dir=os.path.join(os.path.expanduser("~"), ".cache")))
    HOMES.append(home)
    bindir = os.path.join(home, "bin")
    os.makedirs(bindir)
    os.makedirs(os.path.join(home, "brew"))

    def stub(name, body):
        p = os.path.join(bindir, name)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o755)

    stub("xcode-build-server", 'echo "$PWD $*" >> "%s"' % os.path.join(home, "xbs.log"))
    stub("xcode-select", "echo /Applications/Xcode.app/Contents/Developer")
    stub("sourcekit-lsp", "exit 0")
    
    
    stub("mockls-srv", 'exec "%s" "%s" "$@"' % (sys.executable, MOCK))
    return home


def env_for(home):
    env = dict(os.environ)
    for k in ("AGENT_LSP_WORKSPACE", "DEVELOPER_DIR", "LSPD_XCODE_APPS", "LSP_CONTEXT_LINES"):
        env.pop(k, None)
    env.update({"HOME": home, "PATH": os.path.join(home, "bin") + ":/usr/bin:/bin",
                "LSPD_HOMEBREW_BIN": os.path.join(home, "brew"), "LSPD_IDLE_EXIT": "60",
                "LSPD_CANARY": "0", "LSPD_SEED_SETTLE": "0.2", "LSPD_GIT_POLL": "3600",
                "MOCKLS_JOURNAL": os.path.join(home, "journal")})
    return env


def swift_ws(home, name, logs=False, xcodeproj=True, bsj=True):
    ws = os.path.join(home, name)
    os.makedirs(ws)
    if xcodeproj:
        os.makedirs(os.path.join(ws, "Foo.xcodeproj"))
    with open(os.path.join(ws, "a.swift"), "w") as fh:
        fh.write("struct Shape {}\n")
    dd = os.path.join(home, name + "-dd")
    os.makedirs(dd)
    if logs:
        add_log(dd)
    if bsj:
        with open(os.path.join(ws, "buildServer.json"), "w") as fh:
            json.dump({"build_root": dd, "scheme": "App"}, fh)
    return os.path.realpath(ws), dd


def add_log(dd):
    os.makedirs(os.path.join(dd, "Logs", "Build"), exist_ok=True)
    open(os.path.join(dd, "Logs", "Build", "x.xcactivitylog"), "w").close()


def sockets(home):
    return sorted(glob.glob(os.path.join(home, ".local", "state", "agent-context", "lsp", "run", "*.sock")))


class MCP:
    def __init__(self, home, key, ws, argv=None):
        DAEMONS.append((home, key, ws))
        self.err = open(os.path.join(home, "mcp-%s.err" % key), "ab")
        cmd = [sys.executable, LSPD, "--mcp", "--key", key, "--workspace", ws]
        if argv:
            cmd += ["--"] + argv
        self.p = subprocess.Popen(cmd, cwd=ws, env=env_for(home), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.err)
        self.q = queue.Queue()
        self.n = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        assert self.p.stdout is not None
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                pass
        self.q.put(None)

    def call(self, method, params, timeout=60):
        self.n += 1
        mid = self.n
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                                       "params": params}).encode() + b"\n")
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if msg is None:
                return None
            if msg.get("id") == mid:
                return msg
        return None

    def start(self):
        r = self.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}, 20)
        assert self.p.stdin is not None
        self.p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.p.stdin.flush()
        return r is not None and "result" in r

    def tool(self, name, args, timeout=60):
        t0 = time.time()
        r = self.call("tools/call", {"name": name, "arguments": args}, timeout)
        secs = time.time() - t0
        if not r or "result" not in r:
            return json.dumps(r), True, secs
        content = r["result"].get("content") or [{}]
        return content[0].get("text", ""), bool(r["result"].get("isError")), secs

    def close(self):
        try:
            assert self.p.stdin is not None
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.err.close()


def unbuilt():
    print("\n[1] an unbuilt Swift project answers with the build-once sentence [c1, c2, c5]")
    home = mk_home()
    ws, dd = swift_ws(home, "App")
    fix = fix_for(ws)
    src = os.path.join(ws, "a.swift")
    m = MCP(home, "sourcekit-lsp", ws, [os.path.join(home, "bin", "mockls-srv")])
    try:
        check("initialize answers", m.start())
        gated = False
        for name, args in (("definition", {"symbolName": "Shape"}),
                           ("references", {"symbolName": "Shape"}),
                           ("hover", {"filePath": src, "line": 1, "column": 8}),
                           ("diagnostics", {"filePath": src}),
                           ("rename_symbol", {"filePath": src, "line": 1, "column": 8,
                                              "newName": "Form"})):
            text, err, secs = m.tool(name, args)
            check("[c1] %s: isError, starting with the build-once sentence" % name,
                  err and text.startswith(fix), "isError=%s text=%s" % (err, short(text)))
            if name == "definition":
                gated = err and text.startswith(fix)
            check("[c1] %s: answered within 5 s" % name, secs < 5, "took %.1fs" % secs)
        text, err, secs = m.tool("edit_file", {"filePath": src, "edits": [
            {"startLine": 1, "endLine": 1, "newText": "struct Shape { let n = 1 }"}]})
        with open(src) as fh:
            body = fh.read()
        check("[c5] edit_file still applies its edit while unbuilt",
              not err and text.startswith("Successfully applied text edits")
              and "let n = 1" in body, "isError=%s text=%s file=%r" % (err, short(text), body))
        time.sleep(1.5)
        check("[c2] no daemon was started while unbuilt", sockets(home) == [],
              "sockets=%s" % sockets(home))

        print("\n[2] a build log appears: the same session answers normally [c3]")
        add_log(dd)
        text, err, secs = m.tool("definition", {"symbolName": "Shape"})
        
        check("[c3] definition, gated before the log, now answers without the sentence",
              gated and "no compiler flags" not in text and not err,
              "gated_before=%s isError=%s text=%s" % (gated, err, short(text)))
    finally:
        m.close()


def not_gated():
    print("\n[3] no gate where flags exist or the check does not apply [c6]")
    home = mk_home()
    cases = (("build logs present", "sourcekit-lsp", dict(logs=True)),
             ("SPM: no .xcodeproj", "sourcekit-lsp", dict(xcodeproj=False)),
             ("no buildServer.json", "sourcekit-lsp", dict(bsj=False)),
             ("a non-Swift key", "zz-ls", dict()))
    for i, (label, key, kw) in enumerate(cases):
        ws, _ = swift_ws(home, "W%d" % i, **kw)
        m = MCP(home, key, ws, [os.path.join(home, "bin", "mockls-srv")])
        try:
            m.start()
            text, err, _ = m.tool("definition", {"symbolName": "Shape"})
            check("[c6] %s: definition answers without the build-once sentence" % label,
                  "no compiler flags" not in text and not err,
                  "isError=%s text=%s" % (err, short(text)))
        finally:
            m.close()


def canary():
    print("\n[4] lsp-canary records the sentence at once [c4]")
    home = mk_home()
    ws, _ = swift_ws(home, "App")
    DAEMONS.append((home, "sourcekit-lsp", ws))
    scripts = os.path.join(home, ".agent-context", "global", "scripts")
    os.makedirs(scripts)
    os.symlink(LSPD, os.path.join(scripts, "lspd.py"))
    with open(os.path.join(home, ".agent-context", "global", "lsp-canaries.json"), "w") as fh:
        json.dump({"canaries": {"App": [{"server": "sourcekit-lsp", "symbol": "Shape"}]}}, fh)
    t0 = time.time()
    p = subprocess.run([sys.executable, CANARY, ws], cwd=ws, env=env_for(home),
                       capture_output=True, text=True, timeout=400)
    secs = time.time() - t0
    rec = {}
    for path in glob.glob(os.path.join(home, ".local", "state", "agent-context", "health", "lsp",
                                       "*-sourcekit-lsp.json")):
        with open(path) as fh:
            rec = json.load(fh)
    check("[c4] the canary exits 0", p.returncode == 0, "rc=%s err=%s"
          % (p.returncode, short(p.stderr)))
    check("[c4] its verdict is ok:false and starts with the build-once sentence",
          rec.get("ok") is False and (rec.get("detail") or "").startswith(fix_for(ws))
          and rec.get("symbol") == "Shape", "rec=%s" % short(rec, 500))
    check("[c4] it stops at the first such answer (under 30 s, not the 90 s budget)",
          secs < 30, "took %.0fs" % secs)


def retire():
    for home, key, ws in DAEMONS:
        subprocess.run([sys.executable, LSPD, "--upgrade", "--force", "--key", key,
                        "--workspace", ws], env=env_for(home), capture_output=True, timeout=60)


def main():
    print("unbuilt Swift projects fail loudly")
    try:
        unbuilt()
        not_gated()
        canary()
    finally:
        retire()
        for home in HOMES:
            shutil.rmtree(home, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
