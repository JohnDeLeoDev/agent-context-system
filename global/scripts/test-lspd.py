#!/usr/bin/env python3
'Test battery for lspd. Every case here is a fault that actually happened.\n\nRun: python3 test-lspd.py'
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LSPD = os.path.join(HERE, "lspd.py")
MOCK = os.path.join(HERE, "mockls.py")





KEY_PREFIX = "mock%d" % os.getpid()
TMPDIRS = []
MOCK_PYTHON = sys.executable      


def reap():
    "Kill this run's daemons and remove its workspaces. Safe to call twice."
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s" % KEY_PREFIX],
                   capture_output=True)
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)

PASS, FAIL = [], []


class ClientGone(RuntimeError):
    'The attach client died before the test could talk to it.\n\n    Its own class so run() can turn it into a NAMED failure and still reach the reaper,\n    rather than letting a BrokenPipeError unwind out of run() and skip cleanup entirely.'


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + detail) if detail and not cond else ""))


class Client:
    'Drives lspd --attach exactly the way mcp-language-server drives a real server.'

    def __init__(self, key, ws, journal, index_ms=0, env_extra=None, lspd_path=None):
        self.key = key
        env = dict(os.environ)
        env["MOCKLS_JOURNAL"] = journal
        env["MOCKLS_INDEX_MS"] = str(index_ms)
        env["LSPD_IDLE_EXIT"] = "3600"
        env.update(env_extra or {})
        
        
        
        self.p = subprocess.Popen(
            [sys.executable, lspd_path or LSPD, "--attach", "--key", key,
             "--workspace", ws, "--", MOCK_PYTHON, MOCK],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, bufsize=0)

    def send(self, obj):
        'Write one LSP message. A DEAD client is a test failure, never a traceback.'
        raw = json.dumps(obj).encode()
        stdin = self.p.stdin
        assert stdin is not None, "Popen was given stdin=PIPE"
        try:
            stdin.write(b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
            stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise ClientGone("attach client for key %r is gone (%s); rc=%s"
                             % (self.key, exc.__class__.__name__, self.p.poll())) from exc

    def read(self, timeout=25.0):
        'One message, with a deadline enforced by a reader thread.'
        import threading
        box = {}
        stdout = self.p.stdout
        assert stdout is not None, "Popen was given stdout=PIPE"

        def go():
            length = None
            while True:
                line = stdout.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    break
                if b":" in line:
                    k, v = line.split(b":", 1)
                    if k.strip().lower() == b"content-length":
                        length = int(v.strip())
            if not length:
                return
            buf = b""
            while len(buf) < length:
                c = stdout.read(length - len(buf))
                if not c:
                    return
                buf += c
            box["m"] = json.loads(buf)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(timeout)
        return box.get("m")

    def await_id(self, want, timeout=25):
        'Skip notifications and server-initiated requests; return the response to `want`.'
        end = time.time() + timeout
        while time.time() < end:
            m = self.read(timeout=max(1, end - time.time()))
            if m is None:
                return None
            if m.get("id") == want and "method" not in m:
                return m
            if m.get("id") is not None and m.get("method"):
                
                self.send({"jsonrpc": "2.0", "id": m["id"], "result": [{}]})
        return None

    def initialize(self, root):
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"processId": os.getpid(), "rootUri": "file://" + root,
                              "capabilities": {}}})
        r = self.await_id(1)
        self.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        return r

    def kill(self):
        try:
            self.p.terminate()
            self.p.wait(timeout=5)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass


