#!/usr/bin/env python3
'Battery for hook-parity-run.py: direct vs dispatched parity for one hook event.\n\nEvery source is a fixture: ~/.claude, settings.json, the store and the live root. Hooks\nare stubs at the names home-settings-sync.py wires, so the wiring under test is the real\none. A stub tells the sides apart by HOOK_DISPATCH_REGISTRY, which only the dispatched\nside sets.\n\nUsage: test-hook-parity-run.py'

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "hook-parity-run.py")
SYNC = os.path.join(HERE, "home-settings-sync.py")
TMP = tempfile.mkdtemp(prefix="hook-parity-test-")
BOTH = ("PostToolUse", "PostToolUseFailure")

passed = 0
failures = []


def check(label, ok, detail: object = ""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + str(detail)[:400] if detail else ""))


def put(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)
    return path


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SETTINGS_SYNC = load(SYNC, "home_settings_sync")
MANAGED_NAMES = sorted({SETTINGS_SYNC.command_basename(path)
                        for _event, _matcher, cmds in SETTINGS_SYNC.MANAGED
                        for path, _extra in cmds})

SILENT_SH = "#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n"
SILENT_PY = "import sys\nsys.stdin.read()\nsys.exit(0)\n"



SIGNING_KEY = os.path.join(TMP, "fixture-signing-key")
subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", SIGNING_KEY],
               check=True, capture_output=True, stdin=subprocess.DEVNULL)
os.chmod(SIGNING_KEY, 0o600)  
SIGN = ["-c", "gpg.format=ssh", "-c", "gpg.ssh.program=" + (shutil.which("ssh-keygen") or "ssh-keygen"),
        "-c", "user.signingkey=" + SIGNING_KEY, "-c", "commit.gpgsign=true"]


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"] + SIGN
                   + list(args), check=True, capture_output=True, text=True)


def source(name, overrides=None):
    "One scenario's sources under TMP/<name>: claude/{hooks,scripts}, settings.json with\n    one machine-local PostToolUse guard, a store with a remote, and a live root."
    root = os.path.join(TMP, name)
    for base in MANAGED_NAMES:
        body = (overrides or {}).get(base) or (SILENT_PY if base.endswith(".py") else SILENT_SH)
        put(os.path.join(root, "claude", "hooks", base), body, 0o755)
    shutil.copytree(HERE, os.path.join(root, "claude", "scripts"),
                    ignore=shutil.ignore_patterns("*.meta.toml", "__pycache__"))
    local = put(os.path.join(root, "local-guard.sh"), SILENT_SH, 0o755)
    put(os.path.join(root, "settings.json"), json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash " + local}]}]}},
        indent=2) + "\n")
    store = os.path.join(root, "store")
    put(os.path.join(store, "README.md"), "fixture store\n")
    put(os.path.join(store, ".agents", "tmp", ".keep"), "")
    git(store, "init", "-q")
    git(store, "add", "-A")
    git(store, "commit", "-qm", "base")
    git(store, "remote", "add", "origin", "https://example.invalid/store.git")
    os.makedirs(os.path.join(root, "live"))
    return root


def run(root, *extra, events=BOTH):
    print("  running hook-parity-run.py for scenario %s" % os.path.basename(root),
          file=sys.stderr, flush=True)
    argv = [sys.executable, TOOL]
    for event in events:
        argv += ["--event", event]
    argv += ["--settings", os.path.join(root, "settings.json"),
             "--claude-dir", os.path.join(root, "claude"),
             "--store", os.path.join(root, "store"),
             "--live-root", os.path.join(root, "live"),
             "--runs", "1", "--json"] + list(extra)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        doc = None
    return proc.returncode, (doc if isinstance(doc, dict) else {}), proc


def tail(proc):
    return ((proc.stderr or "") + (proc.stdout or ""))[-400:]


def payloads(doc):
    return doc.get("payloads") or []


def mismatch_text(doc):
    return "\n".join(m for p in payloads(doc) for m in (p.get("mismatches") or []))


def py_stub(body):
    return "import json, os, sys, time\npayload = json.loads(sys.stdin.read() or '{}')\n" + body


print("[6] [7] [8] [9] [12] identical sides")
root = source("clean")
before = open(os.path.join(root, "settings.json"), encoding="utf-8").read()
code, doc, proc = run(root, "--keep")
check("[13] identical sides exit 0", code == 0, tail(proc))
registry = doc.get("registry") or {}
for event in BOTH:
    check("[7] the registry matches the direct wiring for %s" % event,
          (registry.get(event) or {}).get("ok") is True, registry.get(event))
