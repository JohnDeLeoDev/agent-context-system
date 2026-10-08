#!/usr/bin/env python3
"Battery for consolidation Phase 3a: the hook dispatcher core.\n\n  [1] hook-dispatch.py <Event> reads the payload once and runs the guards whose matcher\n      fits, in registry order. Matching is Claude Code's documented rule: a matcher of\n      plain names is an exact list split on | or , and anything else an unanchored\n      regex; SessionStart matches source, SubagentStop agent_type; Stop and\n      UserPromptSubmit ignore matchers; an identical command runs once per call. The\n      registry is ~/.agent-context/hook-dispatch.json, or $HOOK_DISPATCH_REGISTRY.\n  [2] Python guards run inside the dispatcher's process with their own stdin, stdout,\n      stderr and exit code, and leave no environment, cwd, sys.path or sys.argv behind;\n      output from a child they spawn is captured with theirs. Shell guards run as\n      subprocesses.\n  [3] Every hook-test-cases case passes through the dispatcher:\n      hook-test-run.py --via-dispatch runs each case's guard alone (--guard <path>).\n  [4] Merge: any refusal (exit 2, permissionDecision deny, decision block) makes the\n      dispatcher exit 2 with every reason on stderr, in order, each prefixed with\n      [<guard file>]. additionalContext and systemMessage are joined in order and still\n      printed as JSON. Without a refusal: permissionDecision by precedence\n      (defer > ask > allow), updatedInput from the first guard that sets it, exit 0.\n      Plain stdout is context on SessionStart and UserPromptSubmit.\n  [5] Fail open: a guard that raises, exits other than 0 or 2, or times out is skipped,\n      named in a systemMessage and recorded through health-record as\n      hook-dispatch-<guard>; its next clean run clears that record. A missing or corrupt\n      registry, or an event absent from it, fails open the same way (hook-dispatch).\n  [6] home-settings-sync.py <target> --dispatch <events> registers one dispatcher command\n      per dispatched event, with no matcher and a timeout covering its guards, and writes\n      the guards to hook-dispatch.json beside the target. Async hooks and\n      home-materialize stay direct. A second sync is a no-op; undispatching restores the\n      per-guard entries.\n  [7] Readers of the hook wiring answer the same for dispatched events: the registration\n      probe, hook-interaction-report, hook-firing-report (which attributes a [guard]\n      refusal to that guard), invariant-check's wiring checks, preflight-crash-watch.\n      Invariant hook-readers-expand-dispatch reports a reader that skips the registry.\n  [8] hook-test-run's setup commands commit without signing, so a host whose global git\n      config signs with a key it lacks (pc's WSL) still runs every case.\n\nNot asserted here: hook overhead (plan criterion 8), measured with hook-latency-probe.py.\n\nEvery dispatcher run gets HOME inside a temp dir, so health records never reach the real\n~/.local/state/agent-context. Interpreter names in fixture text are built by concatenation, so\ninvariant interpreter-is-rendered does not report this file.\n\nUsage: test-hook-dispatch.py"

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.dirname(os.path.dirname(HERE))
DISPATCH = os.path.join(HERE, "hook-dispatch.py")
SYNC = os.path.join(HERE, "home-settings-sync.py")
RUNNER = os.path.join(HERE, "hook-test-run.py")
CHECK = os.path.join(HERE, "invariant-check.py")
REAL_HOME = os.path.expanduser("~")
INVARIANT = "hook-readers-expand-dispatch"
LOCAL = "/usr/local/libexec/machine-local-guard.sh"
ALL_EVENTS = ("SessionStart,UserPromptSubmit,PreToolUse,PostToolUse,PostToolUseFailure,"
              "Stop,Notification,SubagentStop,SessionEnd,PreCompact")
PY = "pyth" + "on"
INTERP_WORD = re.compile(r"^(ba)?sh$|^" + PY + r"(\d+(\.\d+)?)?$")

