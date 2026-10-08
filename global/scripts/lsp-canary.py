#!/usr/bin/env python3
'lsp-canary — prove the language server is not just alive but answering correctly.\n\nWhy a canary and not a liveness check. A dead LSP is the easy case: the process is\ngone, `claude mcp list` says so, and core-health-probe.py catches it. The dangerous\ncase is the server that launches, connects, registers all six tools, and then answers\n`AgentEntry not found` for a type that is sitting in Kit/Models. Nothing anywhere\nreports an error. The agent reads "not found" as fact, concludes the symbol does not\nexist, and acts on it. That is a wrong answer delivered with full confidence, which\nis worse than no answer, and no liveness check will ever see it.\n\nTwo causes:\n\n  1. Cold index. Right after initialize, `definition` for a symbol that exists\n     returns "not found"; seconds later the same call returns the correct answer.\n     sourcekit-lsp indexes asynchronously and answers questions in the meantime,\n     negatively. The first symbol question of a session is therefore the one most\n     likely to be answered wrong. Running this canary in the background at\n     SessionStart is both the check and the warm-up: by the time an agent asks\n     anything, the index is up.\n\n  2. Stale BSP binding. buildServer.json pins an absolute DerivedData build_root, and\n     this repo regenerates its .xcodeproj on every scripts/generate.sh, re-deriving\n     that hash. lspd.py --mcp already self-heals a build_root that no longer exists; it\n     cannot detect one that exists and is stale. The canary can, because it asks a\n     question only a correctly-bound server can answer.\n\nSo not-found is only a finding after the budget. Retrying with backoff is what\nseparates cause 1 (transient, self-resolving, and warmed by this run) from cause\n2 (permanent, and worth waking someone over). A canary that reported the t=0 answer\nwould cry wolf on every single session.\n\nRuns detached from the SessionStart hook -- it costs tens of seconds by design and\nmust never be in the session\'s critical path. Verdict lands in\n~/.local/state/agent-context/health/lsp/<encoded-cwd>-<server>.json for preflight-core-health.py.\n\nUsage:  lsp-canary.py [<cwd>]      (default: the current directory; -h/--help prints this)'
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.path.expanduser("~")
STORE = os.path.join(HOME, ".agent-context", "global")
CONFIG = os.path.join(STORE, "lsp-canaries.json")
LSPD = os.path.join(STORE, "scripts", "lspd.py")
STATE = os.path.join(hp.state_dir(HOME), "health", "lsp")

BUDGET = 90.0        
BACKOFF = [0, 5, 10, 15, 20, 30]


def canaries_for(cwd):
    'The canary symbols configured for the project containing cwd.'
    try:
        with open(CONFIG) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return []
    cwd = os.path.realpath(cwd)
    best, out = None, []
    for rel, entries in (cfg.get("canaries") or {}).items():
        root = os.path.realpath(os.path.join(HOME, rel))
        if (cwd == root or cwd.startswith(root + os.sep)) and (
                best is None or len(root) > len(best)):
            best, out = root, [dict(e, workspace=root) for e in entries]
    return out


class Bridge:
    'Minimal MCP stdio client -- just enough to call one tool.'

    def __init__(self, server, workspace):
        
        
        
        
        
        
        
        
        env = dict(os.environ, AGENT_LSP_WORKSPACE=workspace)
        self.stderr = []
        self.proc = subprocess.Popen(
            [sys.executable, LSPD, "--mcp", "--key", server, "--workspace", workspace],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=workspace, env=env,
        )
        threading.Thread(target=self._drain, daemon=True).start()
        self._id = 0

    def _drain(self):
        
        
        
        stderr = self.proc.stderr
        if stderr is None:
            return
        for line in stderr:
            self.stderr.append(line.rstrip())
            del self.stderr[:-40]

    def _send(self, obj):
        stdin = self.proc.stdin
        if stdin is None:
            raise RuntimeError("bridge stdin not available")
        stdin.write(json.dumps(obj) + "\n")
        stdin.flush()

    def _recv(self, timeout):
        "Read one JSON reply, honoring `timeout` for real.\n\n        The deadline must gate the read, not only book-end it. readline() on a pipe\n        blocks until a line arrives or the pipe closes, and a wedged bridge does\n        neither: it is still running, so there is no EOF, and it has nothing to say, so\n        there is no line. Checking `time.time()` only between reads would let the canary\n        hang on its first question, orphaned and still holding its probe bridge. That\n        bridge holds the workspace's index lock, so the harness's own bridge loses the\n        race and every later call returns `broken pipe` until a human reconnects the\n        MCP server.\n\n        select() on the fd with the remaining budget is what makes the timeout real:\n        we only read once bytes are known to be waiting, so no read can outlive the\n        deadline."
        deadline = time.time() + timeout
        fd = self.proc.stdout
        if fd is None:
            return None
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                return None          
            if not ready:
                return None          
            line = fd.readline()
            if not line:
                return None          
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue

    def handshake(self):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "lsp-canary", "version": "1"}}})
        if self._recv(60) is None:
            return False
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized",
                    "params": {}})
        return True

    def definition(self, symbol, timeout=60):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                    "params": {"name": "definition",
                               "arguments": {"symbolName": symbol}}})
        reply = self._recv(timeout)
        if reply is None:
            return None
        content = (reply.get("result") or {}).get("content") or []
        return "".join(c.get("text", "") for c in content)

    def references(self, symbol, timeout=90):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                    "params": {"name": "references",
                               "arguments": {"symbolName": symbol}}})
        reply = self._recv(timeout)
        if reply is None:
            return None
        content = (reply.get("result") or {}).get("content") or []
        return "".join(c.get("text", "") for c in content)

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                self.proc.kill()
            except OSError:
                pass