found = payloads(doc)
check("[8] built-in payloads cover both events",
      {p.get("event") for p in found} >= set(BOTH), [p.get("event") for p in found])
check("[8] at least eight built-in PostToolUse payloads",
      sum(p.get("event") == "PostToolUse" for p in found) >= 8, len(found))
check("[9] [10] every payload passes on identical stubs",
      bool(found) and all(p.get("ok") is True for p in found), mismatch_text(doc))
check("[12] every payload reports both wall times",
      bool(found) and all(isinstance(p.get("direct_ms"), (int, float))
                          and isinstance(p.get("dispatched_ms"), (int, float)) for p in found))
check("[11] nothing reached the live root", (doc.get("live") or {}).get("ok") is True,
      doc.get("live"))
check("[6] the source settings file is not written",
      open(os.path.join(root, "settings.json"), encoding="utf-8").read() == before)
fixture = doc.get("fixture")
check("[6] --keep names the fixture directory", bool(fixture) and os.path.isdir(fixture), fixture)
if fixture and os.path.isdir(fixture):
    home = os.path.join(fixture, "home")
    clone = os.path.join(home, ".agent-context")
    remotes = subprocess.run(["git", "-C", clone, "remote"], capture_output=True,
                             text=True).stdout.strip()
    check("[6] the store clone exists and has no remotes",
          os.path.isdir(os.path.join(clone, ".git")) and remotes == "", remotes)
    scripts = os.path.join(home, ".agent-context", "global", "scripts")
    check("[6] the fixture scripts hold the dispatcher but not lsp-canary.py",
          os.path.isfile(os.path.join(scripts, "hook-dispatch.py"))
          and not os.path.exists(os.path.join(scripts, "lsp-canary.py")))
    direct = load_json(os.path.join(home, ".claude", "settings.json")) or {}
    wired = load_json(os.path.join(fixture, "wired", "settings.json")) or {}
    reg = load_json(os.path.join(fixture, "wired", "hook-dispatch.json")) or {}
    direct_cmds = [h.get("command", "") for g in (direct.get("hooks") or {}).get("PostToolUse") or []
                   for h in g.get("hooks") or []]
    wired_cmds = [h.get("command", "") for g in (wired.get("hooks") or {}).get("PostToolUse") or []
                  for h in g.get("hooks") or []]
    hooks_dir = os.path.join(home, ".agent-context", "global", "hooks")
    check("[6] the direct wiring runs the fixture's hooks with no dispatcher",
          any(os.path.join(hooks_dir, "agents-remateralize.py") in c for c in direct_cmds)
          and not any("hook-dispatch.py" in c for c in direct_cmds), direct_cmds)
    check("[6] the dispatched wiring holds one PostToolUse dispatcher command",
          sum("hook-dispatch.py" in c and c.endswith(" PostToolUse") for c in wired_cmds) == 1,
          wired_cmds)
    
    
    check("[6] the machine-local guard stays a direct entry, outside the registry",
          any("local-guard.sh" in c for c in wired_cmds)
          and "local-guard.sh" not in json.dumps(reg.get("hooks") or {}), wired_cmds)
    shutil.rmtree(fixture, ignore_errors=True)

print("[10] advisory output, added context and a refusal")
root = source("advisory", {
    "failure-trace.py": py_stub("print(json.dumps({'systemMessage': 'trace said'}))\n"),
    "warn-truncated-search.py": py_stub(
        "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse', "
        "'additionalContext': 'search note'}}))\n"),
    "read-width-nudge.sh": "#!/usr/bin/env bash\ncat >/dev/null\necho 'nudge refused' >&2\n"
                           "exit 2\n",
})
code, doc, proc = run(root)
check("[10] messages, context and a refusal merge the same on both sides", code == 0,
      mismatch_text(doc) or tail(proc))

print("[10] output that differs under the dispatcher")
root = source("output-diverges", {
    "failure-trace.py": py_stub("if os.environ.get('HOOK_DISPATCH_REGISTRY'):\n"
                                "    print(json.dumps({'systemMessage': 'only dispatched'}))\n"),
})
code, doc, proc = run(root)
check("[13] a differing output exits 1", code == 1, tail(proc))
check("[10] the mismatch names the systemMessage", "systemMessage" in mismatch_text(doc),
      mismatch_text(doc))