TMP = tempfile.mkdtemp(prefix="hook-dispatch-test-")

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def put(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)
    return path


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def parse(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def clip(text, size=300):
    text = text or ""
    return text if len(text) <= size else "..." + text[-size:]


def in_order(text, *parts):
    'Every part appears in text, each after the one before it.'
    text = text or ""
    at = -1
    for part in parts:
        found = text.find(part, at + 1)
        if found < 0:
            return False
        at = found
    return True


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def script_of(command):
    'The script a hook command runs, past any interpreter word.'
    words = (command or "").split()
    while len(words) > 1 and INTERP_WORD.match(os.path.basename(words[0])):
        words = words[1:]
    return words[0] if words else ""


def names_in(groups):
    return [os.path.basename(script_of(h.get("command", "")))
            for g in groups or [] for h in g.get("hooks") or []]


class Result:
    'One dispatcher run: exit code, both streams, the stdout JSON, wall time, pid.'

    def __init__(self, rc, out, err, secs, pid):
        self.rc, self.out, self.err, self.secs, self.pid = rc, out or "", err or "", secs, pid
        self.doc = None
        self.json_ok = True
        if self.out.strip():
            self.doc = parse(self.out)
            self.json_ok = isinstance(self.doc, dict)

    def field(self, key):
        return self.doc.get(key) if isinstance(self.doc, dict) else None

    def hso(self, key):
        out = self.field("hookSpecificOutput")
        return out.get(key) if isinstance(out, dict) else None

    def __str__(self):
        return "rc=%s stdout=%r stderr=%r" % (self.rc, clip(self.out, 240), clip(self.err, 240))


class Scene:
    'One registry, its guards and their run log, in a fresh directory.'

    def __init__(self, name):
        self.dir = os.path.join(TMP, name)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.log = os.path.join(self.dir, "ran.log")
        self.registry = os.path.join(self.dir, "hook-dispatch.json")

    def path(self, name):
        return os.path.join(self.dir, name)

    def py(self, name, body="", timeout=None):
        'A Python guard, wired the way home-settings-sync wires one.'
        path = self.path(name)
        put(path, "import json, os, sys\nwith open(%r, 'a') as _log:\n    _log.write(%r + '\\n')\n%s"
            % (self.log, name, body))
        return self._entry(sys.executable + " " + path, timeout)

    def sh(self, name, body="", timeout=None, bare=False):
        'A shell guard, as `bash <path>` or as the bare executable path.'
        path = self.path(name)
        put(path, "#!/bin/bash\necho %s >> '%s'\n%s" % (name, self.log, body), 0o755)
        return self._entry(path if bare else "bash " + path, timeout)

    @staticmethod
    def _entry(command, timeout):
        entry = {"type": "command", "command": command}
        if timeout is not None:
            entry["timeout"] = timeout
        return entry

    def write(self, hooks, path=None):
        put(path or self.registry, json.dumps({"hooks": hooks}))

    def ran(self):
        return [line for line in read(self.log).splitlines() if line]

    def health(self, component):
        return os.path.join(self.home, ".local", "state", "agent-context", "health", component + ".json")

    def run(self, event, payload, args=(), registry=True, timeout=60):
        env = dict(os.environ, HOME=self.home)
        env.pop("HOOK_DISPATCH_REGISTRY", None)
        if registry:
            env["HOOK_DISPATCH_REGISTRY"] = self.registry
        remove(self.log)
        started = time.time()
        proc = subprocess.Popen([sys.executable, DISPATCH, event] + list(args),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=self.dir, env=env)
        try:
            out, err = proc.communicate(json.dumps(payload), timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return Result(None, out, err, time.time() - started, proc.pid)
        return Result(proc.returncode, out, err, time.time() - started, proc.pid)


def tool_call(event, tool, **extra):
    payload = {"session_id": "hook-dispatch-test", "transcript_path": "", "cwd": TMP,
               "hook_event_name": event, "tool_name": tool,
               "tool_input": {"command": "true"}}
    payload.update(extra)
    return payload


def lifecycle(event, **extra):
    payload = {"session_id": "hook-dispatch-test", "transcript_path": "", "cwd": TMP,
               "hook_event_name": event}
    payload.update(extra)
    return payload


def refuse_sh(scene, name, reason, **kw):
    return scene.sh(name, "echo '%s' >&2\nexit 2\n" % reason, **kw)


def say_py(scene, name, obj, **kw):
    return scene.py(name, "print(json.dumps(%r))\n" % (obj,), **kw)


def say_sh(scene, name, obj, **kw):
    return scene.sh(name, "printf '%%s\\n' '%s'\n" % json.dumps(obj), **kw)


def pre_output(**fields):
    out = {"hookEventName": "PreToolUse"}
    out.update(fields)
    return {"hookSpecificOutput": out}




def check_selection():
    print("[1] selection, order and the registry")
    s = Scene("select")
    e = {n: (s.py(n) if n.endswith(".py") else s.sh(n))
         for n in ("a.py", "b.sh", "c.py", "d.py", "e.sh", "f.py", "g.py", "h.sh")}
    s.write({"PreToolUse": [
        {"matcher": "Bash", "hooks": [e["a.py"], e["b.sh"]]},
        {"matcher": "Write|Edit|MultiEdit|NotebookEdit", "hooks": [e["c.py"]]},
        {"matcher": "*", "hooks": [e["d.py"]]},
        {"matcher": "mcp__agent-context__.*", "hooks": [e["e.sh"]]},
        {"matcher": "Glob, Grep", "hooks": [e["g.py"]]},
        {"matcher": "", "hooks": [e["h.sh"]]},
        {"hooks": [e["f.py"]]},
    ]})
    for label, tool, want in (
            ("guards whose matcher fits run in registry order", "Bash",
             ["a.py", "b.sh", "d.py", "h.sh", "f.py"]),
            ("an exact list selects each of its names", "Edit", ["c.py", "d.py", "h.sh", "f.py"]),
            ("an exact list matches a name whole: NotebookEdit", "NotebookEdit",
             ["c.py", "d.py", "h.sh", "f.py"]),
            ("a plain name is exact: Bash does not select BashOutput", "BashOutput",
             ["d.py", "h.sh", "f.py"]),
            ("a comma list with spaces is an exact list too", "Grep",
             ["d.py", "g.py", "h.sh", "f.py"]),
            ("a regex matcher selects what it matches", "mcp__agent-context__upsert_doc",
             ["d.py", "e.sh", "h.sh", "f.py"]),
            ("a regex matcher is unanchored, like the harness's RegExp.test",
             "x_mcp__agent-context__y", ["d.py", "e.sh", "h.sh", "f.py"])):
        r = s.run("PreToolUse", tool_call("PreToolUse", tool))
        check(label, r.rc == 0 and s.ran() == want, "%s; ran %s" % (r, s.ran()))

    s = Scene("stdin")
    outs = [s.path(n) for n in ("load.json", "read.json", "cat.json")]
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [
        s.py("load.py", "json.dump(json.load(sys.stdin), open(%r, 'w'))\n" % outs[0]),
        s.py("read.py", "open(%r, 'w').write(sys.stdin.read())\n" % outs[1]),
        s.sh("cat.sh", "cat > '%s'\n" % outs[2]),
    ]}]})
    payload = tool_call("PreToolUse", "Bash",
                        tool_input={"command": "echo " + chr(0xE9) + " 'quoted' " + chr(92) + " back"})
    r = s.run("PreToolUse", payload)
    got = [parse(read(p)) for p in outs]
    check("every guard reads the same whole payload: json.load, read() and a shell cat",
          len(s.ran()) == 3 and all(g == payload for g in got), "%s; got %s" % (r, got))

    s = Scene("dedup")
    once = s.py("once.py")
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [once]},
                            {"matcher": "Bash|Read", "hooks": [dict(once)]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"))
    check("an identical command in two matching groups runs once",
          r.rc == 0 and s.ran() == ["once.py"], "%s; ran %s" % (r, s.ran()))

    s = Scene("lifecycle")
    s.write({
        "SessionStart": [{"matcher": "compact", "hooks": [s.py("compact.py")]},
                         {"hooks": [s.py("every.py")]}],
        "SubagentStop": [{"matcher": "worker-review", "hooks": [s.py("review.py")]},
                         {"hooks": [s.py("anyagent.py")]}],
        "Stop": [{"matcher": "Bash", "hooks": [s.py("stop.py")]}],
        "UserPromptSubmit": [{"matcher": "never-a-prompt", "hooks": [s.py("prompt.py")]}],
    })
    for label, event, payload, want in (
            ("SessionStart matches source: compact", "SessionStart",
             lifecycle("SessionStart", source="compact"), ["compact.py", "every.py"]),
            ("SessionStart matches source: startup skips the compact group", "SessionStart",
             lifecycle("SessionStart", source="startup"), ["every.py"]),
            ("SubagentStop matches agent_type", "SubagentStop",
             lifecycle("SubagentStop", agent_type="worker-review"), ["review.py", "anyagent.py"]),
            ("SubagentStop skips a group for another agent_type", "SubagentStop",
             lifecycle("SubagentStop", agent_type="general-purpose"), ["anyagent.py"]),
            ("Stop ignores a matcher", "Stop", lifecycle("Stop"), ["stop.py"]),
            ("UserPromptSubmit ignores a matcher", "UserPromptSubmit",
             lifecycle("UserPromptSubmit", prompt="hi"), ["prompt.py"])):
        r = s.run(event, payload)
        check(label, r.rc == 0 and s.ran() == want, "%s; ran %s" % (r, s.ran()))

    s = Scene("registry")
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [s.py("home.py")]}]},
            path=os.path.join(s.home, ".agent-context", "hook-dispatch.json"))
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"), registry=False)
    check("without HOOK_DISPATCH_REGISTRY it reads ~/.agent-context/hook-dispatch.json",
          r.rc == 0 and s.ran() == ["home.py"], "%s; ran %s" % (r, s.ran()))

    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [s.py("quiet.py")]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Read"))
    check("an event with no matching guard: exit 0, no output, nothing recorded",
          r.rc == 0 and not r.out.strip() and s.ran() == []
          and not os.path.exists(s.health("hook-dispatch")) and os.path.exists(DISPATCH),
          str(r))

    for label, arrange in (
            ("an event absent from the registry",
             lambda: s.write({"PreToolUse": []})),
            ("a missing registry file", lambda: remove(s.registry)),
            ("a corrupt registry file", lambda: put(s.registry, "{not json"))):
        arrange()
        remove(s.health("hook-dispatch"))
        r = s.run("Stop", lifecycle("Stop"))
        check("%s fails open: exit 0 and a systemMessage naming hook-dispatch" % label,
              r.rc == 0 and "hook-dispatch" in (r.field("systemMessage") or ""), str(r))
        check("%s is recorded through health-record as hook-dispatch" % label,
              "Stop" in read(s.health("hook-dispatch")), clip(read(s.health("hook-dispatch"))))




