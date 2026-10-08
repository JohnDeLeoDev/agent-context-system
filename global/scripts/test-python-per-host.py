#!/usr/bin/env python3
'Battery for consolidation Phase 2: one Python per host.\n\nPlan: get_doc("consolidation/plan.md"). What it pins:\n\nInterpreter names in fixture text are built by concatenation, so invariant\ninterpreter-is-rendered does not report this file for its own fixtures. The worktree\nfixtures live under ~/.cache/hook-test-fixtures, outside every temp root the hook exempts.\n\nUsage: test-python-per-host.py'

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "require-worktree-edit.py")
PY3 = "pyth" + "on3"
PY = "pyth" + "on"
INTERP_WORD = re.compile(r"^(ba)?sh$|^" + PY + r"(\d+(\.\d+)?)?$")
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

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


def load(name, filename):
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def clip(text, size=300):
    return text if len(text) <= size else "..." + text[-size:]


def script_of(command):
    'The script a hook command runs, past any interpreter word.'
    words = command.split()
    while len(words) > 1 and INTERP_WORD.match(os.path.basename(words[0])):
        words = words[1:]
    return words[0] if words else ""




def check_resolver():
    print("[1] agent-python.py picks one interpreter per host")
    try:
        ap = load("agent_python", "agent-python.py")
    except Exception as exc:
        check("agent-python.py loads", False, "%s: %s" % (type(exc).__name__, exc))
        ap = None
    home = "/home/j"
    brew = "/opt/homebrew/bin/" + PY3 + ".14"
    uv = home + "/.local/bin/" + PY3 + ".14"
    running = "/usr/bin/" + PY3
    override = {"AGENT_CONTEXT_PYTHON": "/x/bin/py"}
    cases = (
        ("macOS with Homebrew 3.14", "darwin", {}, (brew, uv), brew),
        ("macOS ignores ~/.local/bin and falls back to the running interpreter", "darwin", {},
         (uv,), running),
        ("Linux with uv's 3.14", "linux", {}, (brew, uv), uv),
        ("Linux without it falls back to the running interpreter", "linux", {}, (brew,), running),
        ("AGENT_CONTEXT_PYTHON wins on macOS", "darwin", override, (brew, uv), "/x/bin/py"),
        ("AGENT_CONTEXT_PYTHON wins on Linux", "linux", override, (brew, uv), "/x/bin/py"),
        ("an empty AGENT_CONTEXT_PYTHON is ignored", "linux", {"AGENT_CONTEXT_PYTHON": ""},
         (uv,), uv),
    )
    for label, platform, env, present, want in cases:
        if ap is None:
            check(label, False, "agent-python.py did not load")
            continue
        try:
            got = ap.interpreter(platform=platform, home=home, env=env, executable=running,
                                 exists=lambda p, present=present: p in present)
        except Exception as exc:
            check(label, False, "%s: %s" % (type(exc).__name__, exc))
            continue
        check(label, got == want, "got %r" % (got,))

    cli = os.path.join(HERE, "agent-python.py")
    r = subprocess.run([sys.executable, cli], capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, AGENT_CONTEXT_PYTHON="/x/bin/py"))
    check("the CLI prints the override", r.returncode == 0 and r.stdout.strip() == "/x/bin/py",
          "rc %d, stdout %r, stderr %s" % (r.returncode, r.stdout, clip(r.stderr)))
    env = dict(os.environ)
    env.pop("AGENT_CONTEXT_PYTHON", None)
    r = subprocess.run([sys.executable, cli], capture_output=True, text=True, timeout=30, env=env)
    path = r.stdout.strip()
    check("without an override the CLI prints an executable absolute path",
          r.returncode == 0 and os.path.isabs(path) and os.access(path, os.X_OK),
          "rc %d, stdout %r" % (r.returncode, r.stdout))




def hook_commands(settings):
    out = []
    for groups in (settings.get("hooks") or {}).values():
        for group in groups or []:
            for hook in group.get("hooks") or []:
                out.append(hook.get("command") or "")
    return out


def sync(target, env):
    return subprocess.run([sys.executable, os.path.join(HERE, "home-settings-sync.py"), target],
                          capture_output=True, text=True, timeout=120, env=env)