print("[10] an effect that differs under the dispatcher")
root = source("effect-diverges", {
    "agents-remateralize.py": py_stub(
        "if os.environ.get('HOOK_DISPATCH_REGISTRY'):\n"
        "    open(os.path.join(os.environ['HOME'], 'diverged'), 'w').close()\n"),
})
code, doc, proc = run(root)
check("[13] a differing effect exits 1", code == 1, tail(proc))
check("[10] the mismatch names the file only one side wrote", "diverged" in mismatch_text(doc),
      mismatch_text(doc))

print("[10] equal effects with volatile content")
root = source("volatile", {
    "failure-trace.py": py_stub(
        "d = os.path.expanduser('~/.local/state/parity-stub')\n"
        "os.makedirs(d, exist_ok=True)\n"
        "with open(os.path.join(d, payload.get('session_id', 'x') + '.log'), 'a') as fh:\n"
        "    fh.write('%s %d pid=%d home=%s\\n' % (time.strftime('%Y-%m-%dT%H:%M:%SZ',"
        " time.gmtime()), int(time.time()), os.getpid(), os.path.expanduser('~')))\n"
        "    fh.write('at %.6f\\n' % time.time())\n"),
    "agents-remateralize.py": py_stub(
        "import subprocess\n"
        "S = os.path.join(os.environ['HOME'], '.agent-context')\n"
        "with open(os.path.join(S, 'stamp.txt'), 'w') as fh:\n"
        "    fh.write(str(int(time.time())))\n"
        "subprocess.run(['git', '-C', S, 'add', 'stamp.txt'])\n"
        "subprocess.run(['git', '-C', S, '-c', 'user.name=t', '-c', 'user.email=t@t'] + %r\n"
        "               + ['commit', '-qm', 'stub commit'])\n" % SIGN),
})
code, doc, proc = run(root)
check("[10] timestamps, epochs, pids, the fixture path and commit hashes are normalized",
      code == 0, mismatch_text(doc) or tail(proc))

print("[11] a write into a live root")
root = source("leak")
live = os.path.join(root, "live")
put(os.path.join(root, "claude", "hooks", "failure-trace.py"), py_stub(
    "with open(%r, 'a') as fh:\n    fh.write(payload.get('session_id', '') + '\\n')\n"
    % os.path.join(live, "leak.txt")), 0o755)
code, doc, proc = run(root)
check("[13] a live-root write exits 1", code == 1, tail(proc))
check("[11] the live report names the file",
      (doc.get("live") or {}).get("ok") is False and "leak.txt" in json.dumps(doc.get("live")),
      doc.get("live"))

print("[11] a hook that acts outside HOME is neutralized in the fixture")
root = source("neutralized")
live = os.path.join(root, "live")
put(os.path.join(root, "claude", "hooks", "orphan-sweep.sh"),
    "#!/usr/bin/env bash\ncat >/dev/null\necho swept >> %s\nexit 0\n"
    % os.path.join(live, "swept.txt"), 0o755)
code, doc, proc = run(root, events=("SessionStart",))
check("[11] orphan-sweep.sh never runs its real body, so nothing reaches the live root",
      code != 2 and not os.path.exists(os.path.join(live, "swept.txt")), tail(proc))

print("[10] an output field the merge model does not cover")
root = source("unmodeled", {
    "post-edit-verify.py": py_stub("print(json.dumps({'futureField': 1}))\n"),
})
code, doc, proc = run(root)
check("[13] an unmodeled field exits 1", code == 1, tail(proc))
check("[10] the mismatch names futureField", "futureField" in mismatch_text(doc),
      mismatch_text(doc))

print("[10] suppressOutput with nothing left to suppress")



root = source("suppressed", {
    "post-edit-verify.py": py_stub("print(json.dumps({'suppressOutput': True}))\n"),
    "failure-trace.py": py_stub("print(json.dumps({'suppressOutput': True}))\n"),
})
code, doc, proc = run(root)
check("[10] suppressOutput alone is not a mismatch", code == 0,
      mismatch_text(doc) or tail(proc))

print("[8] [9] --payload, --no-builtin and parallel direct hooks")
root = source("parallel", {
    
    
    "warn-long-foreground-command.py": py_stub("time.sleep(1.0)\n"),
    "warn-truncated-search.py": py_stub("time.sleep(1.0)\n"),
})
extra = put(os.path.join(root, "bash.json"), json.dumps({
    "hook_event_name": "PostToolUse", "tool_name": "Bash",
    "tool_input": {"command": "true"}, "tool_response": {"stdout": ""}}))
code, doc, proc = run(root, "--no-builtin", "--payload", extra)
found = payloads(doc)
check("[8] --no-builtin with --payload runs exactly that payload",
      code == 0 and len(found) == 1 and found[0].get("tool") == "Bash", tail(proc))