def check_in_process():
    print("[2] Python guards in-process, shell guards as subprocesses")
    s = Scene("inproc")
    pids = {n: s.path(n + ".pid") for n in ("a", "b", "s", "bare")}
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [
        s.py("pa.py", "open(%r, 'w').write(str(os.getpid()))\n" % pids["a"]),
        s.py("pb.py", "open(%r, 'w').write(str(os.getpid()))\n" % pids["b"]),
        s.sh("ps.sh", "echo $$ > '%s'\n" % pids["s"]),
        s.sh("bare.sh", "echo $$ > '%s'\n" % pids["bare"], bare=True),
    ]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"))
    got = {n: read(p).strip() for n, p in pids.items()}
    check("both Python guards run in the dispatcher's own process",
          got["a"] == got["b"] == str(r.pid), "dispatcher %s, guards %s; %s" % (r.pid, got, r))
    check("shell guards run as subprocesses, as `bash <path>` and as a bare path",
          got["s"] and got["bare"] and str(r.pid) not in (got["s"], got["bare"])
          and got["s"] != got["bare"], "dispatcher %s, guards %s" % (r.pid, got))

    s = Scene("exits")
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [s.py(
        "main.py", "def main():\n    sys.stderr.write('refused from main')\n    sys.exit(2)\n\n\n"
                   "if __name__ == '__main__':\n    main()\n")]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"))
    check("a guard behind a __main__ check runs its main, and its exit 2 refuses",
          r.rc == 2 and "[main.py] refused from main" in r.err, str(r))
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [s.py(
        "top.py", "sys.stderr.write('refused at top level')\nsys.exit(2)\n")]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"))
    check("a guard with no __main__ check runs its top level",
          r.rc == 2 and "[top.py] refused at top level" in r.err, str(r))

    s = Scene("isolation")
    probe_py, probe_sh = s.path("probe.json"), s.path("probe.txt")
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [
        s.py("leak.py", "os.environ['DISPATCH_LEAK'] = '1'\nos.chdir('/')\n"
                        "sys.path.insert(0, '/dispatch-leak')\nsys.argv.append('--leak')\n"),
        s.py("probe.py", "json.dump({'env': os.environ.get('DISPATCH_LEAK'), 'cwd': os.getcwd(), "
                         "'path': '/dispatch-leak' in sys.path, 'argv': sys.argv[1:]}, "
                         "open(%r, 'w'))\n" % probe_py),
        s.sh("probe.sh", "echo \"${DISPATCH_LEAK:-unset} $PWD\" > '%s'\n" % probe_sh),
    ]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"))
    got = parse(read(probe_py)) or {}
    shell = (read(probe_sh).split() + ["", ""])[:2]
    ran = s.ran() == ["leak.py", "probe.py", "probe.sh"]
    check("a guard's environment change does not reach the next guard",
          ran and "env" in got and got["env"] is None and shell[0] == "unset",
          "%s; probe %s; shell %s" % (r, got, shell))
    check("nor does its working directory",
          ran and got.get("cwd") and os.path.realpath(got["cwd"]) == os.path.realpath(s.dir)
          and shell[1] and os.path.realpath(shell[1]) == os.path.realpath(s.dir),
          "probe %s; shell %s" % (got, shell))
    check("nor its sys.path or sys.argv",
          ran and got.get("path") is False and got.get("argv") == [], "probe %s" % got)

    s = Scene("child")
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [
        s.py("child.py", "import subprocess\nsubprocess.run(['/bin/echo', 'child-wrote-this'])\n"),
        say_py(s, "ctx.py", pre_output(additionalContext="ctx-after-child")),
    ]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"))
    check("output from a child a Python guard spawns cannot corrupt the dispatcher's JSON",
          r.rc == 0 and r.json_ok and "ctx-after-child" in (r.hso("additionalContext") or ""),
          str(r))