def check_settings_sync():
    print("[2] home-settings-sync runs .py hooks through the interpreter")
    root = tempfile.mkdtemp(prefix="python-per-host-test-")
    interp = "/fake/bin/" + PY3 + ".14"
    env = dict(os.environ, AGENT_CONTEXT_PYTHON=interp)
    try:
        target = os.path.join(root, "settings.json")
        put(target, "{}")
        r = sync(target, env)
        check("a sync of an empty settings.json exits 0", r.returncode == 0, clip(r.stderr))
        if r.returncode != 0:
            return
        first = read(target)
        commands = hook_commands(json.loads(first))
        py = [c for c in commands if script_of(c).endswith(".py")]
        sh = [c for c in commands if script_of(c).endswith(".sh")]
        check("the sync wires .py hooks", bool(py))
        if not py:
            return
        
        
        
        bad = [c for c in py if not (c.split()[0] == interp and len(c.split()) >= 2
                                     and os.path.isabs(c.split()[1]))]
        check("every .py hook is `<interpreter> <absolute path>`, arguments allowed", not bad,
              "e.g. %r" % bad[:2])
        
        
        check("no hook runs a shell script", not sh, "e.g. %r" % sh[:2])
        again = sync(target, env)
        check("a second sync changes nothing", again.returncode == 0 and read(target) == first)

        sample = py[0]
        path = script_of(sample)
        wanted = sum(1 for c in commands if script_of(c) == path)
        user_hook = PY3 + " /opt/user/own-hook.py"
        for label, old in (("a bare .py entry", path),
                           ("an entry behind another interpreter",
                            "/old/bin/" + PY3 + ".13 " + path)):
            settings = json.loads(first)
            for groups in settings["hooks"].values():
                for group in groups:
                    for hook in group.get("hooks") or []:
                        if hook.get("command") == sample:
                            hook["command"] = old
            settings["hooks"].setdefault("PreToolUse", []).append(
                {"matcher": "OwnMatcher", "hooks": [{"type": "command", "command": user_hook}]})
            put(target, json.dumps(settings, indent=2))
            r = sync(target, env)
            after = hook_commands(json.loads(read(target))) if r.returncode == 0 else []
            count = sum(1 for c in after if script_of(c) == path)
            check(label + " is replaced, not duplicated",
                  r.returncode == 0 and old not in after and count == wanted,
                  "rc %d, %d entries for %s where %d were wired"
                  % (r.returncode, count, os.path.basename(path), wanted))
            check(label + ": an unmanaged user hook is kept once", after.count(user_hook) == 1)
    finally:
        shutil.rmtree(root, ignore_errors=True)




def check_readers():
    print("[3] hook-command readers see past an absolute versioned interpreter")
    brew = "/opt/homebrew/bin/" + PY3 + ".14 /h/.claude/hooks/x.py"
    uv = "/home/j/.local/bin/" + PY3 + ".14 /h/.claude/hooks/x.py"
    hss = load("home_settings_sync", "home-settings-sync.py")
    for label, command, want in (
            ("home-settings-sync: a Homebrew interpreter", brew, "x.py"),
            ("home-settings-sync: a uv interpreter", uv, "x.py"),
            ("home-settings-sync: bare " + PY3 + " is still recognized", PY3 + " /h/x.py", "x.py"),
            ("home-settings-sync: bash is still recognized", "bash /h/x.sh", "x.sh"),
            ("home-settings-sync: a bare path is still recognized", "/h/x.sh", "x.sh")):
        got = hss.command_basename(command)
        check(label, got == want, "got %r" % (got,))
    probe = load("hook_registration_probe", "hook-registration-probe.py")
    for label, command, want in (
            ("hook-registration-probe: a Homebrew interpreter", brew, "/h/.claude/hooks/x.py"),
            ("hook-registration-probe: a uv interpreter", uv, "/h/.claude/hooks/x.py"),
            ("hook-registration-probe: bash is still recognized", "bash /h/x.sh", "/h/x.sh")):
        got = probe.script_word(command)
        check(label, got == want, "got %r" % (got,))




def check_invariant():
    print("[4] invariant interpreter-is-rendered")
    tmp = tempfile.mkdtemp(prefix="python-per-host-inv-")
    store = os.path.join(tmp, "store")
    scripts = os.path.join(store, "global", "scripts")
    os.makedirs(scripts)
    os.makedirs(os.path.join(store, "global", "hooks"))
    saved = os.environ.get("AGENT_CONTEXT_STORE")
    os.environ["AGENT_CONTEXT_STORE"] = store
    try:
        mod = load("invariant_check", "invariant-check.py")
        inv = next((i for i in mod.REGISTRY if i.id == "interpreter-is-rendered"), None)
        check("interpreter-is-rendered is registered", inv is not None)
        if inv is None:
            return
        cases = (
            ("a list command starting with bare " + PY3,
             'subprocess.run(["' + PY3 + '", path])\n', True),
            ("a list command starting with bare " + PY,
             "subprocess.run(['" + PY + "', path])\n", True),
            ("an f-string command starting with " + PY3, 'cmd = f"' + PY3 + ' {mat}"\n', True),
            ("a command string starting with " + PY3,
             'hook = {"command": "' + PY3 + ' -m tool"}\n', True),
            ("sys.executable", "subprocess.run([sys.executable, path])\n", False),
            ("a tuple of interpreter names",
             '_NAMES = ("bash", "sh", "' + PY3 + '", "' + PY + '")\n', False),
            ("a comment", "# run it with " + PY3 + " foo.py\n", False),
            ("a docstring", 'def f():\n    """Run as `' + PY3 + ' foo.py`."""\n', False),
            ("a message that mentions it mid-string", 'print("needs ' + PY3 + ' 3.14")\n', False),
        )
        for n, (label, src, want) in enumerate(cases):
            path = os.path.join(scripts, "fixture-%d.py" % n)
            put(path, src)
            try:
                got = bool(inv.violated(path, mod._read(path)))
            except Exception as exc:
                check(label, False, "raised %s: %s" % (type(exc).__name__, exc))
                continue
            check(label + (" is reported" if want else " passes"), got == want,
                  "reported %r" % (got,))
        check("eval-cases.py is allowlisted (its commands run inside eval sandboxes)",
              "eval-cases.py" in inv.allow)
    finally:
        if saved is None:
            os.environ.pop("AGENT_CONTEXT_STORE", None)
        else:
            os.environ["AGENT_CONTEXT_STORE"] = saved
        shutil.rmtree(tmp, ignore_errors=True)