REF_COUNT_RE = re.compile(r"References in File:\s*(\d+)")


def breadth_failure(bridge, spec, symbol):
    '(detail-when-narrow-or-None, files-seen-or-None) for the reach check.\n\n    The count is returned even when the check passes, so the verdict can record\n    what was measured. A check whose success leaves no trace is\n    indistinguishable from a check that never ran. With the count on disk, "the index saw 3 files" and\n    "nobody asked" are different states you can tell apart afterwards.\n\n      After a `git merge --ff-only`, a server can answer `references` with a\n      fraction of the call sites, with no error and no partial-result signal, while\n      `definition` for the same symbol stays correct. A canary asking only for a\n      definition is green through the whole outage.\n\n    So a canary with `min_reference_files` set asserts reach: the symbol is found from\n    at least that many files. Deliberately files and not raw hits -- hit counts move\n    with ordinary editing, whereas a symbol used by three modules stops being used by\n    three modules only through a real refactor, which is a config change somebody makes\n    deliberately. Absent from a spec, this check does not run, so no project pays for a\n    threshold nobody has measured.'
    want_files = spec.get("min_reference_files")
    if not want_files:
        return None, None
    text = bridge.references(symbol)
    if text is None:
        return ("the index resolved '%s' but never answered a references query for it. "
                "That is the shape of a half-built index: definitions come from the "
                "file it parsed, references need the whole workspace." % symbol), None
    counts = [int(n) for n in REF_COUNT_RE.findall(text)]
    if len(counts) >= want_files:
        return None, len(counts)
    return ("Alive but answering narrowly: '%s' resolves, but references were found in "
            "only %d file(s) where this project expects at least %d. A partial index "
            "does not error -- it returns a short list, which is indistinguishable from "
            "a symbol that genuinely has few callers. Do not trust 'who calls this' in "
            "this session until it is repaired: `lspd.py --resync` forces the server to "
            "re-read the workspace. If the symbol was genuinely refactored, update "
            "min_reference_files in lsp-canaries.json."
            % (symbol, len(counts), want_files)), len(counts)


def write(cwd, server, ok, detail, extra=None, symbol=None):
    'Record the question alongside the answer.\n\n    A verdict is only evidence about the symbol it asked for. After the canary symbol\n    in lsp-canaries.json changes, a failing verdict for the old symbol is still on\n    disk. Stamping `symbol` lets preflight discard a verdict for a question nobody\n    asks any more; without it preflight would believe it for the whole staleness\n    window.'
    os.makedirs(STATE, exist_ok=True)
    rec = {"ts": int(time.time()), "cwd": cwd, "server": server,
           "ok": ok, "detail": detail, "symbol": symbol}
    if extra:
        rec.update(extra)
    path = os.path.join(STATE, "%s-%s.json" % (cwd.replace("/", "-"), server))
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh, indent=2)
    os.replace(tmp, path)