def check_merge():
    print("[4] merging what the guards said")
    s = Scene("merge")

    def scenario(event, hooks, payload=None):
        s.write({event: [{"hooks": hooks}]})
        return s.run(event, payload or tool_call(event, "Bash"))

    r = scenario("PreToolUse", [
        refuse_sh(s, "one.sh", "reason-one"),
        say_py(s, "two.py", pre_output(permissionDecision="deny",
                                       permissionDecisionReason="reason-two")),
        say_py(s, "three.py", dict(pre_output(additionalContext="ctx-three"),
                                   systemMessage="msg-three")),
    ])
    check("any refusal wins: the dispatcher runs its guards, then exits 2",
          r.rc == 2 and s.ran()[:1] == ["one.sh"], "%s; ran %s" % (r, s.ran()))
    check("every refusal reaches stderr in order, each named [<guard file>]",
          in_order(r.err, "[one.sh] reason-one", "[two.py] reason-two"), str(r))
    check("every guard still runs after a refusal",
          s.ran() == ["one.sh", "two.py", "three.py"], "ran %s" % s.ran())
    check("context and systemMessage still ride along as JSON on a refusal",
          r.rc == 2 and r.json_ok and "ctx-three" in (r.hso("additionalContext") or "")
          and "msg-three" in (r.field("systemMessage") or ""), str(r))

    r = scenario("PreToolUse", [
        say_py(s, "rewrite.py", pre_output(permissionDecision="allow",
                                           updatedInput={"command": "rewritten"})),
        refuse_sh(s, "no.sh", "still-no"),
    ])
    check("a refusal beats another guard's allow and updatedInput",
          r.rc == 2 and "[no.sh] still-no" in r.err and r.hso("permissionDecision") != "allow"
          and r.hso("updatedInput") is None, str(r))

    r = scenario("PreToolUse", [
        say_py(s, "first.py", pre_output(permissionDecision="allow",
                                         updatedInput={"command": "first"})),
        say_py(s, "second.py", pre_output(permissionDecision="allow",
                                          updatedInput={"command": "second"})),
    ])
    check("updatedInput comes from the first guard that sets it",
          r.rc == 0 and r.hso("updatedInput") == {"command": "first"}
          and r.hso("permissionDecision") == "allow" and r.hso("hookEventName") == "PreToolUse",
          str(r))

    r = scenario("PreToolUse", [
        say_py(s, "allow1.py", pre_output(permissionDecision="allow")),
        say_py(s, "ask.py", pre_output(permissionDecision="ask", permissionDecisionReason="ask-why")),
        say_py(s, "allow2.py", pre_output(permissionDecision="allow")),
    ])
    check("without a refusal, ask outranks allow and keeps its reason",
          r.rc == 0 and r.hso("permissionDecision") == "ask"
          and "ask-why" in (r.hso("permissionDecisionReason") or ""), str(r))
    r = scenario("PreToolUse", [
        say_py(s, "ask2.py", pre_output(permissionDecision="ask")),
        say_py(s, "defer.py", pre_output(permissionDecision="defer")),
    ])
    check("and defer outranks ask", r.rc == 0 and r.hso("permissionDecision") == "defer", str(r))

    r = scenario("PreToolUse", [
        say_py(s, "ctx1.py", dict(pre_output(additionalContext="ctx-one"), systemMessage="msg-one")),
        say_sh(s, "ctx2.sh", dict(pre_output(additionalContext="ctx-two"), systemMessage="msg-two")),
    ])
    check("additionalContext from every guard, joined in order",
          r.rc == 0 and in_order(r.hso("additionalContext"), "ctx-one", "ctx-two"), str(r))
    check("systemMessage from every guard, joined in order",
          r.rc == 0 and in_order(r.field("systemMessage"), "msg-one", "msg-two"), str(r))

    r = scenario("PreToolUse", [s.py("mute.py"), s.sh("mute.sh")])
    check("guards that say nothing: exit 0 and no output",
          r.rc == 0 and not r.out.strip() and s.ran() == ["mute.py", "mute.sh"], str(r))

    r = scenario("PreToolUse", [s.py("note.py", "sys.stderr.write('side-note')\n")])
    check("a guard's stderr passes through when nothing refuses",
          r.rc == 0 and "side-note" in r.err, str(r))

    r = scenario("Stop", [
        say_py(s, "block.py", {"decision": "block", "reason": "stop-one"}),
        refuse_sh(s, "stop.sh", "stop-two"),
        s.py("stop-quiet.py"),
    ], lifecycle("Stop", stop_hook_active=False))
    check("a Stop block collects every reason, in order",
          r.rc == 2 and in_order(r.err, "[block.py] stop-one", "[stop.sh] stop-two"), str(r))

    r = scenario("PostToolUse", [say_py(s, "post.py", {"decision": "block", "reason": "post-one"})])
    check("a PostToolUse decision block is a refusal like any other",
          r.rc == 2 and "[post.py] post-one" in r.err, str(r))

    r = scenario("UserPromptSubmit", [
        s.sh("plain.sh", "echo plain-one\n"),
        say_py(s, "ups.py", {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                    "additionalContext": "ctx-two"}}),
    ], lifecycle("UserPromptSubmit", prompt="hello"))
    check("plain stdout is context on UserPromptSubmit, joined in order with JSON context",
          r.rc == 0 and in_order(r.hso("additionalContext"), "plain-one", "ctx-two")
          and r.hso("hookEventName") == "UserPromptSubmit", str(r))

    r = scenario("SessionStart", [
        s.sh("banner.sh", "echo session-plain\n"),
        say_py(s, "sm.py", {"systemMessage": "session-note"}),
    ], lifecycle("SessionStart", source="startup"))
    check("plain stdout is context on SessionStart",
          r.rc == 0 and "session-plain" in (r.hso("additionalContext") or "")
          and r.hso("hookEventName") == "SessionStart", str(r))




