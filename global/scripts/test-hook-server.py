#!/usr/bin/env python3
"Battery for the warm hook path (policy): hook-client-build.py, hook-client.rs and\nhook-server.py, and home-settings-sync's wiring of them.\n\n  [1] hook-client-build compiles the client into ~/.local/share/agent-context/bin and\n      installed() names it only while its recorded source digest matches.\n  [2] the first call finds no server, runs cold and starts one; later calls are served\n      by a forked child of the server (the guard's parent is not the client).\n  [3] served output equals the cold dispatcher's: context, plain stdout, a refusal with\n      exit 2 and its stderr, a failing guard's fail-open message.\n  [4] each call runs in the client's working directory and environment, and a guard's\n      change to either does not reach the next call.\n  [5] a request naming another dispatcher or Python gets a retry and runs cold, correctly.\n  [6] a changed dispatcher makes the server answer one retry and re-execute; the next\n      call is served by the new process.\n  [7] a dead server leaves a socket file behind: the call still runs cold and correctly.\n  [8] the server exits after HOOK_SERVER_IDLE seconds and saves what it learned: the\n      regular expression a guard compiled is in hook-server-warm.json.\n  [9] home-settings-sync wires a dispatched event through the client only when a current\n      build exists and the target is the home's own settings.json, and reads the client\n      command back as hook-dispatch.py.\n\nSkips everything (exit 0, says so) on a host with no rustc.\nUsage: test-hook-server.py"
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


COPIED = ("hook-dispatch.py", "hook-server.py", "harness_paths.py", "health-record.py",
          "codex-hook-adapter.py")
RESULTS = []


def check(name, ok, detail: object = ""):
    RESULTS.append(ok)
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GUARDS = {
    
    "probe.py": (
        "import json, os, re, sys\n"
        "sys.stdin.read()\n"
        "re.compile(r'probe-warm-[0-9]+')\n"
        "ctx = {'ppid': os.getppid(), 'cwd': os.getcwd(), 'probe': os.environ.get('PROBE'),\n"
        "       'leak': os.environ.get('LEAK')}\n"
        "os.environ['LEAK'] = 'yes'\n"
        "os.chdir('/')\n"
        "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',\n"
        "      'additionalContext': json.dumps(ctx)}}))\n"),
    "plain.py": "import sys\nsys.stdin.read()\nprint('plain words from a guard')\n",
    "refuse.py": ("import json, sys\n"
                  "data = json.load(sys.stdin)\n"
                  "if 'refuse-me' in json.dumps(data):\n"
                  "    sys.stderr.write('refused by the fixture\\n')\n"
                  "    sys.exit(2)\n"),
    "crash.py": "raise RuntimeError('fixture crash')\n",
}


class Fixture:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="hook-server-test-")
        self.scripts = os.path.join(self.root, "scripts")
        os.makedirs(self.scripts)
        for name in COPIED:
            shutil.copy(os.path.join(HERE, name), self.scripts)
        self.dispatch = os.path.join(self.scripts, "hook-dispatch.py")
        self.server = os.path.join(self.scripts, "hook-server.py")
        self.home = os.path.join(self.root, "home")
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.home)
        os.makedirs(self.work)
        guards = os.path.join(self.root, "guards")
        os.makedirs(guards)
        for name, body in GUARDS.items():
            with open(os.path.join(guards, name), "w") as fh:
                fh.write(body)
        hooks = [{"type": "command", "command": "%s %s" % (PY, os.path.join(guards, n))}
                 for n in ("probe.py", "plain.py", "refuse.py")]
        self.registry = os.path.join(self.root, "registry.json")
        with open(self.registry, "w") as fh:
            json.dump({"hooks": {
                "PreToolUse": [{"matcher": "*", "hooks": hooks}],
                "Stop": [{"hooks": [{"type": "command", "command": "%s %s" % (
                    PY, os.path.join(guards, "crash.py"))}]}],
            }}, fh)
        
        self.sock = os.path.join(tempfile.mkdtemp(prefix="hs-", dir=os.path.expanduser(
            "~/.cache")), "s")
        self.client = None

    def env(self, **extra):
        env = dict(os.environ, HOME=self.home, HOOK_DISPATCH_REGISTRY=self.registry,
                   HOOK_SERVER_SOCK=self.sock, PROBE="from-client")
        env.pop("TMUX_PANE", None)
        env.update(extra)
        return env

    def call(self, event="PreToolUse", payload=None, via_client=True, dispatcher=None,
             **extra):
        dispatcher = dispatcher or self.dispatch
        payload = payload or {"tool_name": "Bash", "tool_input": {"command": "ls"},
                              "hook_event_name": event, "cwd": self.work}
        argv = ([self.client, PY, dispatcher, event] if via_client
                else [PY, dispatcher, event])
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=self.work, env=self.env(**extra))
        out, err = proc.communicate(json.dumps(payload).encode(), timeout=60)
        return proc.pid, proc.returncode, out.decode(), err.decode()

    def call_codex(self, command, payload, via_client=True):
        'A Codex hook as harness-materialize renders it: the adapter, then CMD.'
        adapter = os.path.join(self.scripts, "codex-hook-adapter.py")
        argv = [PY, adapter, "--event", "PreToolUse", "--", command]
        proc = subprocess.Popen([self.client] + argv if via_client else argv,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=self.work, env=self.env())
        out, err = proc.communicate(json.dumps(payload).encode(), timeout=60)
        return proc.pid, proc.returncode, out.decode(), err.decode()

    def context(self, out):
        doc = json.loads(out)
        text = doc["hookSpecificOutput"]["additionalContext"]
        return json.loads(text.split("\n\n")[0])

    def wait_socket(self, present=True, limit=10.0):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if os.path.exists(self.sock) == present:
                return True
            time.sleep(0.05)
        return False

    def server_pid(self):
        
        if os.path.isdir("/proc/self"):
            found = []
            for name in filter(str.isdigit, os.listdir("/proc")):
                try:
                    with open("/proc/%s/cmdline" % name, "rb") as fh:
                        cmdline = fh.read()
                except OSError:
                    continue
                if self.server.encode() in cmdline:
                    found.append(int(name))
            return sorted(found)
        proc = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
        return sorted(int(line.split(None, 1)[0]) for line in proc.stdout.splitlines()
                      if self.server in line)

    def stop(self):
        for pid in self.server_pid():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        shutil.rmtree(os.path.dirname(self.sock), ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)