def run_one(cwd, spec):
    server, symbol = spec["server"], spec["symbol"]
    workspace = spec["workspace"]

    try:
        bridge = Bridge(server, workspace)
    except OSError as exc:
        write(cwd, server, False, "could not launch the bridge: %s" % exc,
              symbol=symbol)
        return

    try:
        if not bridge.handshake():
            tail = "; ".join(bridge.stderr[-3:]) or "no output"
            
            
            
            
            
            
            
            
            
            if any("index lock" in s or "yielding" in s for s in bridge.stderr):
                return
            write(cwd, server, False,
                  "the bridge never completed an MCP initialize (%s). Code "
                  "intelligence is absent from this session; every symbol question "
                  "will silently fall back to grep." % tail, symbol=symbol)
            return

        started = time.time()
        answers = []
        for wait in BACKOFF:
            if time.time() - started > BUDGET:
                break
            if wait:
                time.sleep(wait)
            text = bridge.definition(symbol)
            if text is None:
                answers.append("no reply")
                continue
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            if text.startswith("no compiler flags:"):
                write(cwd, server, False, text.strip(), symbol=symbol)
                return
            if text.startswith("failed to ") or "broken pipe" in text:
                answers.append("bridge up, language server dead: %s" % text.strip()[:160])
                continue
            if text.strip() == "%s not found" % symbol or "not found" in text[:80]:
                answers.append("not found")
                continue
            if symbol in text:
                elapsed = time.time() - started
                bad, ref_files = breadth_failure(bridge, spec, symbol)
                extra = {"warmup_seconds": round(elapsed, 1),
                         "cold_misses": answers.count("not found")}
                
                
                
                
                if ref_files is not None:
                    extra["reference_files"] = ref_files
                if bad:
                    write(cwd, server, False, bad, extra, symbol=symbol)
                    return
                write(cwd, server, True,
                      "resolved %s in %.0fs%s"
                      % (symbol, elapsed,
                         "" if ref_files is None
                         else ", references in %d file(s)" % ref_files),
                      extra, symbol=symbol)
                return
            answers.append("unrecognized reply")

        tail = "; ".join(s for s in bridge.stderr[-3:] if "[INFO]" not in s)

        
        
        
        
        
        
        if answers and answers[-1].startswith("bridge up"):
            write(cwd, server, False,
                  "Bridge up, language server dead: the MCP bridge connects and "
                  "registers its tools, but every '%s' call fails inside it (%s). "
                  "`claude mcp list` will report this server as Connected and the "
                  "agent's tool roster will look complete -- each call just returns "
                  "an error string that reads like prose. Check that '%s' runs at all "
                  "in this workspace.%s"
                  % (server, answers[-1].split(": ", 2)[-1], server,
                     " Bridge said: " + tail if tail else ""), symbol=symbol)
            return

        
        
        
        hints = {
            "sourcekit-lsp": "Usual cause: buildServer.json is bound to a stale "
                             "DerivedData root (rebind: xcode-build-server config "
                             "-project ./*.xcodeproj -scheme <scheme>, then a clean "
                             "build).",
            "csharp-ls": "Usual cause: the solution has not been restored or built on "
                         "this machine (dotnet restore, then dotnet build).",
            "kotlin-lsp": "Usual cause: the Gradle import failed or never finished -- "
                          "check that ./gradlew tasks succeeds in this workspace.",
            "typescript-language-server": "Usual cause: tsserver never picked up "
                                          "tsconfig.json, or node_modules is absent "
                                          "(npm install).",
        }
        write(cwd, server, False,
              "Alive but answering wrong: after %.0fs of retries it still cannot "
              "resolve '%s', a symbol that exists in this workspace (replies: %s). "
              "The server is connected, so nothing else will report a problem -- but "
              "it will answer 'not found' for symbols that exist, and that answer "
              "must not be trusted. %s%s"
              % (BUDGET, symbol, ", ".join(dict.fromkeys(answers)) or "none",
                 hints.get(server, ""),
                 " Bridge said: " + tail if tail else ""), symbol=symbol)
    finally:
        bridge.close()


def _arm_deadman():
    "Hard ceiling on this process's whole life.\n\n    _recv honors its own timeout. This exists because that covers one blocking call,\n    and the process has others -- `_send` can block when a pipe buffer fills, `proc.wait`\n    can hang on a child that ignores SIGTERM, and any future edit can introduce a\n    third. The cost of being wrong is not a slow probe: it is a canary orphaned to\n    ppid=1 for hours, holding the index lock that the session's real\n    bridge needs, with no symptom anywhere except code intelligence that answers\n    `broken pipe` and a user who has to reconnect the MCP server by hand.\n\n    SIGALRM fires even inside a blocking C-level read, which is exactly why it is\n    the right instrument here. The handler raises, so `finally: bridge.close()`\n    still runs and the probe bridge is reaped on the way out. The ceiling is well\n    clear of BUDGET so a legitimately slow cold index is never cut short."
    ceiling = int(BUDGET * 3) + 60

    def _expire(_sig, _frm):
        raise TimeoutError(
            "lsp-canary exceeded its %ds ceiling and is aborting so it cannot "
            "orphan its probe bridge" % ceiling)

    try:
        signal.signal(signal.SIGALRM, _expire)
        signal.alarm(ceiling)
    except (AttributeError, ValueError, OSError):
        pass                     


def main():
    
    
    
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    flags = [a for a in args if a.startswith("-")]
    if flags:
        sys.stderr.write("lsp-canary: unknown flag %s (usage: lsp-canary.py [<cwd>])\n"
                         % flags[0])
        return 2
    if len(args) > 1:
        sys.stderr.write("lsp-canary: expected at most one directory, got %d "
                         "(usage: lsp-canary.py [<cwd>])\n" % len(args))
        return 2
    _arm_deadman()
    cwd = os.path.realpath(args[0] if args else os.getcwd())
    specs = canaries_for(cwd)
    if not specs:
        return 0
    for spec in specs:
        try:
            run_one(cwd, spec)
        except Exception as exc:  
            write(cwd, spec.get("server", "?"), False,
                  "the canary itself failed: %r" % exc,
                  symbol=spec.get("symbol"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