DENY_JSON = pre_output(permissionDecision="deny", permissionDecisionReason="should-not-count")


def check_fail_open():
    print("[5] a broken guard fails open and is recorded")
    s = Scene("failopen")
    bash = tool_call("PreToolUse", "Bash")

    for label, name, make, cause in (
            ("a guard that raises", "raise.py",
             lambda: s.py("raise.py", "print(json.dumps(%r))\nraise ValueError('boom')\n" % (DENY_JSON,)),
             "ValueError"),
            ("a Python guard that exits 3", "exit3.py",
             lambda: s.py("exit3.py", "print(json.dumps(%r))\nsys.exit(3)\n" % (DENY_JSON,)),
             "exit 3"),
            ("a shell guard that exits 1", "exit1.sh",
             lambda: s.sh("exit1.sh", "printf '%%s\\n' '%s'\necho sh-broke >&2\nexit 1\n"
                          % json.dumps(DENY_JSON)),
             "exit 1"),
            ("a guard that exits with a message", "message.py",
             lambda: s.py("message.py", "sys.exit('went wrong')\n"), "exit 1"),
            ("a Python guard past its timeout", "slow.py",
             lambda: s.py("slow.py", "import time\ntime.sleep(30)\n", timeout=1), "timed out")):
        s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [make(), s.py("after.py")]}]})
        record = s.health("hook-dispatch-" + name.rsplit(".", 1)[0])
        remove(record)
        r = s.run("PreToolUse", bash)
        check("%s is skipped, never refuses, and the next guard runs" % label,
              r.rc == 0 and "should-not-count" not in r.out + r.err
              and s.ran() == [name, "after.py"] and r.secs < 10, "%s; ran %s" % (r, s.ran()))
        check("%s is named in a systemMessage" % label,
              name in (r.field("systemMessage") or ""), str(r))
        check("%s is recorded through health-record with event, guard and cause" % label,
              all(part in read(record) for part in ("PreToolUse", name, cause)),
              clip(read(record)))

    late = s.path("late.marker")
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [
        s.sh("slow.sh", "sleep 4\ntouch '%s'\n" % late, timeout=1), s.py("after.py")]}]})
    remove(s.health("hook-dispatch-slow"))
    r = s.run("PreToolUse", bash)
    time.sleep(4.5)
    check("a shell guard past its timeout is killed, not left running",
          r.rc == 0 and r.secs < 3.5 and s.ran() == ["slow.sh", "after.py"]
          and not os.path.exists(late) and "timed out" in read(s.health("hook-dispatch-slow")),
          "%s in %.1fs; ran %s; late marker %s" % (r, r.secs, s.ran(), os.path.exists(late)))

    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [
        s.py("crash.py", "raise RuntimeError('crash')\n"), refuse_sh(s, "guard.sh", "still-refused")]}]})
    remove(s.health("hook-dispatch-guard"))
    r = s.run("PreToolUse", bash)
    check("a crash does not stop a later guard's refusal",
          r.rc == 2 and "[guard.sh] still-refused" in r.err, str(r))
    check("a refusal is not a fault: nothing is recorded for the refusing guard",
          r.rc == 2 and os.path.exists(s.health("hook-dispatch-crash"))
          and not os.path.exists(s.health("hook-dispatch-guard")), str(r))

    flag = put(s.path("flaky.flag"), "")
    s.write({"PreToolUse": [
        {"matcher": "Bash", "hooks": [s.py("flaky.py", "if os.path.exists(%r):\n"
                                                        "    raise RuntimeError('flaky')\n" % flag)]},
        {"matcher": "Edit", "hooks": [s.py("steady.py")]}]})
    record = s.health("hook-dispatch-flaky")
    remove(record)
    s.run("PreToolUse", bash)
    recorded = os.path.exists(record)
    s.run("PreToolUse", tool_call("PreToolUse", "Edit"))
    survived = os.path.exists(record) and s.ran() == ["steady.py"]
    remove(flag)
    s.run("PreToolUse", bash)
    check("a guard's record survives another guard's clean run",
          recorded and survived, "recorded %s, survived %s" % (recorded, survived))
    check("and clears on that guard's own next clean run",
          recorded and survived and not os.path.exists(record) and s.ran() == ["flaky.py"],
          "still there: %s" % os.path.exists(record))