if len(found) == 1:
    check("[9] the direct side runs its hooks in parallel",
          isinstance(found[0].get("direct_ms"), (int, float)) and found[0]["direct_ms"] < 1800,
          found[0])
    check("[9] the dispatcher runs them one after another",
          isinstance(found[0].get("dispatched_ms"), (int, float))
          and found[0]["dispatched_ms"] >= 1900, found[0])

print("[13] setup faults")
root = source("faults")
code, doc, proc = run(root, "--no-builtin")
check("[13] no payloads exits 2 and says so",
      code == 2 and "no payload" in (proc.stderr or "").lower(), tail(proc))
code, doc, proc = run(root, events=("SubagentStart",))
check("[13] an event with no payload set exits 2 and names it",
      code == 2 and "SubagentStart" in (proc.stderr or ""), tail(proc))
not_a_repo = os.path.join(root, "plain")
os.makedirs(not_a_repo)
proc = subprocess.run([sys.executable, TOOL, "--event", "PostToolUse",
                       "--settings", os.path.join(root, "settings.json"),
                       "--claude-dir", os.path.join(root, "claude"), "--store", not_a_repo,
                       "--live-root", os.path.join(root, "live"), "--runs", "1", "--json"],
                      capture_output=True, text=True, timeout=300)
check("[13] a store that is not a git repository exits 2 and names it",
      proc.returncode == 2 and not_a_repo in (proc.stderr or ""), tail(proc))

print("[7] registry_mismatch")
try:
    tool = load(TOOL, "hook_parity_run")
except Exception as exc:  
    tool = None
    check("[7] hook-parity-run.py imports", False, exc)
if tool is not None:
    a = {"type": "command", "command": "/h/a.sh", "timeout": 10}
    b = {"type": "command", "command": "/h/b.sh"}
    groups = [{"matcher": "Bash", "hooks": [a]}, {"matcher": "*", "hooks": [b]}]
    wired = {"PostToolUse": [{"hooks": [{"type": "command",
                                         "command": "/py /s/hook-dispatch.py PostToolUse",
                                         "timeout": 610}]}]}
    fn = getattr(tool, "registry_mismatch", None)
    same = fn(groups, wired, {"hooks": {"PostToolUse": groups}}, "PostToolUse") if fn else "absent"
    check("[7] identical groups are no mismatch", same is None, same)
    reordered = {"hooks": {"PostToolUse": list(reversed(groups))}}
    swapped = fn(groups, wired, reordered, "PostToolUse") if fn else "absent"
    check("[7] groups in another order are not a mismatch", swapped is None, swapped)
    note_fn = getattr(tool, "registry_order_note", None)
    note = note_fn(groups, wired, reordered, "PostToolUse") if note_fn else None
    check("[7] the order note names the reordered guards",
          isinstance(note, str) and "a.sh" in note and "b.sh" in note, note)
    same_note = (note_fn(groups, wired, {"hooks": {"PostToolUse": groups}}, "PostToolUse")
                 if note_fn else "absent")
    check("[7] the same order has no note", same_note is None, same_note)
    slower = [{"matcher": "Bash", "hooks": [dict(a, timeout=99)]}, groups[1]]
    changed = fn(groups, wired, {"hooks": {"PostToolUse": slower}}, "PostToolUse") if fn else None
    check("[7] a changed timeout is a mismatch", isinstance(changed, str) and changed, changed)
    missing = fn(groups, wired, {"hooks": {}}, "PostToolUse") if fn else None
    check("[7] an event absent from the registry is a mismatch",
          isinstance(missing, str) and missing, missing)

    fx = tool.Fixture(os.path.join(TMP, "no-fixture"))
    quiet = {"name": "post-edit-verify.py", "command": "/py /h/post-edit-verify.py", "code": 0,
             "out": '{"suppressOutput": true}\n', "err": "", "fault": None}

    def dispatcher(out):
        return {"name": "hook-dispatch.py", "command": "/py /s/hook-dispatch.py PostToolUse",
                "code": 0, "out": out, "err": "", "fault": None}

    silent = tool.compare(fx, "PostToolUse", [quiet], [dispatcher("")])
    check("[10] a dropped suppressOutput with no dispatcher output is no mismatch",
          silent == [], silent)
    loud = tool.compare(fx, "PostToolUse", [quiet], [dispatcher('{"systemMessage": "x"}\n')])
    check("[10] dispatcher output where every hook asked to suppress is a mismatch",
          any("suppress" in m for m in loud), loud)

shutil.rmtree(TMP, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
