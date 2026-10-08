#!/usr/bin/env python3
"Test battery for preflight-core-health's LSP verdict reading.\n\nEvery case here is a FALSE ALARM this function actually produced. That is the\nwhole reason the file exists: `lsp_findings` is the one place in the fleet that\ndecides whether a session opens by declaring its language server degraded, and it\nhas now been wrong three separate ways --\n\nNone of the three was a crash. Each produced a confident, plausible, WRONG banner,\nand the instruction attached to that banner tells the agent to distrust negative\nsymbol answers -- so a false alarm here does not merely annoy, it degrades the\nsession's evidence standard on purpose. A banner that is routinely wrong is one\neverybody learns to skim, which is exactly how a real degradation gets missed.\n\nRun: python3 test-preflight-core-health.py"
import importlib.util
import json
import os
import sys
import tempfile
import time

HOOK = os.path.join(os.path.expanduser("~"), ".agent-context", "global", "hooks",
                    "preflight-core-health.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if detail and not cond else ""))


def load_module(name, path):
    'Import a hook/script by path, or None if it is not projected.'
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load spec for %s from %s" % (name, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_hook():
    return load_module("preflight_core_health", HOOK)


def read_json_file(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


class Bed:
    'A throwaway health-state dir + canary config, wired into the module.\n\n    The hook resolves everything off module-level constants, so redirecting those\n    is enough to run the real function against a synthetic machine. `bridge_serving`\n    is stubbed to False throughout: it shells out to ps, and every case here is\n    about what the VERDICT FILES say, not about what is running.'

    def __init__(self, mod, project="Developer/example-workspace/example-web"):
        self.mod = mod
        self.tmp = tempfile.mkdtemp(prefix="preflight-test-")
        self.state = os.path.join(self.tmp, "health")
        os.makedirs(os.path.join(self.state, "lsp"))
        self.home = os.path.join(self.tmp, "home")
        self.cwd = os.path.join(self.home, project)
        os.makedirs(self.cwd)
        self.config = os.path.join(self.tmp, "lsp-canaries.json")
        self.project = project

        mod.STATE = self.state
        mod.HOME = self.home
        mod.CANARY_CONFIG = self.config
        mod.bridge_serving = lambda cwd, server: False

    def roster(self, entries):
        'entries: [{"server":..., "symbol":...}] configured for the project.'
        with open(self.config, "w") as fh:
            json.dump({"canaries": {self.project: entries}}, fh)

    def verdict(self, server, ok=True, age_hours=0, symbol=None, cwd=None,
                detail=None):
        rec = {"server": server, "ok": ok, "cwd": cwd or self.cwd,
               "ts": time.time() - age_hours * 3600}
        if symbol:
            rec["symbol"] = symbol
        if detail:
            rec["detail"] = detail
        name = (cwd or self.cwd).replace(os.sep, "-") + "-" + server + ".json"
        with open(os.path.join(self.state, "lsp", name), "w") as fh:
            json.dump(rec, fh)

    def findings(self):
        
        
        return self.mod.lsp_findings(self.cwd, time.time() - 30 * 86400)

    def servers_reported(self):
        return sorted(w[len("LSP "):] for w, _ in self.findings())


def main():
    if not os.path.exists(HOOK):
        print("X hook not found at %s (run home-materialize.py)" % HOOK)
        return 1
    mod = load_hook()
    if mod is None:
        print("X hook failed to load from %s" % HOOK)
        return 1
    print("preflight-core-health -- LSP verdict reading\n")

    
    
    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    b.verdict("tsgo", ok=True, age_hours=0, symbol="WeatherStation")
    b.verdict("typescript-language-server", ok=True, age_hours=56)
    check("retired server's stale PASS is not reported",
          b.servers_reported() == [], "got %s" % b.servers_reported())

    
    
    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    b.verdict("tsgo", ok=True, age_hours=56, symbol="WeatherStation")
    rep = b.findings()
    check("rostered server's stale PASS IS reported",
          [w for w, _ in rep] == ["LSP tsgo"], "got %s" % rep)
    check("stale finding names the age",
          rep and "56 hours old" in rep[0][1], "got %s" % (rep and rep[0][1]))

    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    b.verdict("tsgo", ok=True, age_hours=1, symbol="WeatherStation")
    check("fresh PASS reports nothing", b.findings() == [],
          "got %s" % b.findings())

    
    
    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    b.verdict("tsgo", ok=True, age_hours=1, symbol="WeatherStation")
    b.verdict("some-other-lsp", ok=False, age_hours=1,
              detail="bridge never completed an MCP initialize")
    rep = b.findings()
    check("unrostered server's FAILURE is still reported",
          [w for w, _ in rep] == ["LSP some-other-lsp"], "got %s" % rep)
    check("failure finding carries the recorded detail",
          rep and "never completed an MCP initialize" in rep[0][1],
          "got %s" % (rep and rep[0][1]))

    
    
    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    b.verdict("tsgo", ok=True, age_hours=1, symbol="WeatherStation")
    b.verdict("rust-analyzer", ok=False, age_hours=120, symbol="decide",
              detail="alive but answering wrong")
    check("unconfigured server's canary FAIL is not reported",
          b.findings() == [], "got %s" % b.findings())

    
    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    b.verdict("tsgo", ok=False, age_hours=99, symbol="WeatherStation",
              detail="symbol did not resolve")
    rep = b.findings()
    check("stale FAILURE reports the failure, not the age",
          len(rep) == 1 and "did not resolve" in rep[0][1], "got %s" % rep)

    
    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    b.verdict("tsgo", ok=True, age_hours=1, symbol="WeatherStation")
    b.verdict("kotlin-lsp", ok=False, age_hours=1,
              cwd=os.path.join(b.home, "Developer/example-workspace/example-app"),
              detail="dead")
    check("a verdict recorded under a different cwd is ignored",
          b.findings() == [], "got %s" % b.findings())

    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    b.verdict("tsgo", ok=True, age_hours=1, symbol="WeatherStation")
    wt = os.path.join(b.cwd, ".claude", "worktrees", "some-branch")
    check("worktree cwd normalizes to the main checkout",
          mod.main_checkout(wt) == os.path.realpath(b.cwd),
          "got %s" % mod.main_checkout(wt))
    check("worktree session sees the main checkout's fresh PASS",
          mod.lsp_findings(mod.main_checkout(wt), time.time() - 30 * 86400) == [],
          "got %s" % mod.lsp_findings(mod.main_checkout(wt), 0))

    
    
    
    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    b.verdict("tsgo", ok=True, age_hours=99, symbol="SomeOldSymbol")
    check("verdict for a superseded symbol reports nothing at all",
          b.findings() == [], "got %s" % b.findings())

    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    rep = b.findings()
    check("rostered server with no verdict reports as absent",
          len(rep) == 1 and "NEVER" in rep[0][1], "got %s" % rep)

    
    
    b = Bed(mod)
    b.roster([{"server": "tsgo", "symbol": "WeatherStation"}])
    check("absent canary is excused on a brand-new machine",
          mod.lsp_findings(b.cwd, time.time()) == [],
          "got %s" % mod.lsp_findings(b.cwd, time.time()))

    
    
    
    
    
    print("\n  -- bridge_serving asks the daemon --")
    import hashlib
    import socket
    import threading

    live = load_module("preflight_core_health_live", HOOK)
    
    
    
    
    assert live is not None, "could not load %s" % HOOK
    from contextlib import ExitStack
    from unittest.mock import patch

    
    
    with ExitStack() as fixtures:
        home = os.path.realpath(fixtures.enter_context(tempfile.TemporaryDirectory(
            prefix="pch-", dir=os.path.join(os.path.expanduser("~"), ".cache"))))
        fixtures.enter_context(patch.object(live.hp, "state_dir", return_value=home))
        
        
        
        setattr(live, "HOME", home)
        proj = os.path.join(home, "proj")
        os.makedirs(proj)

        def health_socket(server, workspace, reply):
            run = os.path.join(home, "lsp", "run")
            os.makedirs(run, exist_ok=True)
            digest = hashlib.sha1(os.path.realpath(workspace).encode()).hexdigest()[:16]
            path = os.path.join(run, "%s-%s.sock" % (server, digest))
            check("fixture socket path fits macOS", len(os.fsencode(path)) < 104, path)
            srv = fixtures.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            srv.settimeout(0.1)
            srv.bind(path)
            srv.listen(4)

            def serve():
                while True:
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                    try:
                        rfh = conn.makefile("rb")
                        length = 0
                        while True:
                            line = rfh.readline()
                            if not line or line in (b"\r\n", b"\n"):
                                break
                            if line.lower().startswith(b"content-length:"):
                                length = int(line.split(b":", 1)[1])
                        body = json.loads(rfh.read(length) or b"{}")
                        raw = json.dumps({"jsonrpc": "2.0", "id": body.get("id"),
                                          "result": reply}).encode()
                        conn.sendall(b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
                    except (OSError, ValueError):
                        pass
                    finally:
                        conn.close()

            threading.Thread(target=serve, daemon=True).start()
            return srv, path

        check("no daemon socket: not serving", live.bridge_serving(proj, "tsgo") is False)
        srv, _ = health_socket("tsgo", proj, {"down": False, "pid": 4242, "warm": True})
        check("daemon with a live server: serving", live.bridge_serving(proj, "tsgo") is True)
        check("a worktree cwd finds the main checkout's daemon",
              live.bridge_serving(os.path.join(proj, ".claude", "worktrees", "x"), "tsgo") is True)
        check("another server's socket does not count",
              live.bridge_serving(proj, "kotlin-lsp") is False)
        srv.close()
        health_socket("basedpyright", proj, {"down": True, "pid": None})
        check("daemon holding its server DOWN: not serving",
              live.bridge_serving(proj, "basedpyright") is False)
        _, stale = health_socket("csharp-ls", proj, {"down": False, "pid": 1})
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        os.unlink(stale)
        dead.bind(stale)
        dead.close()
        check("a socket file nobody listens on: not serving",
              live.bridge_serving(proj, "csharp-ls") is False)

    
    
    
    
    import shutil
    import subprocess

    print("\n  -- self-report on crash --")
    with open(HOOK) as fh:
        src = fh.read()
    broken = src.replace("def main():\n",
                         "def main():\n    raise TypeError(\"injected\")\n", 1)
    check("test fixture actually injected a crash", broken != src)
    tmp = tempfile.mkdtemp(prefix="preflight-crash-")
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    broken_path = os.path.join(tmp, "hooks", "preflight-core-health.py")
    os.makedirs(os.path.dirname(broken_path))
    with open(broken_path, "w") as fh:
        fh.write(broken)
    scripts_copy = os.path.join(tmp, "scripts")
    os.makedirs(scripts_copy)
    shutil.copy(os.path.join(os.path.dirname(HOOK), "..", "scripts", "harness_paths.py"), scripts_copy)
    env = dict(os.environ, HOME=home)
    p = subprocess.run([sys.executable, broken_path], input="{}",
                       capture_output=True, text=True, env=env)
    
    
    check("a crashing hook still exits 0", p.returncode == 0,
          "rc=%s stderr=%s" % (p.returncode, p.stderr[:200]))
    try:
        out = json.loads(p.stdout or "{}")
    except ValueError:
        out = {}
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    check("a crashing hook reports the crash into the session's context",
          "the health hook itself crashed" in ctx, "stdout=%s" % p.stdout[:200])
    check("the crash report names the exception",
          "TypeError" in ctx and "injected" in ctx, "ctx=%s" % ctx[:200])
    check("the crash report says core systems are unchecked, not healthy",
          "unchecked" in ctx, "ctx=%s" % ctx[:200])
    check("the crash report carries the repair command",
          "home-materialize.py" in ctx, "ctx=%s" % ctx[:200])
    stamp = read_json_file(os.path.join(home, ".local", "state", "agent-context", "health",
                                        ".preflight.stamp"))
    check("a crash records ok:false with the detail",
          bool(stamp) and stamp.get("ok") is False
          and "TypeError" in (stamp.get("detail") or ""), "stamp=%s" % stamp)

    
    
    
    home2 = os.path.join(tmp, "home2")
    os.makedirs(home2)
    p = subprocess.run([sys.executable, HOOK], input=json.dumps({"cwd": home2}),
                       capture_output=True, text=True,
                       env=dict(os.environ, HOME=home2))
    stamp = read_json_file(os.path.join(home2, ".local", "state", "agent-context", "health",
                                        ".preflight.stamp"))
    check("a completed run records ok:true",
          bool(stamp) and stamp.get("ok") is True,
          "rc=%s stamp=%s stderr=%s" % (p.returncode, stamp, p.stderr[:200]))

    print("  -- the watchdog outside it --")
    watch = load_module("preflight_crash_watch",
                        os.path.join(os.path.expanduser("~"), ".agent-context",
                                     "global", "scripts", "preflight-crash-watch.py"))
    if watch is None:
        check("preflight-crash-watch.py is projected", False,
              "missing from ~/.agent-context/global/scripts")
    else:
        now = time.time()
        t = [now - 300, now - 200, now - 100]
        check("silent while the stamp is newer than every tick",
              watch.verdict(t, {"ts": now - 50, "ok": True}) is None)
        check("silent with too little history to judge",
              watch.verdict([now - 100], None) is None)
        
        
        
        v = watch.verdict(t, {"ts": now - 50, "ok": False,
                              "detail": "TypeError: boom"})
        check("reports a recorded crash, quoting it",
              v is not None and "CRASHED" in v and "TypeError: boom" in v,
              "got %s" % v)
        
        
        
        old = [now - 7200, now - 7100, now - 7000]
        v = watch.verdict(old, None)
        check("reports a hook that has NEVER completed",
              v is not None and "NEVER" in v, "got %s" % v)
        
        
        
        
        
        check("a just-installed watch stays quiet",
              watch.verdict(t, None) is None, "got %s" % watch.verdict(t, None))
        
        v = watch.verdict(t, {"ts": now - 9999, "ok": True})
        check("reports a stamp that predates every observed session start",
              v is not None and "predates" in v, "got %s" % v)
        
        
        check("one missed session alone is not yet a finding",
              watch.verdict(t, {"ts": now - 150, "ok": True}) is None)

        
        
        
        
        
        
        home3 = os.path.join(tmp, "home3")
        hstate = os.path.join(home3, ".local", "state", "agent-context", "health")
        os.makedirs(hstate)
        os.makedirs(os.path.join(home3, ".claude"), exist_ok=True)
        with open(os.path.join(home3, ".claude", "settings.json"), "w") as fh:
            json.dump({"hooks": {"SessionStart": [{"hooks": [
                {"command": "python3 ~/.agent-context/global/hooks/preflight-core-health.py"}
            ]}]}}, fh)
        WATCH = os.path.join(os.path.expanduser("~"), ".agent-context", "global", "scripts",
                             "preflight-crash-watch.py")
        
        
        
        
        for _ in range(3):
            subprocess.run([sys.executable, WATCH], capture_output=True,
                           text=True, env=dict(os.environ, HOME=home3))
        check("a run without --session-start records no tick at all",
              read_json_file(os.path.join(hstate, ".preflight.ticks")) is None,
              "ticks=%s" % (read_json_file(
                  os.path.join(hstate, ".preflight.ticks")),))
        for _ in range(3):
            subprocess.run([sys.executable, WATCH, "--session-start"],
                           capture_output=True, text=True,
                           env=dict(os.environ, HOME=home3))
        ticks = (read_json_file(os.path.join(hstate, ".preflight.ticks"))
                 or {}).get("ticks")
        check("three runs inside one session start record ONE tick",
              ticks is not None and len(ticks) == 1, "ticks=%s" % ticks)
        
        
        with open(os.path.join(hstate, ".preflight.ticks"), "w") as fh:
            json.dump({"ticks": [int(time.time()) - 4000]}, fh)
        subprocess.run([sys.executable, WATCH, "--session-start"],
                       capture_output=True, text=True,
                       env=dict(os.environ, HOME=home3))
        ticks = (read_json_file(os.path.join(hstate, ".preflight.ticks"))
                 or {}).get("ticks")
        check("a session start outside the window is a new tick",
              ticks is not None and len(ticks) == 2, "ticks=%s" % ticks)

    print("  -- health-record: one fault is one finding --")
    
    
    
    
    home4 = os.path.join(tmp, "home4")
    os.makedirs(home4)
    REC = os.path.join(os.path.expanduser("~"), ".agent-context", "global",
                       "scripts", "health-record.py")
    recfile = os.path.join(home4, ".local", "state", "agent-context", "health", "probe.json")
    for age in ("6 minutes", "10 minutes", "15 minutes"):
        subprocess.run([sys.executable, REC, "probe", "--fail",
                        "it stopped reporting %s ago.\n  run the repair" % age],
                       capture_output=True, env=dict(os.environ, HOME=home4))
    rec = read_json_file(recfile) or {}
    check("the same fault reported three times is ONE finding",
          len(rec.get("failures") or []) == 1, "failures=%s" % rec.get("failures"))
    check("and the surviving wording is the NEWEST one",
          "15 minutes" in (rec.get("failures") or [""])[0],
          "failures=%s" % rec.get("failures"))
    subprocess.run([sys.executable, REC, "probe", "--fail",
                    "a completely different thing is broken"],
                   capture_output=True, env=dict(os.environ, HOME=home4))
    rec = read_json_file(recfile) or {}
    check("a genuinely different fault is still its own finding",
          len(rec.get("failures") or []) == 2, "failures=%s" % rec.get("failures"))

    print("\n  -- a snapshot probe reports its state, not its history --")
    
    
    
    
    
    
    
    
    
    rec2file = os.path.join(home4, ".local", "state", "agent-context", "health",
                            "probe2.json")
    for detail in ("3 half-applied invariant(s) across 2 rule(s): alpha (2); beta (1)",
                   "1 half-applied invariant(s) across 1 rule(s): gamma (1)"):
        subprocess.run([sys.executable, REC, "probe2", "--fail", detail, "--replace"],
                       capture_output=True, env=dict(os.environ, HOME=home4))
    rec2 = read_json_file(rec2file) or {}
    check("--replace keeps only the newest snapshot",
          len(rec2.get("failures") or []) == 1, "failures=%s" % rec2.get("failures"))
    check("and the surviving snapshot is the newest one",
          "gamma" in (rec2.get("failures") or [""])[0], "failures=%s" % rec2.get("failures"))
    check("--replace still records the component as unwell",
          rec2.get("ok") is False, "rec=%s" % rec2)
    
    
    check("--replace preserves since",
          isinstance(rec2.get("since"), int), "since=%s" % rec2.get("since"))
    
    
    rec = read_json_file(recfile) or {}
    check("plain --fail still keeps distinct findings side by side",
          len(rec.get("failures") or []) == 2, "failures=%s" % rec.get("failures"))
    
    
    for script in ("invariant-check.py", "observation-coverage.py"):
        try:
            with open(os.path.join(os.path.expanduser("~"), ".agent-context", "global",
                                   "scripts", script)) as fh:
                src = fh.read()
        except OSError:
            src = ""
        check("%s records its snapshot with --replace" % script,
              '"--replace"' in src, "flag missing from %s" % script)

    print("\n  -- policy: a store-sync claim is checked against the present --")
    
    
    
    
    
    

    def sync_bed(info, ok=False, age=600):
        'A fake machine: one store-sync verdict, and daemon.info offsets from it.'
        root = tempfile.mkdtemp(prefix="preflight-sync-")
        setattr(mod, "HOME", os.path.join(root, "home"))
        setattr(mod, "STATE", os.path.join(mod.HOME, ".local", "state", "agent-context", "health"))
        os.makedirs(mod.STATE)
        ts = time.time() - age
        with open(os.path.join(mod.STATE, "store-sync.json"), "w") as fh:
            json.dump({"component": "agent-context sync", "ok": ok, "ts": ts,
                       "failures": ["has not synced for 20 consecutive cycles"]}, fh)
        if info is not None:
            d = mod.daemon_state_dir()
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "daemon.info"), "w") as fh:
                json.dump({k: ts + v for k, v in info.items()}, fh)
        return [w for w, _ in mod.generic_findings()]

    check("a fault the daemon has since synced past is dropped",
          sync_bed({"started_at": -3600, "last_successful_sync": 300}) == [])
    check("a verdict left by a daemon generation that has restarted is dropped",
          sync_bed({"started_at": 60, "last_successful_sync": -900}) == [])
    check("a sync fault that is still true is REPORTED",
          sync_bed({"started_at": -3600, "last_successful_sync": -60})
          == ["agent-context sync"])
    check("with no daemon.info the fault is reported, never excused",
          sync_bed(None) == ["agent-context sync"])

    
    
    def other_bed():
        root = tempfile.mkdtemp(prefix="preflight-other-")
        setattr(mod, "HOME", os.path.join(root, "home"))
        setattr(mod, "STATE", os.path.join(mod.HOME, ".local", "state", "agent-context", "health"))
        os.makedirs(mod.STATE)
        now = time.time()
        with open(os.path.join(mod.STATE, "materialize.json"), "w") as fh:
            json.dump({"component": "materialize", "ok": False, "ts": now - 600,
                       "failures": ["projection incomplete"]}, fh)
        d = mod.daemon_state_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "daemon.info"), "w") as fh:
            json.dump({"started_at": now, "last_successful_sync": now}, fh)
        return [w for w, _ in mod.generic_findings()]

    check("a healthy daemon does not excuse another component's failure",
          other_bed() == ["materialize"])

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED: %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