def check_parity():
    print("[3] every hook-test-cases case passes through the dispatcher")
    cases = load("hook_test_cases_for_dispatch", os.path.join(HERE, "hook-test-cases.py")).CASES
    total = sum(len(specs) for specs in cases.values())
    want = "%d case(s), 0 failure(s), via hook-dispatch.py" % total
    proc = subprocess.run([sys.executable, RUNNER, "--via-dispatch"],
                          capture_output=True, text=True, timeout=1800)
    check("hook-test-run --via-dispatch reports " + want,
          proc.returncode == 0 and want in proc.stdout, clip(proc.stdout, 1500))
    proc = subprocess.run([sys.executable, RUNNER, "--via-dispatch", "--hook", "block-deploy",
                           "--verbose"], capture_output=True, text=True, timeout=300)
    check("--via-dispatch really routes each case: a refusal carries [block-deploy.py]",
          proc.returncode == 0 and "[block-deploy.py]" in proc.stdout, clip(proc.stdout, 800))




def sync_file(path, events, registry=None):
    'Sync a fixture settings file. The registry goes beside it unless a path is given, and\n    never to the live one: $HOOK_DISPATCH_REGISTRY is always set.'
    registry = registry or os.path.join(os.path.dirname(path), "hook-dispatch.json")
    return subprocess.run([sys.executable, SYNC, path, "--dispatch", events],
                          capture_output=True, text=True, timeout=180,
                          env=dict(os.environ, HOOK_DISPATCH_REGISTRY=registry))


def registry_beside(path):
    'The hooks map of hook-dispatch.json next to a settings file; None when absent.'
    text = read(os.path.join(os.path.dirname(path), "hook-dispatch.json"))
    if not text:
        return None
    doc = parse(text)
    return doc.get("hooks") if isinstance(doc, dict) else "corrupt"


def check_registration():
    print("[6] home-settings-sync registers one dispatcher per dispatched event")
    ap = load("agent_python_for_dispatch", os.path.join(HERE, "agent-python.py"))
    dispatcher = "%s %s" % (ap.interpreter(),
                            os.path.join(REAL_HOME, ".agent-context", "global", "scripts", "hook-dispatch.py"))
    start = json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": LOCAL}]}]}})
    root = os.path.join(TMP, "sync")

    def fresh(name, events):
        path = put(os.path.join(root, name, "settings.json"), start)
        sync_file(path, events)
        return path, parse(read(path)) or {"hooks": {}}, registry_beside(path)

    plain_path, plain, _ = fresh("plain", "")
    disp_path, disp, reg = fresh("pre", "PreToolUse")
    reg = reg if isinstance(reg, dict) else {}

    pre = disp["hooks"].get("PreToolUse", [])
    entries = [(g, h) for g in pre for h in g.get("hooks") or []]
    ours = [(g, h) for g, h in entries if h.get("command", "").startswith(dispatcher)]
    others = [h.get("command") for g, h in entries if not h.get("command", "").startswith(dispatcher)]
    dispatched = (len(ours) == 1 and ours[0][1].get("command") == dispatcher + " PreToolUse"
                  and not ours[0][0].get("matcher"))
    check("PreToolUse becomes one dispatcher command, host interpreter, no matcher",
          dispatched, clip(json.dumps(pre), 600))
    check("no managed guard stays registered directly, and a machine-local hook survives",
          dispatched and others == [LOCAL], "direct entries: %s" % others)

    managed_only = []
    for g in plain["hooks"].get("PreToolUse", []):
        kept = [h for h in g.get("hooks") or [] if h.get("command") != LOCAL]
        if kept:
            managed_only.append(dict(g, hooks=kept))
    check("hook-dispatch.json holds exactly PreToolUse's guards as settings.json listed them",
          dispatched and set(reg) == {"PreToolUse"} and reg["PreToolUse"] == managed_only,
          clip(json.dumps(reg), 400))
    check("every other event is untouched",
          dispatched and {k: v for k, v in disp["hooks"].items() if k != "PreToolUse"}
          == {k: v for k, v in plain["hooks"].items() if k != "PreToolUse"})
    budget = sum(h.get("timeout", 600) for g in reg.get("PreToolUse", []) for h in g.get("hooks") or [])
    check("the dispatcher's timeout covers every guard's",
          dispatched and budget > 0 and ours[0][1].get("timeout", 600) >= budget,
          "budget %s, entry %s" % (budget, ours[0][1] if ours else None))

    reg_path = os.path.join(os.path.dirname(disp_path), "hook-dispatch.json")
    before = [(read(p), os.stat(p).st_mtime_ns) if os.path.exists(p) else None
              for p in (disp_path, reg_path)]
    again = sync_file(disp_path, "PreToolUse")
    after = [(read(p), os.stat(p).st_mtime_ns) if os.path.exists(p) else None
             for p in (disp_path, reg_path)]
    check("a second sync is a no-op: neither file is rewritten",
          dispatched and "already in sync" in again.stdout and before == after, again.stdout)

    sync_file(disp_path, "")
    back = parse(read(disp_path))
    left = registry_beside(disp_path)
    check("undispatching restores the per-guard entries exactly",
          dispatched and back == plain and (left is None or "PreToolUse" not in left),
          clip(json.dumps(left), 200))

    _, stop, stop_reg = fresh("stop", "Stop")
    direct = [h for g in stop["hooks"].get("Stop", []) for h in g.get("hooks") or []]
    direct_names = sorted(os.path.basename(script_of(h.get("command", ""))) for h in direct)
    stop_names = names_in((stop_reg or {}).get("Stop") if isinstance(stop_reg, dict) else [])
    
    
    check("Stop: the dispatcher is the only direct entry",
          direct_names == ["hook-dispatch.py"] and "terse-output-gate.py" in stop_names,
          "direct %s; registry %s" % (direct_names, stop_names))

    _, ss, ss_reg = fresh("session", "SessionStart")
    ss_direct = names_in(ss["hooks"].get("SessionStart"))
    ss_groups = (ss_reg or {}).get("SessionStart") if isinstance(ss_reg, dict) else []
    check("SessionStart: home-materialize stays registered directly, ahead of the dispatcher",
          ss_direct[:1] == ["home-materialize.py"]
          and sorted(ss_direct) == ["home-materialize.py", "hook-dispatch.py"]
          and "home-materialize.py" not in names_in(ss_groups), "direct %s" % ss_direct)
    check("SessionStart: the compact-only group keeps its matcher inside the registry",
          any(g.get("matcher") == "compact" and "compact-invalidates-bootstrap.py" in names_in([g])
              for g in ss_groups or []), clip(json.dumps(ss_groups), 300))

    _, multi, multi_reg = fresh("multi", "PreToolUse,Stop")
    check("several events in one --dispatch list",
          isinstance(multi_reg, dict) and set(multi_reg) == {"PreToolUse", "Stop"}
          and "hook-dispatch.py" in names_in(multi["hooks"].get("PreToolUse"))
          and "hook-dispatch.py" in names_in(multi["hooks"].get("Stop")),
          clip(json.dumps(sorted(multi_reg) if isinstance(multi_reg, dict) else multi_reg)))