def cksum(text):
    return subprocess.run(["cksum"], input=text, capture_output=True, text=True).stdout.split()[0]


def git(*args):
    subprocess.run(["git"] + list(args), check=True, capture_output=True, env=GIT_ENV)


def run_hook(payload, env):
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True,
                          text=True, timeout=60, env=env)


def redirected_to(r):
    'The rewritten file_path when the hook allowed and moved the edit, else None.'
    try:
        out = json.loads(r.stdout)
    except ValueError:
        return None
    spec = out.get("hookSpecificOutput") or {} if isinstance(out, dict) else {}
    if spec.get("permissionDecision") != "allow":
        return None
    return (spec.get("updatedInput") or {}).get("file_path")


def described(r):
    return "rc %d, stdout %s, stderr %s" % (r.returncode, clip(r.stdout, 200), clip(r.stderr, 200))


def check_worktree_symlink():
    print("[5] require-worktree-edit redirects through a symlinked path")
    base = os.path.join(os.path.expanduser("~"), ".cache", "hook-test-fixtures")
    os.makedirs(base, exist_ok=True)
    root = tempfile.mkdtemp(prefix="python-per-host-wt-", dir=base)
    try:
        real = os.path.join(root, "real")
        link = os.path.join(root, "link")
        proj = os.path.join(real, "proj")
        os.makedirs(proj)
        os.symlink(real, link)
        put(os.path.join(proj, "a.py"), "v1\n")
        git("-C", proj, "init", "-q")
        git("-C", proj, "config", "user.email", "t@t")
        git("-C", proj, "config", "user.name", "t")
        git("-C", proj, "add", "-A")
        git("-C", proj, "commit", "-qm", "one")
        git("-C", proj, "worktree", "add", "-q",
            os.path.join(proj, ".claude", "worktrees", "fix-x"), "-b", "fix-x")
        sid = "wt-symlink-%d" % os.getpid()
        state = os.path.join(root, "state")
        put(os.path.join(state, "claims", sid + ".json"),
            json.dumps({"session": sid, "worktree": "fix-x"}))
        home = os.path.join(root, "home")
        os.makedirs(home)
        env = dict(GIT_ENV, AGENT_CONTEXT_STATE_DIR=state, HOME=home,
                   TMPDIR=os.path.join(root, "not-a-temp-root"))

        linked = os.path.join(link, "proj")
        wt_real = os.path.join(proj, ".claude", "worktrees", "fix-x")
        wt_link = os.path.join(linked, ".claude", "worktrees", "fix-x")

        def payload(tool, file_path, cwd):
            tool_input = {"file_path": file_path}
            if tool == "Write":
                tool_input["content"] = "x"
            else:
                tool_input.update(old_string="v1", new_string="v2")
            return {"tool_name": tool, "session_id": sid, "cwd": cwd, "tool_input": tool_input}

        r = run_hook(payload("Write", os.path.join(proj, "src", "new.py"), proj), env)
        check("unchanged: a Write through the resolved path is moved to the worktree",
              redirected_to(r) == os.path.join(wt_real, "src", "new.py"), described(r))

        r = run_hook(payload("Write", os.path.join(linked, "src", "new.py"), linked), env)
        check("a Write through the symlink is moved to the worktree, spelled through the symlink",
              redirected_to(r) == os.path.join(wt_link, "src", "new.py"), described(r))

        r = run_hook(payload("Edit", os.path.join(linked, "a.py"), linked), env)
        check("an Edit through the symlink whose worktree copy was never read is refused, "
              "naming that copy",
              r.returncode == 2 and os.path.join(wt_link, "a.py") in r.stderr
              and "Read THAT copy" in r.stderr, described(r))

        ledger = os.path.join(home, ".local", "state", "agent-context", "read-ledger", sid)
        key = cksum(os.path.join(linked, "a.py"))
        sig = cksum(os.path.join(wt_link, "a.py"))
        put(os.path.join(ledger, key), "")
        put(os.path.join(ledger, key + ".sig." + sig), "")
        r = run_hook(payload("Edit", os.path.join(linked, "a.py"), linked), env)
        check("an Edit through the symlink whose worktree copy was read is moved there",
              redirected_to(r) == os.path.join(wt_link, "a.py"), described(r))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    for section in (check_resolver, check_settings_sync, check_readers, check_invariant,
                    check_worktree_symlink):
        try:
            section()
        except Exception as exc:
            check(section.__name__ + " ran to the end", False,
                  "raised %s: %s" % (type(exc).__name__, exc))
    print("\n%d passed, %d failed" % (passed, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