def main():
    build = load("hook_client_build", "hook-client-build.py")
    if not build.find_rustc():
        print("test-hook-server: no rustc on this host; skipped")
        return 0
    fx = Fixture()
    try:
        return run(fx, build)
    finally:
        fx.stop()


def run(fx, build):
    print("[1] build")
    check("installed() is None before a build", build.installed(fx.home) is None)
    code, message = build.build(fx.home)
    check("build succeeds", code == 0, message)
    fx.client = build.installed(fx.home)
    check("installed() names the binary", fx.client == build.binary_path(fx.home), fx.client)
    with open(fx.client + ".source", "w") as fh:
        fh.write("0" * 64 + "\n")
    check("a stale digest is not installed", build.installed(fx.home) is None)
    build.build(fx.home)
    check("a rebuild restores it", build.installed(fx.home) == fx.client)

    print("[2] cold first, served after")
    pid, code, out, err = fx.call()
    check("first call runs cold (guard's parent is the client)",
          code == 0 and fx.context(out)["ppid"] == pid, (code, out, err))
    check("it starts a server", fx.wait_socket())
    time.sleep(0.2)
    pid, code, out, err = fx.call()
    check("second call is served (guard's parent is not the client)",
          code == 0 and fx.context(out)["ppid"] != pid, (code, out, err))

    print("[3] parity with the cold dispatcher")
    for label, event, payload in (
            ("context and plain stdout", "PreToolUse", None),
            ("a refusal", "PreToolUse", {"tool_name": "Bash", "hook_event_name": "PreToolUse",
                                         "tool_input": {"command": "refuse-me"}}),
            ("a crashing guard fails open", "Stop", {"hook_event_name": "Stop"})):
        _, c1, o1, e1 = fx.call(event, payload)
        _, c2, o2, e2 = fx.call(event, payload, via_client=False)
        
        o1, o2 = (re.sub(r'ppid\\": \d+', "ppid", o) for o in (o1, o2))
        check("%s: same exit, stdout and stderr" % label,
              (c1, o1, e1) == (c2, o2, e2), ((c1, o1, e1), (c2, o2, e2)))
    _, code, _, err = fx.call(payload={"tool_name": "Bash", "hook_event_name": "PreToolUse",
                                       "tool_input": {"command": "refuse-me"}})
    check("the refusal is exit 2 naming the guard",
          code == 2 and "[refuse.py] refused by the fixture" in err, (code, err))

    print("[4] working directory and environment")
    _, _, out, _ = fx.call(PROBE="second-value")
    ctx = fx.context(out)
    check("the client's cwd and environment reach the guard",
          ctx["cwd"] == os.path.realpath(fx.work) and ctx["probe"] == "second-value", ctx)
    check("a guard's environment change does not reach the next call", ctx["leak"] is None, ctx)

    print("[4b] the Codex adapter")
    codex = {"tool_name": "exec_command", "hook_event_name": "PreToolUse",
             "tool_input": {"command": "ls"}, "cwd": fx.work}
    inner = "%s %s PreToolUse" % (PY, fx.dispatch)
    pid, code, out, err = fx.call_codex(inner, codex)
    check("an adapter call is served, with hook-dispatch in the same child",
          code == 0 and fx.context(out)["ppid"] != pid, (code, out, err))
    _, c1, o1, e1 = fx.call_codex(inner, codex)
    _, c2, o2, e2 = fx.call_codex(inner, codex, via_client=False)
    o1, o2 = (re.sub(r'ppid\\": \d+', "ppid", o) for o in (o1, o2))
    check("served and cold adapter calls agree", (c1, o1, e1) == (c2, o2, e2),
          ((c1, o1, e1), (c2, o2, e2)))
    refuse = dict(codex, tool_input={"command": "refuse-me"})
    _, code, _, err = fx.call_codex(inner, refuse)
    check("a refusal through the adapter is exit 2",
          code == 2 and "refused by the fixture" in err, (code, err))
    _, code, out, _ = fx.call_codex("echo shell-path; cat >/dev/null", codex)
    check("a command that is not hook-dispatch runs in a shell",
          code == 0 and out.strip() == "shell-path", (code, out))

    print("[5] another dispatcher or Python")
    other = os.path.join(fx.root, "hook-dispatch.py")
    for name in COPIED:
        shutil.copy(os.path.join(HERE, name), fx.root)
    pid, code, out, err = fx.call(dispatcher=other)
    check("another dispatcher's call runs cold and correctly",
          code == 0 and fx.context(out)["ppid"] == pid, (code, out, err))

    print("[6] reload on a changed dispatcher")
    before = fx.server_pid()
    with open(fx.dispatch, "a") as fh:
        fh.write("\n# changed by test-hook-server\n")
    pid, code, out, _ = fx.call()
    check("the call after the change runs cold", code == 0 and fx.context(out)["ppid"] == pid)
    time.sleep(0.5)
    pid, code, out, _ = fx.call()
    check("the re-executed server serves the next call",
          code == 0 and fx.context(out)["ppid"] != pid, out)
    check("it is the same process, re-executed", fx.server_pid() == before,
          (before, fx.server_pid()))

    print("[7] a dead server")
    for p in fx.server_pid():
        os.kill(p, signal.SIGKILL)
    time.sleep(0.2)
    check("the socket file is left behind", os.path.exists(fx.sock))
    pid, code, out, _ = fx.call(HOOK_SERVER_SPAWN="0")
    check("the call runs cold and correctly", code == 0 and fx.context(out)["ppid"] == pid, out)

    print("[8] idle exit and warming")
    try:
        os.unlink(fx.sock)
    except OSError:
        pass
    stamp = os.path.join(fx.home, ".cache", "agent-context", "hook-server.spawn")
    try:
        os.unlink(stamp)
    except OSError:
        pass
    fx.call(HOOK_SERVER_IDLE="1.5")
    check("a server starts", fx.wait_socket())
    time.sleep(0.2)
    fx.call(HOOK_SERVER_IDLE="1.5")
    check("it exits after the idle limit", fx.wait_socket(present=False, limit=8))
    warm = os.path.join(fx.home, ".cache", "agent-context", "hook-server-warm.json")
    try:
        with open(warm) as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        doc = {"error": str(exc)}
    check("the guard's regular expression was learned",
          any(item[0] == "probe-warm-[0-9]+" for item in doc.get("regex", [])), doc)

    print("[9] settings wiring")
    sync = os.path.join(HERE, "home-settings-sync.py")
    env = dict(os.environ, HOME=fx.home,
               HOOK_DISPATCH_REGISTRY=os.path.join(fx.root, "synced-registry.json"))
    probe = ("import importlib.util, json, sys\n"
             "spec = importlib.util.spec_from_file_location('s', %r)\n"
             "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
             "cmd = m.dispatch_command('PreToolUse', m.HOOK_CLIENT.installed())\n"
             "print(json.dumps([cmd, m.command_basename(cmd)]))\n" % sync)
    cmd, base = json.loads(subprocess.run([PY, "-c", probe], env=env, capture_output=True,
                                          text=True, check=True).stdout)
    check("a current build goes in front of the dispatcher",
          cmd.startswith(fx.client + " ") and cmd.endswith(" PreToolUse"), cmd)
    check("the command reads back as hook-dispatch.py", base == "hook-dispatch.py", base)

    def synced_commands(target=None):
        subprocess.run([PY, sync] + ([target] if target else []), env=env,
                       capture_output=True, text=True, timeout=120)
        path = target or os.path.join(fx.home, ".claude", "settings.json")
        with open(path) as fh:
            hooks = json.load(fh).get("hooks") or {}
        return [h.get("command", "") for groups in hooks.values() for g in groups
                for h in g.get("hooks") or [] if "hook-dispatch.py" in h.get("command", "")]

    for path in (os.path.join(fx.home, ".claude", "settings.json"),
                 os.path.join(fx.root, "other-settings.json")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{}\n")
    live = synced_commands()
    check("the home's own settings.json is wired through the client",
          live and all(c.startswith(fx.client + " ") for c in live), live)
    other = synced_commands(os.path.join(fx.root, "other-settings.json"))
    check("a settings file named on the command line gets the plain command",
          other and not any("hook-client" in c for c in other), other)
    os.unlink(fx.client)
    live = synced_commands()
    check("without a build the dispatcher is wired alone",
          live and not any("hook-client" in c for c in live), live)

    passed = sum(RESULTS)
    print("\n%d passed, %d failed" % (passed, len(RESULTS) - passed))
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