READER_PROBE = r'''
import importlib.util, json, sys
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
here, names = sys.argv[1], sys.argv[2].split(",")
out = {}
out["interaction"] = load("hir", here + "/hook-interaction-report.py").registered()
out["firing"] = {k: sorted(v) for k, v in load("hfr", here + "/hook-firing-report.py").registered().items()}
inv = load("inv", here + "/invariant-check.py")
out["events"] = {n: sorted(inv._deployed_events(n) or []) for n in names}
out["matchers"] = {n: inv._deployed_matchers(n) for n in names}
out["crash_watch"] = load("pcw", here + "/preflight-crash-watch.py").registered()
print(json.dumps(out, sort_keys=True))
'''

READER_NAMES = ("block-deploy,plain-language-check,compact-invalidates-bootstrap,"
                "preflight-core-health,record-session-claim,failure-trace,"
                "require-store-bootstrap,lsp-failure-tripwire,terse-output-gate")


def check_readers():
    print("[7] readers of the hook wiring see through the dispatcher")
    ap = load("agent_python_for_readers", os.path.join(HERE, "agent-python.py"))
    dispatcher = "%s %s" % (ap.interpreter(),
                            os.path.join(REAL_HOME, ".agent-context", "global", "scripts", "hook-dispatch.py"))
    homes, settings = {}, {}
    for label, events in (("plain", ""), ("dispatched", ALL_EVENTS)):
        home = os.path.join(TMP, "readers-" + label)
        target = put(os.path.join(home, ".claude", "settings.json"), json.dumps({"hooks": {}}))
        sync_file(target, events, os.path.join(home, ".agent-context", "hook-dispatch.json"))
        homes[label] = home
        settings[label] = parse(read(target)) or {"hooks": {}}

    direct = {os.path.basename(script_of(h.get("command", "")))
              for groups in settings["dispatched"]["hooks"].values()
              for g in groups for h in g.get("hooks") or []}
    fixture_ok = (direct == {"hook-dispatch.py", "home-materialize.py", "worktree-create.py"}
                  and isinstance(registry_beside(os.path.join(homes["dispatched"], ".agent-context",
                                                              "settings.json")), dict))
    check("fixture: every event in the dispatched home registers the dispatcher",
          fixture_ok, "direct entries: %s" % sorted(direct))

    answers = {}
    for label, home in homes.items():
        env = dict(os.environ, HOME=home, AGENT_CONTEXT_STORE=STORE)
        proc = subprocess.run([sys.executable, "-c", READER_PROBE, HERE, READER_NAMES],
                              capture_output=True, text=True, env=env, timeout=180)
        answers[label] = parse(proc.stdout) if proc.returncode == 0 else {"error": clip(proc.stderr)}
    plain, disp = answers["plain"] or {}, answers["dispatched"] or {}
    for key, label in (("interaction", "hook-interaction-report sees the same chain, in order"),
                       ("firing", "hook-firing-report sees the same events per guard"),
                       ("events", "invariant-check sees the same events per hook"),
                       ("matchers", "invariant-check sees the same matchers per hook"),
                       ("crash_watch", "preflight-crash-watch still finds preflight on SessionStart")):
        check(label, fixture_ok and plain.get(key) not in (None, {}, [], False)
              and plain.get(key) == disp.get(key),
              "plain %s; dispatched %s" % (clip(json.dumps(plain.get(key)), 200),
                                           clip(json.dumps(disp.get(key)), 200)))

    guard_names = {os.path.basename(script_of(h.get("command", "")))
                   for groups in settings["plain"]["hooks"].values()
                   for g in groups for h in g.get("hooks") or []}
    verdicts = {}
    for label, home in homes.items():
        for name in guard_names:
            put(os.path.join(home, ".agent-context", "global", "hooks", name), "")
        out = os.path.join(home, ".local", "state", "agent-context", "health", "hooks.json")
        remove(out)
        subprocess.run([sys.executable, os.path.join(HERE, "hook-registration-probe.py")],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, HOME=home))
        verdicts[label] = read(out)
    reg_path = os.path.join(homes["dispatched"], ".agent-context", "hook-dispatch.json")
    hidden = reg_path + ".hidden"
    if os.path.exists(reg_path):
        os.rename(reg_path, hidden)
    out = os.path.join(homes["dispatched"], ".local", "state", "agent-context", "health", "hooks.json")
    subprocess.run([sys.executable, os.path.join(HERE, "hook-registration-probe.py")],
                   capture_output=True, text=True, timeout=60,
                   env=dict(os.environ, HOME=homes["dispatched"]))
    without = read(out)
    if os.path.exists(hidden):
        os.rename(hidden, reg_path)
    check("hook-registration-probe finds dispatched guards in the registry, and misses them "
          "without it", fixture_ok and verdicts["plain"] == "" and verdicts["dispatched"] == ""
          and "block-deploy.py" in without,
          "plain %r; dispatched %r; without registry %r"
          % (clip(verdicts["plain"], 120), clip(verdicts["dispatched"], 120), clip(without, 120)))

    home = homes["dispatched"]

    def refusal(text):
        return {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": text}]}}

    put(os.path.join(home, ".claude", "projects", "fixture", "session.jsonl"), "".join(
        json.dumps(rec) + "\n" for rec in (
            refusal("PreToolUse:Bash hook error: [%s PreToolUse]: [block-deploy.py] Blocked: "
                    "deploy scripts need a go-ahead" % dispatcher),
            refusal("Stop hook error: [%s Stop]: [terse-output-gate.py] too long" % dispatcher),
            refusal("PreToolUse:Bash hook error: [%s/.claude/hooks/block-git-stash.sh]: Blocked: "
                    "git stash" % REAL_HOME))))
    proc = subprocess.run([sys.executable, os.path.join(HERE, "hook-firing-report.py")],
                          capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, HOME=home, AGENT_CONTEXT_STORE=STORE))
    report = proc.stdout

    def fired(name, event):
        return re.search(r"^\s+%s\s+\d+\s+\S*%s" % (re.escape(name), event), report, re.M)

    check("hook-firing-report attributes a dispatcher refusal to its guard",
          fixture_ok and fired("block-deploy", "PreToolUse") and fired("terse-output-gate", "Stop"),
          clip(report, 600))
    check("and still counts the shape a direct registration leaves",
          bool(fired("block-git-stash", "PreToolUse")), clip(report, 600))
    check("and never lists the dispatcher itself as a guard",
          report.strip() and "hook-dispatch" not in report, clip(report, 600))