def journal(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def count(j, kind):
    return sum(1 for r in j if r["kind"] == kind)


def alive_pid(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def run():
    'Wrapper: the cases always get reaped after them, however they end.'
    try:
        return _cases()
    except ClientGone as exc:
        
        
        check("the battery kept its attach client to the end", False, str(exc))
        print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
        print("FAILED: " + ", ".join(FAIL))
        return 1
    finally:
        reap()


def _cases():
    global MOCK_PYTHON
    
    
    
    
    
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    if os.path.abspath(os.environ.get("HOME", real_home)) == os.path.abspath(real_home):
        
        
        test_home = tempfile.mkdtemp(prefix="lspd-h-", dir="/tmp")
        TMPDIRS.append(test_home)
        os.environ["HOME"] = test_home
    tmp = tempfile.mkdtemp(prefix="lspd-test-")
    TMPDIRS.append(tmp)
    
    
    
    
    MOCK_PYTHON = os.path.join(tmp, "mockpy")
    os.symlink(sys.executable, MOCK_PYTHON)
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws)
    jrn = os.path.join(tmp, "journal")
    key = KEY_PREFIX

    print("\n[1] two clients share ONE server")
    a = Client(key, ws, jrn)
    ra = a.initialize(ws)
    check("client A gets an InitializeResult", ra is not None and "result" in (ra or {}),
          repr(ra))

    b = Client(key, ws, jrn)
    rb = b.initialize(ws)
    check("client B gets an InitializeResult", rb is not None and "result" in (rb or {}),
          repr(rb))

    time.sleep(1.0)
    j = journal(jrn)
    check("server saw exactly ONE initialize", count(j, "initialize") == 1,
          "saw %d" % count(j, "initialize"))
    check("server saw exactly ONE initialized", count(j, "initialized") == 1,
          "saw %d" % count(j, "initialized"))

    print("\n[2] colliding request ids from different clients")
    a.send({"jsonrpc": "2.0", "id": 77, "method": "workspace/symbol",
            "params": {"query": "FromA"}})
    b.send({"jsonrpc": "2.0", "id": 77, "method": "workspace/symbol",
            "params": {"query": "FromB"}})
    resa = a.await_id(77)
    resb = b.await_id(77)
    na = (resa or {}).get("result", [{}])[0].get("name") if resa else None
    nb = (resb or {}).get("result", [{}])[0].get("name") if resb else None
    check("A's id=77 answered with A's query", na == "SYM:FromA", repr(na))
    check("B's id=77 answered with B's query", nb == "SYM:FromB", repr(nb))

    print("\n[3] server-initiated request reaches a client (lspmux drops these)")
    j = journal(jrn)
    check("a client answered workspace/configuration", count(j, "response") >= 1,
          "no response recorded")

    print("\n[4] didOpen/didClose are refcounted")
    uri = "file://" + os.path.join(ws, "f.txt")
    doc = {"textDocument": {"uri": uri, "languageId": "plaintext",
                            "version": 1, "text": "x"}}
    a.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": doc})
    b.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": doc})
    time.sleep(0.8)
    j = journal(jrn)
    check("two didOpens collapse to one", count(j, "textDocument/didOpen") == 1,
          "saw %d" % count(j, "textDocument/didOpen"))

    a.send({"jsonrpc": "2.0", "method": "textDocument/didClose",
            "params": {"textDocument": {"uri": uri}}})
    time.sleep(0.8)
    j = journal(jrn)
    check("first didClose is suppressed (B still has it open)",
          count(j, "textDocument/didClose") == 0,
          "saw %d" % count(j, "textDocument/didClose"))

    print("\n[5] one client leaving does NOT kill the shared server")
    a.send({"jsonrpc": "2.0", "id": 999, "method": "shutdown", "params": None})
    sd = a.await_id(999, timeout=10)
    check("A's shutdown is answered locally", sd is not None and "result" in (sd or {}))
    a.send({"jsonrpc": "2.0", "method": "exit", "params": None})
    a.kill()
    time.sleep(1.2)
    j = journal(jrn)
    check("server never saw shutdown", count(j, "shutdown") == 0)
    check("server never saw exit", count(j, "exit") == 0)

    b.send({"jsonrpc": "2.0", "id": 78, "method": "workspace/symbol",
            "params": {"query": "AfterAleft"}})
    r = b.await_id(78)
    nm = (r or {}).get("result", [{}])[0].get("name") if r else None
    check("client B still works after A left", nm == "SYM:AfterAleft", repr(nm))

    print("\n[6] a third client attaches to the ALREADY-WARM server")
    t0 = time.time()
    c = Client(key, ws, jrn)
    rc = c.initialize(ws)
    dt = time.time() - t0
    check("client C initializes from cache", rc is not None and "result" in (rc or {}))
    check("C attached fast (no second cold start)", dt < 6.0, "took %.1fs" % dt)
    j = journal(jrn)
    check("server STILL saw only one initialize", count(j, "initialize") == 1,
          "saw %d" % count(j, "initialize"))

    b.kill()
    c.kill()

    print("\n[6b] the daemon outlives every client, then idles out")
    time.sleep(1.0)
    still = subprocess.run(
        ["/bin/ps", "-Ao", "command"], capture_output=True, text=True).stdout
    check("daemon survives with zero clients attached",
          ("--key %s " % key) in still or ("--key " + key) in still)

    print("\n[7] navigation is gated while the index is cold")
    jrn2 = os.path.join(tmp, "journal2")
    key2 = key + "idx"
    env_client = Client(key2, ws, jrn2, index_ms=3000)
    env_client.initialize(ws)
    time.sleep(0.5)                       
    t0 = time.time()
    env_client.send({"jsonrpc": "2.0", "id": 55, "method": "workspace/symbol",
                     "params": {"query": "Cold"}})
    r = env_client.await_id(55, timeout=30)
    held = time.time() - t0
    check("cold nav request was answered", r is not None)
    check("cold nav request was HELD until warm (>=2s)", held >= 2.0,
          "returned after %.1fs" % held)
    env_client.kill()

    print("\n[8] a language-server CRASH is absorbed, not passed to the client")
    
    
    jrn3 = os.path.join(tmp, "journal3")
    key3 = key + "crash"
    surv = Client(key3, ws, jrn3)
    r0 = surv.initialize(ws)
    check("crash-test client initialized", r0 is not None and "result" in (r0 or {}))
    surv.send({"jsonrpc": "2.0", "id": 10, "method": "workspace/symbol",
               "params": {"query": "Before"}})
    pre = surv.await_id(10)
    check("answers before the crash",
          (pre or {}).get("result", [{}])[0].get("name") == "SYM:Before" if pre else False)

    ps = subprocess.run(["/bin/ps", "-Ao", "pid,ppid,command"],
                        capture_output=True, text=True).stdout
    daemon_pid = None
    for line in ps.splitlines():
        if ("lspd.py --daemon --key %s " % key3) in line:
            daemon_pid = int(line.split()[0])
            break
    victim = None
    if daemon_pid:
        for line in ps.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[1] == str(daemon_pid) and "mockls.py" in parts[2]:
                victim = int(parts[0])
                break
    check("found the language server child to kill", victim is not None,
          "daemon=%s" % daemon_pid)
    if victim:
        os.kill(victim, 9)
        time.sleep(4.0)                       
        surv.send({"jsonrpc": "2.0", "id": 11, "method": "workspace/symbol",
                   "params": {"query": "AfterCrash"}})
        post = surv.await_id(11, timeout=30)
        nm = (post or {}).get("result", [{}])[0].get("name") if post else None
        check("SAME client still answers after the server was killed",
              nm == "SYM:AfterCrash", repr(nm))
        j3 = journal(jrn3)
        check("the replacement server got its own initialize",
              count(j3, "initialize") == 2, "saw %d" % count(j3, "initialize"))
    surv.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key3],
                   capture_output=True)

    print("\n[9] a document the server cannot parse never reaches it")
    
    
    
    
    jrn4 = os.path.join(tmp, "journal4")
    ts = Client("tsgo", ws, jrn4)
    ts.initialize(ws)
    md = "file://" + os.path.join(ws, "AGENTS.md")
    ts.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": md, "languageId": "markdown", "version": 1,
                         "text": "# nope"}}})
    real = "file://" + os.path.join(ws, "app.ts")
    ts.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": real, "languageId": "typescript", "version": 1,
                         "text": "export const x = 1"}}})
    time.sleep(1.0)
    j4 = journal(jrn4)
    opened = [r["payload"]["params"]["textDocument"]["uri"]
              for r in j4 if r["kind"] == "textDocument/didOpen"]
    check("markdown didOpen is refused", md not in opened, repr(opened))
    check("TypeScript didOpen still goes through", real in opened, repr(opened))
    ts.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key tsgo --workspace %s" % ws],
                   capture_output=True)

    print("\n[10] an exhausted restart budget does NOT take the daemon down")
    
    
    
    
    jrn5 = os.path.join(tmp, "journal5")
    key5 = key + "budget"
    hold = Client(key5, ws, jrn5,
                  env_extra={"LSPD_MAX_RESTARTS": "1", "LSPD_RESTART_WINDOW": "3"})
    hold.initialize(ws)

    def kill_server_child(dkey):
        out = subprocess.run(["/bin/ps", "-Ao", "pid,ppid,command"],
                             capture_output=True, text=True).stdout
        dpid = None
        for line in out.splitlines():
            if ("lspd.py --daemon --key %s " % dkey) in line:
                dpid = line.split()[0]
                break
        if not dpid:
            return False
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[1] == dpid and "mockls.py" in parts[2]:
                os.kill(int(parts[0]), 9)
                return True
        return False

    check("killed the server once", kill_server_child(key5))
    time.sleep(2.5)
    check("killed the replacement too (budget now exhausted)", kill_server_child(key5))
    time.sleep(1.5)
    check("the client connection is still alive", hold.p.poll() is None)
    hold.send({"jsonrpc": "2.0", "id": 20, "method": "workspace/symbol",
               "params": {"query": "WhileDown"}})
    down = hold.await_id(20, timeout=15)
    check("a request while down is ANSWERED, not hung", down is not None, repr(down))

    time.sleep(3.5)                    
    hold.send({"jsonrpc": "2.0", "id": 21, "method": "workspace/symbol",
               "params": {"query": "Revived"}})
    back = hold.await_id(21, timeout=30)
    nm = (back or {}).get("result", [{}])[0].get("name") if back else None
    check("the server revives under the same client", nm == "SYM:Revived", repr(nm))
    hold.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key5],
                   capture_output=True)

    print("\n[11] a git-driven change re-seats the server's view (policy)")
    
    
    
    
    gws = os.path.join(tmp, "gitws")
    os.makedirs(gws)
    src = os.path.join(gws, "src.ts")
    with open(src, "w") as fh:
        fh.write("export const before = 1;\n")
    
    
    signkey = os.path.join(tmp, "signkey")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", signkey],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    os.chmod(signkey, 0o600)
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"],
                ["git", "config", "gpg.format", "ssh"],
                ["git", "config", "user.signingkey", signkey + ".pub"],
                ["git", "config", "commit.gpgsign", "true"],
                ["git", "add", "-A"],
                ["git", "commit", "-qm", "one"]):
        subprocess.run(cmd, cwd=gws, capture_output=True)

    gkey = key + "git"
    gjrn = os.path.join(tmp, "gitjournal")
    g = Client(gkey, gws, gjrn, env_extra={"LSPD_GIT_POLL": "1"})
    g.initialize(gws)
    guri = "file://" + src
    g.send({"jsonrpc": "2.0", "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": guri, "languageId": "typescript",
                                        "version": 1, "text": "export const before = 1;\n"}}})
    time.sleep(1.0)

    
    with open(src, "w") as fh:
        fh.write("export const after = 2;\n")
    subprocess.run(["git", "add", "-A"], cwd=gws, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "two"], cwd=gws, capture_output=True)

    time.sleep(5.0)                       
    gj = journal(gjrn)
    check("server was told the watched files changed",
          count(gj, "workspace/didChangeWatchedFiles") >= 1,
          "saw %d" % count(gj, "workspace/didChangeWatchedFiles"))
    reopened = [r for r in gj if r["kind"] == "textDocument/didOpen"
                and "after" in ((r["payload"].get("params") or {})
                                .get("textDocument", {}).get("text", ""))]
    check("the open document was re-seated with the NEW text on disk", bool(reopened),
          "no didOpen carried the post-commit text")
    
    
    kinds = [r["kind"] for r in gj]
    check("it was closed before being re-opened",
          "textDocument/didClose" in kinds
          and kinds.index("textDocument/didClose") < len(kinds) - 1,
          repr(kinds[-4:]))

    print("\n[12] --resync forces the same repair by hand")
    with open(src, "w") as fh:
        fh.write("export const manual = 3;\n")
    
    
    
    
    
    
    rc = subprocess.run([sys.executable, LSPD, "--resync", "--key", gkey, src],
                        capture_output=True, text=True)
    check("--resync exits clean", rc.returncode == 0, rc.stdout + rc.stderr)
    check("--resync reports how many documents it re-seated",
          "reseated=" in rc.stdout and "reseated=?" not in rc.stdout, rc.stdout)
    time.sleep(1.5)
    gj = journal(gjrn)
    manual = [r for r in gj if r["kind"] == "textDocument/didOpen"
              and "manual" in ((r["payload"].get("params") or {})
                               .get("textDocument", {}).get("text", ""))]
    check("--resync re-seated the document without any git change", bool(manual),
          "no didOpen carried the hand-edited text")
    g.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % gkey],
                   capture_output=True)

    print("\n[13] the restart replay never double-opens a document (policy)")
    
    
    
    
    
    
    
    
    
    
    
    
    jrn6 = os.path.join(tmp, "journal6")
    key6 = key + "dup"
    dup = os.path.join(ws, "dup.ts")
    with open(dup, "w") as fh:
        fh.write("export const dup = 1;\n")
    duri = "file://" + dup
    dopen = {"jsonrpc": "2.0", "method": "textDocument/didOpen",
             "params": {"textDocument": {"uri": duri, "languageId": "typescript",
                                         "version": 1, "text": "export const dup = 1;\n"}}}
    
    
    
    
    d = Client(key6, ws, jrn6, env_extra={"LSPD_REPLAY_PAUSE": "8.0"})
    d.initialize(ws)
    d.send(dopen)
    time.sleep(1.0)

    ps6 = subprocess.run(["/bin/ps", "-Ao", "pid,ppid,command"],
                         capture_output=True, text=True).stdout
    dpid = None
    for line in ps6.splitlines():
        if ("lspd.py --daemon --key %s " % key6) in line:
            dpid = int(line.split()[0])
            break
    victim6 = None
    if dpid:
        for line in ps6.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[1] == str(dpid) and "mockls.py" in parts[2]:
                victim6 = int(parts[0])
                break
    check("found the language server child to kill", victim6 is not None,
          "daemon=%s" % dpid)
    if victim6:
        os.kill(victim6, 9)
        
        
        
        
        time.sleep(4.0)
        d.send(dopen)
        time.sleep(13.0)                      
        j6 = journal(jrn6)
        kinds6 = [r["kind"] for r in j6]
        second = ([i for i, k in enumerate(kinds6) if k == "initialize"] + [None, None])[1]
        check("the replacement server got its own initialize", second is not None,
              repr(kinds6[:6]))
        if second is not None:
            after = [r for r in j6[second:]
                     if r["kind"] == "textDocument/didOpen"
                     and ((r["payload"].get("params") or {})
                          .get("textDocument", {}).get("uri")) == duri]
            check("exactly ONE didOpen for the restored document after the restart",
                  len(after) == 1, "saw %d" % len(after))
        d.send({"jsonrpc": "2.0", "id": 12, "method": "workspace/symbol",
                "params": {"query": "Survived"}})
        got = d.await_id(12, timeout=30)
        nm6 = (got or {}).get("result", [{}])[0].get("name") if got else None
        check("the server survived the replay and still answers",
              nm6 == "SYM:Survived", repr(nm6))
    d.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key6],
                   capture_output=True)

    print("\n[14] --restart replaces the server under a live client (obs #235b)")
    
    
    
    
    
    
    jrn7 = os.path.join(tmp, "journal7")
    key7 = key + "restart"
    rs = Client(key7, ws, jrn7)
    rs.initialize(ws)
    rs.send({"jsonrpc": "2.0", "id": 30, "method": "workspace/symbol",
             "params": {"query": "BeforeRestart"}})
    pre7 = rs.await_id(30)
    check("restart-test client answers before the restart",
          (pre7 or {}).get("result", [{}])[0].get("name") == "SYM:BeforeRestart"
          if pre7 else False, repr(pre7))

    def server_child(dkey):
        out = subprocess.run(["/bin/ps", "-Ao", "pid,ppid,command"],
                             capture_output=True, text=True).stdout
        dpid = None
        for line in out.splitlines():
            if ("lspd.py --daemon --key %s " % dkey) in line:
                dpid = line.split()[0]
                break
        if not dpid:
            return None
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[1] == dpid and "mockls.py" in parts[2]:
                return int(parts[0])
        return None

    before_pid = server_child(key7)
    check("found the server process the restart should replace", before_pid is not None)
    rc7 = subprocess.run([sys.executable, LSPD, "--restart", "--key", key7,
                          "--workspace", ws], capture_output=True, text=True)
    check("--restart exits clean", rc7.returncode == 0, rc7.stdout + rc7.stderr)
    
    
    
    check("--restart reports the pid it replaced and the handshake",
          "pid %s ->" % before_pid in rc7.stdout and "handshaked=yes" in rc7.stdout,
          rc7.stdout)
    after_pid = server_child(key7)
    check("the server is a DIFFERENT process now",
          after_pid is not None and after_pid != before_pid,
          "before=%s after=%s" % (before_pid, after_pid))
    check("the old server is gone (not leaked alongside the new one)",
          before_pid is not None and not alive_pid(before_pid), "pid %s" % before_pid)
    rs.send({"jsonrpc": "2.0", "id": 31, "method": "workspace/symbol",
             "params": {"query": "AfterRestart"}})
    post7 = rs.await_id(31, timeout=30)
    nm7 = (post7 or {}).get("result", [{}])[0].get("name") if post7 else None
    check("the SAME client still answers -- the socket never moved",
          nm7 == "SYM:AfterRestart", repr(nm7))
    j7 = journal(jrn7)
    check("the replacement server got its own initialize",
          count(j7, "initialize") == 2, "saw %d" % count(j7, "initialize"))

    
    
    bare = subprocess.run([sys.executable, LSPD, "--restart"],
                          capture_output=True, text=True)
    check("--restart with no --key is REFUSED rather than swept",
          bare.returncode != 0 and "--key" in (bare.stderr + bare.stdout),
          bare.stdout + bare.stderr)
    rs.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key7],
                   capture_output=True)

    print("\n[15] a server that is alive and answering EMPTY is replaced (policy)")
    
    
    
    
    
    def canary_status(dkey):
        out = subprocess.run([sys.executable, LSPD, "--status"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if dkey in line:
                return line
        return ""

    key8 = key + "can"
    jrn8 = os.path.join(tmp, "canjournal")
    deg = os.path.join(tmp, "degrade")
    cws = os.path.join(tmp, "canws")
    os.makedirs(cws)
    csrc = os.path.join(cws, "probe.ts")
    with open(csrc, "w") as fh:
        fh.write("export function probe() { return 1; }\n")
    c8 = Client(key8, cws, jrn8, env_extra={
        "MOCKLS_REFS": "3", "MOCKLS_DEGRADE_FILE": deg,
        "LSPD_CANARY_POLL": "1", "LSPD_CANARY_MIN_GAP": "0",
        "LSPD_CANARY_STRIKES": "2", "LSPD_CANARY_COOLDOWN": "0"})
    c8.initialize(cws)
    c8.send({"jsonrpc": "2.0", "method": "textDocument/didOpen",
             "params": {"textDocument": {"uri": "file://" + csrc,
                                         "languageId": "typescript", "version": 1,
                                         "text": "export function probe() {}\n"}}})
    time.sleep(4.0)
    line8 = canary_status(key8)
    
    
    check("the canary calibrates itself off a document the server loaded",
          "canary=ok" in line8 and "baseline=3" in line8, repr(line8))
    pid8 = server_child(key8)
    open(deg, "w").close()             
    time.sleep(8.0)                    
    new8 = server_child(key8)
    check("the empty-answering server is replaced under its client",
          new8 is not None and pid8 is not None and new8 != pid8,
          "before=%s after=%s" % (pid8, new8))
    check("--status reports answer quality, not just liveness",
          "restarts=1" in canary_status(key8), repr(canary_status(key8)))
    os.unlink(deg)                     
    time.sleep(5.0)
    check("the canary recalibrates once real answers come back",
          "canary=ok" in canary_status(key8), repr(canary_status(key8)))
    c8.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key8],
                   capture_output=True)

    print("\n[16] a restart whose replacement cannot start says so, and --status says DOWN"
          " (policy)")
    
    
    
    
    
    
    
    key9 = key + "die"
    jrn9 = os.path.join(tmp, "diejournal")
    die = os.path.join(tmp, "die")
    dws = os.path.join(tmp, "diews")
    os.makedirs(dws)
    c9 = Client(key9, dws, jrn9, env_extra={"MOCKLS_DIE_FILE": die})
    c9.initialize(dws)
    live9 = canary_status(key9)
    check("a healthy daemon still reports LIVE", live9.startswith("LIVE"), repr(live9))
    was9 = server_child(key9)
    open(die, "w").close()               
    rc9 = subprocess.run([sys.executable, LSPD, "--restart", "--key", key9,
                          "--workspace", dws], capture_output=True, text=True)
    check("--restart REFUSES to call a failed replacement OK",
          rc9.returncode != 0, "rc=%d %s" % (rc9.returncode, rc9.stdout + rc9.stderr))
    check("--restart says the replacement never answered initialize",
          "NEVER ANSWERED initialize" in rc9.stdout, repr(rc9.stdout))
    
    check("--restart says the old server is gone and the project has no code intelligence",
          "old server is GONE" in rc9.stdout and "NO code intelligence" in rc9.stdout,
          repr(rc9.stdout))
    check("the old server really is dead",
          was9 is not None and not alive_pid(was9), "pid %s" % was9)
    st9 = subprocess.run([sys.executable, LSPD, "--status"],
                         capture_output=True, text=True)
    line9 = [l for l in st9.stdout.splitlines() if key9 in l]
    line9 = line9[0] if line9 else ""
    check("--status reports DOWN, not LIVE, when the server is not running",
          line9.startswith("DOWN"), repr(line9))
    check("--status explains that symbol questions will fail",
          "every symbol question fails" in line9, repr(line9))
    check("--status exits non-zero while any server is DOWN", st9.returncode != 0,
          "rc=%d" % st9.returncode)

    
    
    
    
    own = subprocess.run([sys.executable, LSPD, "--status", "--key", key],
                         capture_output=True, text=True)
    check("--status --key exits 0 for a LIVE key while another daemon is DOWN",
          own.returncode == 0, "rc=%d %s" % (own.returncode, own.stdout))
    check("--status --key prints only that key's rows, not keys it is a prefix of",
          ("%s-" % key) in own.stdout and key9 not in own.stdout
          and (key + "idx") not in own.stdout, repr(own.stdout))
    down9 = subprocess.run([sys.executable, LSPD, "--status", "--key", key9],
                           capture_output=True, text=True)
    check("--status --key exits 1 when THAT key is DOWN", down9.returncode == 1,
          "rc=%d %s" % (down9.returncode, down9.stdout))
    check("--status --key still prints that key's DOWN row",
          any(l.startswith("DOWN") and key9 in l for l in down9.stdout.splitlines()),
          repr(down9.stdout))
    check("unscoped --status still lists every daemon",
          key9 in st9.stdout and ("%s-" % key) in st9.stdout, repr(st9.stdout))
    
    
    ghost = os.path.join(os.path.expanduser("~"), ".local", "state", "agent-context", "lsp", "run",
                         "%sghost-0000000000000000.sock" % key)
    open(ghost, "w").close()
    none9 = subprocess.run([sys.executable, LSPD, "--status", "--key", key + "none"],
                           capture_output=True, text=True)
    check("--status --key with no daemon for that key exits 0 and lists nothing else",
          none9.returncode == 0 and key9 not in none9.stdout,
          "rc=%d %s" % (none9.returncode, none9.stdout))
    check("--status --key never deletes ANOTHER key's stale socket",
          os.path.exists(ghost), repr(none9.stdout))
    try:
        os.unlink(ghost)
    except OSError:
        pass
    c9.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key9],
                   capture_output=True)

    print("\n[17] --upgrade retires a daemon pinned to an old lspd build (policy)")
    
    
    
    
    
    
    
    
    
    
    
    key10 = key + "upg"
    jrn10 = os.path.join(tmp, "upgjournal")
    uws = os.path.join(tmp, "upgws")
    os.makedirs(uws)
    alt_dir = os.path.join(tmp, "altbuild")
    os.makedirs(alt_dir)
    alt = os.path.join(alt_dir, "lspd.py")
    shutil.copyfile(LSPD, alt)
    shutil.copyfile(os.path.join(HERE, "harness_paths.py"), os.path.join(alt_dir, "harness_paths.py"))

    def alt_run(*argv):
        return subprocess.run([sys.executable, alt] + list(argv),
                              capture_output=True, text=True)

    def alt_status_line(dkey):
        out = alt_run("--status").stdout
        keep = False
        rows = []
        for line in out.splitlines():
            if dkey in line:
                keep = True
                rows.append(line)
                continue
            
            
            if keep and line.startswith(" "):
                rows.append(line)
            else:
                keep = False
        return "\n".join(rows)

    def daemon_pid_for(dkey):
        out = subprocess.run(["/bin/ps", "-Ao", "pid,command"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if ("lspd.py --daemon --key %s " % dkey) in line:
                return int(line.split()[0])
        return None

    u = Client(key10, uws, jrn10, lspd_path=alt)
    r10 = u.initialize(uws)
    check("upgrade-test client initialized", r10 is not None and "result" in (r10 or {}),
          repr(r10))
    was10 = daemon_pid_for(key10)
    check("found the daemon process", was10 is not None)
    fresh = alt_status_line(key10)
    
    
    
    check("a current daemon says NOTHING about its code", "code=" not in fresh,
          repr(fresh))
    
    
    
    check("--status reports how many sessions are attached", "clients=1" in fresh,
          repr(fresh))

    
    
    
    with open(alt, "a") as fh:
        fh.write("\n# policy test: a cosmetic edit lands on disk under a running daemon\n")
    cosmetic = alt_status_line(key10)
    check("a comment-only edit does NOT stale the daemon (key is LSPD_PROTOCOL, not sha)",
          "code=STALE" not in cosmetic, repr(cosmetic))

    
    
    with open(alt) as fh:
        src = fh.read()
    assert "LSPD_PROTOCOL = " in src, "the protocol constant moved; update this test"
    bumped = re.sub(r"^LSPD_PROTOCOL = (\d+)$",
                    lambda m: "LSPD_PROTOCOL = %d" % (int(m.group(1)) + 1), src,
                    count=1, flags=re.M)
    assert bumped != src
    with open(alt, "w") as fh:
        fh.write(bumped)
    stale = alt_status_line(key10)
    check("--status reports the daemon is running an OLD build",
          "code=STALE" in stale, repr(stale))
    check("--status names BOTH protocol numbers so the reader sees what changed",
          "protocol" in stale, repr(stale))
    check("--status names the command that fixes it",
          "--upgrade --key %s" % key10 in stale, repr(stale))
    
    
    
    
    check("--status still exits 0 -- stale is not the same as broken",
          alt_run("--status", "--key", key10).returncode == 0)

    
    
    busy = alt_run("--upgrade", "--key", key10, "--workspace", uws)
    check("--upgrade REFUSES while a client is attached", busy.returncode != 0,
          "rc=%d %s" % (busy.returncode, busy.stdout + busy.stderr))
    check("--upgrade says how many clients would lose their server",
          "client(s) are attached" in busy.stdout, repr(busy.stdout))
    check("the daemon is untouched by a refused upgrade",
          daemon_pid_for(key10) == was10)

    u.kill()
    time.sleep(2.0)
    done = alt_run("--upgrade", "--key", key10, "--workspace", uws)
    check("--upgrade exits clean once nothing is attached", done.returncode == 0,
          "rc=%d %s" % (done.returncode, done.stdout + done.stderr))
    check("--upgrade reports the daemon it retired", "retired daemon pid" in done.stdout,
          repr(done.stdout))
    check("the old daemon process is GONE",
          was10 is not None and not alive_pid(was10), "pid %s" % was10)

    
    
    u2 = Client(key10, uws, jrn10, lspd_path=alt)
    r10b = u2.initialize(uws)
    check("a client attaching afterwards gets a working daemon",
          r10b is not None and "result" in (r10b or {}), repr(r10b))
    new10 = daemon_pid_for(key10)
    check("it is a DIFFERENT daemon process", new10 is not None and new10 != was10,
          "before=%s after=%s" % (was10, new10))
    after = alt_status_line(key10)
    check("--status no longer reports a stale build", "code=STALE" not in after,
          repr(after))

    
    
    
    noop = alt_run("--upgrade", "--key", key10, "--workspace", uws)
    check("--upgrade on a current daemon does nothing and says so",
          noop.returncode == 0 and "nothing to do" in noop.stdout, repr(noop.stdout))
    check("the current daemon survived the no-op", daemon_pid_for(key10) == new10)

    
    
    
    
    
    old_health = {"key": key10, "clients": 0, "canary": {"state": "uncalibrated"}}
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("lspd_under_test", alt)
    assert _spec is not None and _spec.loader is not None, "cannot load %s" % alt
    _lspd = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_lspd)
    check("a daemon with no code field reads STALE, not unknown",
          _lspd.code_line(old_health, key10).startswith("code=STALE"),
          repr(_lspd.code_line(old_health, key10)))
    check("and it names the fix",
          "--upgrade --key %s" % key10 in _lspd.code_line(old_health, key10),
          repr(_lspd.code_line(old_health, key10)))
    check("a daemon too old to answer health at all also reads STALE",
          _lspd.code_line(None, key10).startswith("code=STALE"),
          repr(_lspd.code_line(None, key10)))

    bare10 = alt_run("--upgrade")
    check("--upgrade with no --key is REFUSED rather than swept",
          bare10.returncode != 0 and "--key" in (bare10.stderr + bare10.stdout),
          bare10.stdout + bare10.stderr)
    u2.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key10],
                   capture_output=True)

    print("\n[18] the canary looks past a document with no symbols")
    
    
    
    
    
    
    def open_doc(client, path, text):
        client.send({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                     "params": {"textDocument": {"uri": "file://" + path,
                                                 "languageId": "typescript",
                                                 "version": 1, "text": text}}})

    def opens_of(jpath, path, start=0):
        return sum(1 for r in journal(jpath)[start:]
                   if r["kind"] == "textDocument/didOpen"
                   and ((r["payload"].get("params") or {}).get("textDocument", {})
                        .get("uri")) == "file://" + path)

    def write_files(root, names_texts):
        os.makedirs(root)
        paths = []
        for name, text in names_texts:
            p = os.path.join(root, name)
            with open(p, "w") as fh:
                fh.write(text)
            paths.append(p)
        return paths

    no_sym = "// nothing here\n"
    has_sym = "export function probe() {}\n"
    canary_env = {"MOCKLS_REFS": "3", "MOCKLS_NO_SYMBOLS": "empty",
                  "LSPD_CANARY_POLL": "1", "LSPD_CANARY_MIN_GAP": "0",
                  "LSPD_SEED_SETTLE": "0.2"}

    
    key11 = key + "nosym"
    jrn11 = os.path.join(tmp, "nosymjournal")
    nws = os.path.join(tmp, "nosymws")
    empty11, probe11 = write_files(nws, [("a_empty.ts", no_sym), ("b_probe.ts", has_sym)])
    c11 = Client(key11, nws, jrn11, env_extra=canary_env)
    c11.initialize(nws)
    open_doc(c11, empty11, no_sym)
    open_doc(c11, probe11, has_sym)
    time.sleep(5.0)
    line11 = canary_status(key11)
    check("the canary calibrates off the open document that HAS symbols",
          "canary=ok" in line11 and "baseline=3" in line11, repr(line11))
    c11.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key11],
                   capture_output=True)

    
    
    
    key12 = key + "seedsym"
    jrn12 = os.path.join(tmp, "seedsymjournal")
    sws = os.path.join(tmp, "seedsymws")
    empty12, probe12 = write_files(sws, [("a_empty.ts", no_sym), ("b_probe.ts", has_sym)])
    c12 = Client(key12, sws, jrn12,
                 env_extra=dict(canary_env, LSPD_SEED="*.ts:typescript"))
    c12.initialize(sws)
    time.sleep(6.0)
    line12 = canary_status(key12)
    check("a seed with no symbols is not the end: the canary still calibrates",
          "canary=ok" in line12 and "baseline=3" in line12, repr(line12))
    check("the seed was opened exactly once", opens_of(jrn12, empty12) == 1,
          "saw %d" % opens_of(jrn12, empty12))
    check("the next candidate was opened exactly once", opens_of(jrn12, probe12) == 1,
          "saw %d" % opens_of(jrn12, probe12))
    
    
    open_doc(c12, probe12, has_sym)
    c12.send({"jsonrpc": "2.0", "method": "textDocument/didClose",
              "params": {"textDocument": {"uri": "file://" + probe12}}})
    time.sleep(1.0)
    check("a client's didClose does not close the daemon's canary document",
          count(journal(jrn12), "textDocument/didClose") == 0,
          "saw %d" % count(journal(jrn12), "textDocument/didClose"))
    rc12 = subprocess.run([sys.executable, LSPD, "--restart", "--key", key12,
                           "--workspace", sws], capture_output=True, text=True)
    check("--restart under a canary document exits clean", rc12.returncode == 0,
          rc12.stdout + rc12.stderr)
    time.sleep(3.0)
    j12 = journal(jrn12)
    inits12 = [i for i, r in enumerate(j12) if r["kind"] == "initialize"]
    reopened12 = opens_of(jrn12, probe12, inits12[-1]) if len(inits12) >= 2 else -1
    check("a restart reopens the daemon's canary document exactly once (policy)",
          reopened12 == 1, "initializes=%d opens=%d" % (len(inits12), reopened12))
    c12.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key12],
                   capture_output=True)

    
    
    
    key13 = key + "allempty"
    jrn13 = os.path.join(tmp, "allemptyjournal")
    aws = os.path.join(tmp, "allemptyws")
    write_files(aws, [("empty_%d.ts" % i, no_sym) for i in range(6)])
    c13 = Client(key13, aws, jrn13,
                 env_extra=dict(canary_env, LSPD_SEED="*.ts:typescript",
                                LSPD_CANARY_CANDIDATES="2"))
    c13.initialize(aws)
    time.sleep(8.0)
    line13 = canary_status(key13)
    check("with no symbols anywhere the canary says no-symbols",
          "canary=no-symbols" in line13, repr(line13))
    opened13 = count(journal(jrn13), "textDocument/didOpen")
    check("it opens the seed plus LSPD_CANARY_CANDIDATES more, and no more across polls",
          opened13 == 3, "saw %d" % opened13)
    c13.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key13],
                   capture_output=True)

    
    
    
    
    key14 = key + "flat"
    jrn14 = os.path.join(tmp, "flatjournal")
    fws = os.path.join(tmp, "flatws")
    (probe14,) = write_files(fws, [("probe.ts", has_sym)])
    c14 = Client(key14, fws, jrn14, env_extra=dict(canary_env, MOCKLS_FLAT_SYMBOLS="1"))
    c14.initialize(fws)
    open_doc(c14, probe14, has_sym)
    time.sleep(5.0)
    line14 = canary_status(key14)
    check("a flat symbol whose range starts at the keyword still calibrates",
          "canary=ok" in line14 and "baseline=3" in line14, repr(line14))
    c14.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key14],
                   capture_output=True)

    
    
    key15 = key + "nullrefs"
    jrn15 = os.path.join(tmp, "nullrefsjournal")
    rws = os.path.join(tmp, "nullrefsws")
    null15, probe15 = write_files(rws, [("a_null.ts", has_sym), ("b_probe.ts", has_sym)])
    c15 = Client(key15, rws, jrn15, env_extra=dict(canary_env, MOCKLS_NULL_REFS="a_null"))
    c15.initialize(rws)
    open_doc(c15, null15, has_sym)
    open_doc(c15, probe15, has_sym)
    time.sleep(5.0)
    line15 = canary_status(key15)
    check("a null references answer moves the canary on to the next document",
          "canary=ok" in line15 and "baseline=3" in line15, repr(line15))
    c15.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key15],
                   capture_output=True)

    
    
    
    key16 = key + "decorated"
    jrn16 = os.path.join(tmp, "decoratedjournal")
    decws = os.path.join(tmp, "decoratedws")
    decorated = "// @decorator\n" + has_sym
    (probe16,) = write_files(decws, [("probe.ts", decorated)])
    c16 = Client(key16, decws, jrn16, env_extra=dict(canary_env, MOCKLS_FLAT_SYMBOLS="1"))
    c16.initialize(decws)
    open_doc(c16, probe16, decorated)
    time.sleep(5.0)
    line16 = canary_status(key16)
    check("a flat symbol whose range starts on a decorator line still calibrates",
          "canary=ok" in line16 and "baseline=3" in line16, repr(line16))
    c16.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key16],
                   capture_output=True)

    print("\n[19] a file deleted and recreated under --resync reaches the server again")
    
    
    
    
    
    
    def text_opens(jpath, path, needle, start=0):
        return sum(1 for r in journal(jpath)[start:]
                   if r["kind"] == "textDocument/didOpen"
                   and ((r["payload"].get("params") or {}).get("textDocument", {})
                        .get("uri")) == "file://" + path
                   and needle in ((r["payload"].get("params") or {})
                                  .get("textDocument", {}).get("text", "")))

    def closes_of(jpath, path, start=0):
        return sum(1 for r in journal(jpath)[start:]
                   if r["kind"] == "textDocument/didClose"
                   and ((r["payload"].get("params") or {}).get("textDocument", {})
                        .get("uri")) == "file://" + path)

    def doc_events(jpath, path, start=0):
        return [r["kind"] for r in journal(jpath)[start:]
                if r["kind"] in ("textDocument/didOpen", "textDocument/didClose")
                and ((r["payload"].get("params") or {}).get("textDocument", {})
                     .get("uri")) == "file://" + path]

    def resync_one(dkey, path):
        return subprocess.run([sys.executable, LSPD, "--resync", "--key", dkey, path],
                              capture_output=True, text=True)

    def close_doc(client, path):
        client.send({"jsonrpc": "2.0", "method": "textDocument/didClose",
                     "params": {"textDocument": {"uri": "file://" + path}}})

    key19 = key + "reborn"
    jrn19 = os.path.join(tmp, "rebornjournal")
    bws = os.path.join(tmp, "rebornws")
    (seed19,) = write_files(bws, [("seed.ts", "export const one = 1;\n")])
    env19 = {"LSPD_SEED": "*.ts:typescript", "LSPD_SEED_SETTLE": "0.2", "LSPD_CANARY": "0"}
    c19 = Client(key19, bws, jrn19, env_extra=env19)
    c19.initialize(bws)
    time.sleep(3.0)
    check("precondition: the daemon seeded seed.ts and holds it",
          opens_of(jrn19, seed19) == 1, "saw %d" % opens_of(jrn19, seed19))

    
    os.unlink(seed19)
    rc19 = resync_one(key19, seed19)
    time.sleep(1.0)
    check("--resync closes the deleted seed on the server",
          rc19.returncode == 0 and closes_of(jrn19, seed19) == 1,
          "rc=%d closes=%d %s" % (rc19.returncode, closes_of(jrn19, seed19), rc19.stdout))
    mark19 = len(journal(jrn19))
    with open(seed19, "w") as fh:
        fh.write("export const two = 2;\n")
    open_doc(c19, seed19, "export const two = 2;\n")
    time.sleep(1.0)
    check("a client didOpen for the recreated seed reaches the server exactly once",
          doc_events(jrn19, seed19, mark19) == ["textDocument/didOpen"]
          and text_opens(jrn19, seed19, "two", mark19) == 1,
          repr(doc_events(jrn19, seed19, mark19)))

    
    mark19b = len(journal(jrn19))
    with open(seed19, "w") as fh:
        fh.write("export const three = 3;\n")
    resync_one(key19, seed19)
    time.sleep(1.0)
    check("a second --resync re-seats it: didClose, then didOpen with the current text",
          doc_events(jrn19, seed19, mark19b)
          == ["textDocument/didClose", "textDocument/didOpen"]
          and text_opens(jrn19, seed19, "three", mark19b) == 1,
          repr(doc_events(jrn19, seed19, mark19b)))

    
    
    held19 = os.path.join(bws, "held.txt")
    with open(held19, "w") as fh:
        fh.write("alpha\n")
    open_doc(c19, held19, "alpha\n")
    b19 = Client(key19, bws, jrn19, env_extra=env19)
    b19.initialize(bws)
    time.sleep(1.0)
    os.unlink(held19)
    resync_one(key19, held19)
    time.sleep(1.0)
    mark19c = len(journal(jrn19))
    with open(held19, "w") as fh:
        fh.write("beta\n")
    open_doc(b19, held19, "beta\n")
    time.sleep(1.0)
    check("another client's didOpen for a recreated, still-held file reaches the server once",
          doc_events(jrn19, held19, mark19c) == ["textDocument/didOpen"]
          and text_opens(jrn19, held19, "beta", mark19c) == 1,
          repr(doc_events(jrn19, held19, mark19c)))
    
    close_doc(c19, held19)
    time.sleep(0.8)
    check("the first client's didClose is suppressed (the other still holds it)",
          closes_of(jrn19, held19, mark19c) == 0,
          "saw %d" % closes_of(jrn19, held19, mark19c))
    close_doc(b19, held19)
    time.sleep(0.8)
    check("the last client's didClose reaches the server exactly once",
          closes_of(jrn19, held19, mark19c) == 1,
          "saw %d" % closes_of(jrn19, held19, mark19c))
    c19.kill()
    b19.kill()
    subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % key19],
                   capture_output=True)

    
    
    
    
    _spec19 = _ilu.spec_from_file_location("lspd_open_held", LSPD)
    assert _spec19 is not None and _spec19.loader is not None, "cannot load %s" % LSPD
    _l19 = _ilu.module_from_spec(_spec19)
    _spec19.loader.exec_module(_l19)
    
    unit = _l19.Daemon(key + "held", bws, ["true"])
    sent19 = []
    unit.to_server = sent19.append
    upath = os.path.join(bws, "unit.ts")
    with open(upath, "w") as fh:
        fh.write("export const u = 1;\n")
    uuri = "file://" + upath
    unit.doc_refs = {uuri: {42}}             
    unit.doc_open_sent = set()               
    got19 = unit.open_held(upath, "typescript")
    check("open_held opens a document a client holds but the server does not",
          got19 == (uuri, False)
          and [m.get("method") for m in sent19] == ["textDocument/didOpen"]
          and uuri in unit.doc_open_sent and 0 in unit.doc_refs.get(uuri, ()),
          repr((got19, sent19)))
    del sent19[:]
    again19 = unit.open_held(upath, "typescript")
    check("open_held sends nothing for a document the server already has",
          again19 == (uuri, True) and sent19 == [], repr((again19, sent19)))

    print("\n[20] the canary probes the symbol's NAME, in the text the server holds")
    
    
    
    
    
    nws20 = os.path.join(tmp, "namesws")
    astral = 'const s = "\U0001F600"; export function probe() {}'
    deco_src = '@route("probe")\ndef probe():\n    return 1\n'
    only_at = "@Dec() class probe {}\n"
    ascii_src = "export function probe() {}\n"
    astral20, deco20, at20, ascii20 = write_files(nws20, [
        ("astral.ts", astral + "\nprobe();\n"), ("deco.py", deco_src),
        ("only_at.ts", only_at), ("ascii.ts", ascii_src)])
    u20 = _l19.Daemon(key + "unit", nws20, ["true"])
    sent20 = []
    u20.to_server = sent20.append

    def moved(path, line, character, name="probe"):
        return u20.name_positions("file://" + path,
                                  [(name, {"line": line, "character": character})])[0][1]

    def at(line, character):
        return {"line": line, "character": character}

    col16 = len(astral[:astral.find("probe")].encode("utf-16-le")) // 2

    
    got = moved(astral20, 0, col16)
    check("a position already on the name, past an astral character, stays put",
          got == at(0, col16), "want %r got %r" % (at(0, col16), got))
    got = moved(astral20, 0, 0)
    check("a range starting at column 0 lands on the name's UTF-16 column",
          got == at(0, col16), "want %r got %r" % (at(0, col16), got))
    got = moved(ascii20, 0, 0)
    check("an ASCII line keeps the column it always had",
          got == at(0, ascii_src.find("probe")), repr(got))

    
    got = moved(deco20, 0, 0)
    check("a decorator argument spelling the name does not take the probe",
          got == at(1, 4), "want line 1 col 4, got %r" % (got,))
    got = moved(at20, 0, 0)
    check("a name that appears only on a decorator line is still used",
          got == at(0, only_at.find("probe")), repr(got))

    
    buf20 = os.path.join(nws20, "buffer.py")
    buri = "file://" + buf20

    def disk(text):
        with open(buf20, "w") as fh:
            fh.write(text)

    def notify(cid, method, **params):
        u20.from_client(cid, {"jsonrpc": "2.0", "method": method,
                              "params": dict(params, textDocument=dict(
                                  {"uri": buri}, **params.get("textDocument", {})))})

    disk("def probe():\n    return 1\n")                     
    held = "# one\n# two\ndef probe():\n    return 1\n"      
    notify(7, "textDocument/didOpen",
           textDocument={"languageId": "python", "version": 1, "text": held})
    got = moved(buf20, 2, 0)
    check("a forwarded didOpen's text is what the probe is placed from",
          got == at(2, 4), repr(got))

    notify(7, "textDocument/didChange", textDocument={"version": 2},
           contentChanges=[{"text": "# a\n# b\n# c\ndef probe():\n    return 2\n"}])
    got = moved(buf20, 3, 0)
    check("a whole-document didChange replaces that text", got == at(3, 4), repr(got))
    
    
    notify(7, "textDocument/didChange", textDocument={"version": 3}, contentChanges=[])
    got = moved(buf20, 3, 0)
    check("an empty contentChanges list leaves that text alone", got == at(3, 4), repr(got))
    notify(7, "textDocument/didChange", textDocument={"version": 3},
           contentChanges=[{"range": {"start": at(0, 0), "end": at(0, 0)}, "text": "x"}])
    got = moved(buf20, 0, 0)
    check("a ranged didChange drops it, so the search falls back to disk",
          got == at(0, 4), repr(got))

    notify(7, "textDocument/didChange", textDocument={"version": 4},
           contentChanges=[{"text": held}])
    notify(7, "textDocument/didClose")
    got = moved(buf20, 2, 0)
    check("the last didClose drops it", got == at(2, 0), repr(got))

    notify(8, "textDocument/didOpen",
           textDocument={"languageId": "python", "version": 1, "text": held})
    disk("\n\n\n\ndef probe():\n")
    u20.resync([buf20])
    got = moved(buf20, 4, 0)
    check("a resync re-seat hands over the disk's text, and that is what is searched",
          got == at(4, 4), repr(got))
    os.unlink(buf20)
    u20.resync([buf20])
    got = moved(buf20, 0, 0)
    check("a resync of a deleted file drops it", got == at(0, 0), repr(got))

    disk("def probe():\n")
    u20.open_held(buf20, "python")
    disk("\n\ndef probe():\n")                               
    got = moved(buf20, 0, 0)
    check("open_held's text is what is searched", got == at(0, 4), repr(got))

    u20.start_server = lambda: True
    u20.restart_child()
    got = moved(buf20, 0, 0)
    check("a restart drops it", got == at(2, 4), repr(got))
    disk("\n\n\n\n\ndef probe():\n")
    u20.reinit_done.set()
    u20.reinitialize({"capabilities": {}}, [buri])
    disk("def probe():\n")                                   
    got = moved(buf20, 5, 0)
    check("the restart replay's text is what is searched", got == at(5, 4), repr(got))

    print("\n[21] a Cargo crate that moves restarts rust-analyzer, with no git change")
    
    
    
    
    
    mws = os.path.join(tmp, "manifestunit")
    for rel in ("a/Cargo.toml", ".hidden/Cargo.toml", "deep/x/Cargo.toml", "plain/README"):
        os.makedirs(os.path.dirname(os.path.join(mws, rel)), exist_ok=True)
        open(os.path.join(mws, rel), "w").close()
    got21 = _l19.cargo_manifests(mws)
    check("cargo_manifests takes each direct child's Cargo.toml, hidden dirs included, "
          "and nothing deeper",
          got21 == frozenset([os.path.join(mws, "a", "Cargo.toml"),
                              os.path.join(mws, ".hidden", "Cargo.toml")]), repr(got21))
    open(os.path.join(mws, "Cargo.toml"), "w").close()
    got21 = _l19.cargo_manifests(mws)
    check("a Cargo.toml at the root is the whole answer, as in rust-analyzer",
          got21 == frozenset([os.path.join(mws, "Cargo.toml")]), repr(got21))
    check("a manifest found in a parent directory is the whole answer",
          _l19.cargo_manifests(os.path.join(mws, "plain"))
          == frozenset([os.path.join(mws, "Cargo.toml")]))

    
    
    rws = os.path.join(tmp, "rustws")
    os.makedirs(os.path.join(rws, "sub", "src"))
    with open(os.path.join(rws, "sub", "Cargo.toml"), "w") as fh:
        fh.write('[package]\nname = "sub"\nversion = "0.1.0"\n')
    with open(os.path.join(rws, "sub", "src", "lib.rs"), "w") as fh:
        fh.write("pub fn probe() {}\n")
    rjrn = os.path.join(tmp, "rustjournal")
    rkill = ["pkill", "-f", "lspd.py --daemon --key rust-analyzer --workspace %s" % rws]
    r = Client("rust-analyzer", rws, rjrn,
               env_extra={"LSPD_GIT_POLL": "0.5", "LSPD_CANARY": "0"})
    try:
        r.initialize(rws)
        time.sleep(2.0)                   
        check("no restart while the manifest set is unchanged",
              count(journal(rjrn), "initialize") == 1,
              "saw %d initialize" % count(journal(rjrn), "initialize"))
        os.rename(os.path.join(rws, "sub"), os.path.join(rws, "other"))
        end = time.time() + 10
        while time.time() < end and count(journal(rjrn), "initialize") < 2:
            time.sleep(0.25)
        check("moving the crate replaced the server and replayed its handshake",
              count(journal(rjrn), "initialize") == 2,
              "saw %d initialize" % count(journal(rjrn), "initialize"))
        time.sleep(2.0)                   
        check("the new set was recorded, so the server is replaced once, not every poll",
              count(journal(rjrn), "initialize") == 2,
              "saw %d initialize" % count(journal(rjrn), "initialize"))
    finally:
        r.kill()
        subprocess.run(rkill, capture_output=True)

    
    
    time.sleep(0.5)
    for k in (key, key + "idx", key + "git", key + "can", key + "die",
              key + "nosym", key + "seedsym", key + "allempty", key + "flat",
              key + "nullrefs", key + "decorated", key + "reborn"):
        subprocess.run(["pkill", "-f", "lspd.py --daemon --key %s " % k],
                       capture_output=True)
    time.sleep(0.8)
    leaked = subprocess.run(["/bin/ps", "-Ao", "command"],
                            capture_output=True, text=True).stdout
    check("test leaves no daemon behind",
          ("--key %s " % key) not in leaked and ("--key %sidx " % key) not in leaked)
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(run())