def check_invariant():
    print("[7b] invariant %s" % INVARIANT)
    fixture = os.path.join(TMP, "inv-store")
    scripts = os.path.join(fixture, "global", "scripts")
    os.makedirs(os.path.join(fixture, "global", "hooks"))
    reader = put(os.path.join(scripts, "reads-wiring.py"),
                 "import json, os\n\n\ndef registered():\n"
                 "    path = os.path.expanduser('~/.claude/settings.json')\n"
                 "    with open(path) as fh:\n        cfg = json.load(fh)\n"
                 "    return list((cfg.get('hooks') or {}).items())\n")
    through = put(os.path.join(scripts, "reads-registry.py"),
                  "import importlib.util, os\n\n\ndef registered():\n"
                  "    here = os.path.dirname(os.path.abspath(__file__))\n"
                  "    spec = importlib.util.spec_from_file_location('hook_registry', "
                  "os.path.join(here, 'hook-registry.py'))\n"
                  "    mod = importlib.util.module_from_spec(spec)\n"
                  "    spec.loader.exec_module(mod)\n"
                  "    return mod.effective_hooks(os.path.expanduser('~/.claude/settings.json'))\n")
    unrelated = put(os.path.join(scripts, "reads-other.py"),
                    "import json\n\n\ndef servers(path):\n    with open(path) as fh:\n"
                    "        return json.load(fh).get('servers')\n")
    saved = os.environ.get("AGENT_CONTEXT_STORE")
    try:
        os.environ["AGENT_CONTEXT_STORE"] = fixture
        mod = load("invariant_check_fixture", CHECK)
        inv = next((i for i in mod.REGISTRY if i.id == INVARIANT), None)
        check("%s is registered" % INVARIANT, inv is not None)
        if inv is not None:
            sites = {os.path.realpath(p) for p in inv.sites()}
            check("a script that reads settings.json hooks without the registry is reported",
                  os.path.realpath(reader) in sites and bool(inv.violated(reader, mod._read(reader))))
            check("a script that reads through hook-registry.py is not",
                  not inv.violated(through, mod._read(through)))
            check("a script reading some other JSON is not",
                  not inv.violated(unrelated, mod._read(unrelated)))
        os.environ["AGENT_CONTEXT_STORE"] = STORE
        mod = load("invariant_check_store", CHECK)
        inv = next((i for i in mod.REGISTRY if i.id == INVARIANT), None)
        found = inv.run()[0] if inv is not None else None
        check("the store holds no reader that skips the registry",
              found == [], clip(json.dumps(found), 400))
    finally:
        if saved is None:
            os.environ.pop("AGENT_CONTEXT_STORE", None)
        else:
            os.environ["AGENT_CONTEXT_STORE"] = saved




def check_unsigned_setup():
    print("[8] hook-test-run's setup commits never need a signing key")
    root = os.path.join(TMP, "signing")
    config = put(os.path.join(root, "gitconfig"),
                 "[user]\n\tname = t\n\temail = t@t\n\tsigningkey = %s\n[gpg]\n\tformat = ssh\n"
                 "[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n"
                 % os.path.join(root, "no-such-key.pub"))
    env = dict(os.environ, GIT_CONFIG_GLOBAL=config)
    for hook in ("guard-git-write", "require-worktree-edit"):
        proc = subprocess.run([sys.executable, RUNNER, "--hook", hook], capture_output=True,
                              text=True, env=env, timeout=600)
        check("%s cases pass under a global config that signs with a missing key" % hook,
              proc.returncode == 0 and " 0 failure(s)" in proc.stdout
              and "setup command failed" not in proc.stdout, clip(proc.stdout, 800))


def main():
    try:
        for step in (check_selection, check_in_process, check_merge, check_fail_open,
                     check_registration, check_readers, check_invariant,
                     check_unsigned_setup, check_parity):
            try:
                step()
            except Exception as exc:  
                check("%s ran to completion" % step.__name__, False,
                      "%s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (passed, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
