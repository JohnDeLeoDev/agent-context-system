#!/usr/bin/env python3
'Tests for deps-check.py (fleet dependency management, phase A).\n\nFixture manifests use invented tool names, so a tool that happens to be installed on the host\nrunning the tests cannot change a result. Fake tools are small sh scripts in a directory under\n~/.cache, because /tmp is mounted noexec on the Synology nodes.\n\nSet DEPS_TEST_MANIFEST to check a manifest that is not yet at global/deps/manifest.toml.'

import ast
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "deps-check.py")
REAL_MANIFEST = os.environ.get("DEPS_TEST_MANIFEST") or os.path.normpath(
    os.path.join(HERE, "..", "deps", "manifest.toml"))

_spec = importlib.util.spec_from_file_location("deps_check_under_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
deps: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deps)

BASE = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-tests")
os.makedirs(BASE, exist_ok=True)
ROOT = tempfile.mkdtemp(dir=BASE, prefix="deps-check-")
BIN = os.path.join(ROOT, "bin")
os.makedirs(BIN)

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        failures.append("%s %s" % (label, detail))
        print("FAIL:", label, detail)


def tool(name, body):
    path = os.path.join(BIN, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


def new_home():
    home = tempfile.mkdtemp(dir=ROOT, prefix="home-")
    return home


def env_for(home, path=BIN):
    return {"HOME": home, "PATH": path + os.pathsep + "/usr/bin:/bin"}


def manifest_text(machines=None, roles=None, tools=None):
    machines = machines or {"box": ("ubuntu", ["base"])}
    roles = roles or {"base": ["fake-a"]}
    tools = tools or {"fake-a": 'check = ["fake-a", "--version"]\nchannel = "manual"\nfix = "install fake-a"'}
    out = ["schema = 1"]
    for mid, (os_id, role_list) in machines.items():
        out.append('[machine.%s]\nos = "%s"\nroles = [%s]' % (
            mid, os_id, ", ".join('"%s"' % r for r in role_list)))
    for role, names in roles.items():
        out.append('[role.%s]\ntools = [%s]' % (role, ", ".join('"%s"' % n for n in names)))
    for name, body in tools.items():
        out.append("[tool.%s]\n%s" % (name, body))
    return "\n".join(out) + "\n"


def write_manifest(text):
    path = os.path.join(ROOT, "manifest-%d.toml" % time.monotonic_ns())
    with open(path, "w") as fh:
        fh.write(text)
    return path


def run(argv, env, expect_stderr=False):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = deps.main(argv, env=env)
    return code, out.getvalue(), err.getvalue()


def quick(text, home=None, extra=(), path=BIN, machine="box"):
    home = home or new_home()
    manifest = write_manifest(text)
    state = os.path.join(home, "state")
    code, out, err = run(["--manifest", manifest, "--machine", machine, "--state-dir", state,
                          "--timeout", "3", "--no-record"] + list(extra), env_for(home, path))
    return code, out, err, home




for text, want in (("git version 2.54.0 (Apple Git-157)", (2, 54, 0)), ("jq-1.8.2", (1, 8, 2)),
                   ("Version 7.0.0-dev.20260707.2", (7, 0, 0)), ("Python 3.14.7", (3, 14, 7)),
                   ("usage: xcode-build-server", None), ("", None), ("v12", None)):
    check("parse_version %r" % text, deps.parse_version(text) == want, str(deps.parse_version(text)))
check("versions compare as numbers, not text",
      deps.parse_version("1.10.0") > deps.parse_version("1.9.0"))



tool("fake-a", 'echo "fake-a version 2.5.1"')
code, out, err, home = quick(manifest_text())
check("all present: exit 0", code == 0, out + err)
check("all present: one summary line", out.strip() == "deps-check: box: all 1 tools ok", out)

code, out, err, home = quick(manifest_text(tools={
    "fake-missing": 'check = ["fake-missing", "--version"]\nchannel = "manual"\nfix = "install fake-missing please"'},
    roles={"base": ["fake-missing"]}))
check("missing: exit 3", code == 3, out)
check("missing: line names tool, machine, role and fix",
      "fake-missing missing on box (role base): install fake-missing please" in out, out)

tool("fake-old", 'echo "tool 1.2.0"')
code, out, err, home = quick(manifest_text(tools={
    "fake-old": 'check = ["fake-old", "--version"]\nfloor = "2.0.0"\nchannel = "manual"\nfix = "upgrade it"'},
    roles={"base": ["fake-old"]}))
check("below floor: exit 3", code == 3, out)
check("below floor: found and required versions shown", "fake-old 1.2.0 is below 2.0.0 on box" in out, out)

tool("fake-noversion", 'echo "usage: fake-noversion [options]"')
code, out, err, home = quick(manifest_text(tools={
    "fake-noversion": 'check = ["fake-noversion", "--version"]\nfloor = "2.0.0"\nchannel = "manual"\nfix = "x"'},
    roles={"base": ["fake-noversion"]}))
check("unparseable version is a note, never a failure", code == 0 and "note: fake-noversion version" in out,
      out)

tool("fake-broken", 'echo "roslyn-language-server: not found in %s/.dotnet" >&2; exit 127' % ROOT)
code, out, err, home = quick(manifest_text(tools={
    "fake-broken": 'check = ["fake-broken", "--version"]\nchannel = "manual"\nfix = "reinstall"'},
    roles={"base": ["fake-broken"]}))
check("broken: on PATH but exits nonzero, exit 3", code == 3 and "fake-broken broken on box" in out, out)
check("broken: the exit code is in the line", "exit 127" in out, out)
check("broken: a home path in the tool's own output is redacted", ROOT not in out and "~" in out, out)

tool("fake-slow", "sleep 5")
began = time.monotonic()
code, out, err, home = quick(manifest_text(tools={
    "fake-slow": 'check = ["fake-slow", "--version"]\nchannel = "manual"\nfix = "x"'},
    roles={"base": ["fake-slow"]}), extra=["--timeout", "0.4"])
check("timeout: reported broken", code == 3 and "timed out after 0.4s" in out, out)
check("timeout: the run does not wait out the command", time.monotonic() - began < 3.5)

marker = os.path.join(ROOT, "exists-only-ran")
tool("fake-exists", 'touch "%s"' % marker)
code, out, err, home = quick(manifest_text(tools={
    "fake-exists": 'check = ["fake-exists"]\nexists_only = true\nchannel = "manual"\nfix = "x"'},
    roles={"base": ["fake-exists"]}))
check("exists_only: found on PATH is ok and the tool is never run", code == 0 and not os.path.exists(marker), out)

tool("fake-list", 'echo "other-thing 1.0"')
listing = manifest_text(tools={"fake-list": 'check = ["fake-list"]\nexpect = "agent-context"\n'
                               'presence_only = true\nchannel = "manual"\nfix = "install it"'},
                        roles={"base": ["fake-list"]})
code, out, err, home = quick(listing)
check("expect: absent text is missing", code == 3 and "fake-list missing" in out, out)
tool("fake-list", 'echo "agent-context v0.1.0"')
code, out, err, home = quick(listing)
check("expect: present text is ok", code == 0, out)



tool("fake-mac", 'echo "1.0.0"')
multi = manifest_text(
    machines={"mac1": ("mac", ["base", "lsp"]), "lin1": ("ubuntu", ["base"])},
    roles={"base": ["fake-a"], "lsp": ["fake-mac"]},
    tools={"fake-a": 'check = ["fake-a", "--version"]\nchannel = "manual"\nfix = "x"',
           "fake-mac": 'check = ["fake-mac", "--version"]\nchannel = "manual"\nfix = "x"\nos = ["mac"]'})
code, out, err, home = quick(multi, extra=["--json"], machine="mac1")
check("role set: the mac machine checks both tools", sorted(json.loads(out)["tools"]) == ["fake-a", "fake-mac"], out)
code, out, err, home = quick(multi, extra=["--json"], machine="lin1")
check("role set: the linux machine checks only its role", sorted(json.loads(out)["tools"]) == ["fake-a"], out)

bad_role = multi.replace('[machine.lin1]\nos = "ubuntu"\nroles = ["base"]',
                         '[machine.lin1]\nos = "ubuntu"\nroles = ["base", "lsp"]')
code, out, err, home = quick(bad_role, machine="lin1")
check("role mismatch is an error naming the machine id",
      code == 2 and "machine lin1: role lsp lists fake-mac, which does not apply to os ubuntu" in err, err)

code, out, err, home = quick(multi, machine="nobody")
check("unknown machine id: exit 2 and the id is named", code == 2 and "'nobody'" in err, err)

home = new_home()
os.makedirs(os.path.join(home, ".config", "chezmoi"))
with open(os.path.join(home, ".config", "chezmoi", "chezmoi.toml"), "w") as fh:
    fh.write('[data]\n    machine_id = "lin1"\n')
manifest = write_manifest(multi)
code, out, err = run(["--manifest", manifest, "--state-dir", os.path.join(home, "s"), "--no-record"],
                     env_for(home))
check("machine id defaults to chezmoi's machine_id", code == 0 and "lin1" in out, out + err)
code, out, err = run(["--manifest", manifest, "--state-dir", os.path.join(home, "s"), "--no-record"],
                     env_for(new_home()))
check("no machine id anywhere: exit 2", code == 2 and "machine id unknown" in err, err)



def errors_of(text):
    return deps.validate(deps.tomllib.loads(text))


base_tool = 'check = ["fake-a"]\nchannel = "manual"\nfix = "x"'
cases = {
    "unknown key": (manifest_text(tools={"fake-a": base_tool + "\nflor = \"1.0\""}), "unknown key flor"),
    "unknown channel": (manifest_text(tools={"fake-a": base_tool.replace("manual", "curl-pipe")}), "channel"),
    "missing fix": (manifest_text(tools={"fake-a": 'check = ["fake-a"]\nchannel = "manual"'}), "fix is missing"),
    "role names an undefined tool": (manifest_text(roles={"base": ["ghost"]}), "tool ghost is not defined"),
    "machine names an undefined role": (manifest_text(machines={"box": ("ubuntu", ["ghost"])}),
                                        "role ghost is not defined"),
    "relay tool not verify_only": (manifest_text(roles={"relay": ["fake-a"]},
                                                 machines={"box": ("ubuntu", ["relay"])},
                                                 tools={"fake-a": base_tool}), "must be verify_only"),
    "relay-update channel not verify_only": (manifest_text(
        tools={"fake-a": base_tool.replace("manual", "relay-update")}), "must be verify_only"),
    "command with shell syntax": (manifest_text(tools={"fake-a": 'check = ["sh -c x; y"]\nchannel = "manual"\nfix = "x"'}),
                                  "not a plain command name"),
    "bad floor": (manifest_text(tools={"fake-a": base_tool + '\nfloor = "latest"'}), "floor"),
    "bad os": (manifest_text(tools={"fake-a": base_tool + '\nos = ["amiga"]'}), "os must be"),
    "bad machine os": (manifest_text(machines={"box": ("amiga", ["base"])}), "os must be one of"),
    "wrong schema": ("schema = 2\n", "schema must be 1"),
}
for label, (text, needle) in cases.items():
    got = errors_of(text)
    check("validate rejects: " + label, any(needle in line for line in got), str(got))
check("validate accepts the fixture manifest", errors_of(manifest_text()) == [])

code, out, err, home = quick(manifest_text(tools={"fake-a": base_tool + "\nflor = 1"}))
check("an invalid manifest exits 2 and names the problem", code == 2 and "unknown key flor" in err, err)
missing_path = os.path.join(ROOT, "no-such-manifest.toml")
code, out, err = run(["--manifest", missing_path, "--machine", "box", "--no-record"], env_for(new_home()))
check("a missing manifest exits 2 and names the file", code == 2 and "no-such-manifest.toml" in err, err)
garbage = write_manifest("this is = = not toml [")
code, out, err = run(["--manifest", garbage, "--machine", "box", "--no-record"], env_for(new_home()))
check("a manifest that is not TOML exits 2", code == 2 and "not valid TOML" in err, err)



saved = deps.tomllib
deps.tomllib = None
code, out, err = run(["--manifest", write_manifest(manifest_text()), "--machine", "box", "--no-record"],
                     env_for(new_home()))
deps.tomllib = saved
check("no tomllib: exit 3 with the fix and no traceback",
      code == 3 and "tomllib" in err and "uv python install 3.14" in err and "Traceback" not in err, err)
with open(SCRIPT) as fh:
    source = fh.read()
try:
    ast.parse(source, feature_version=(3, 8))
    parsed = True
except SyntaxError as exc:
    parsed = False
    print(exc)
check("the script parses as Python 3.8", parsed)



fix_marker = os.path.join(ROOT, "fix-ran")
tool("fake-present", 'echo "1.0.0"')
danger = manifest_text(
    roles={"base": ["fake-gone", "fake-present"]},
    tools={"fake-gone": 'check = ["fake-gone"]\nchannel = "manual"\nfix = "touch %s"' % fix_marker,
           "fake-present": 'check = ["fake-present", "--version"]\nchannel = "manual"\nfix = "touch %s"' % fix_marker})
calls = []
real_run = deps.subprocess.run


def recording_run(argv, *args, **kwargs):
    calls.append((list(argv), dict(kwargs)))
    return real_run(argv, *args, **kwargs)


deps.subprocess.run = recording_run
try:
    home = new_home()
    manifest = write_manifest(danger)
    code, out, err = run(["--manifest", manifest, "--machine", "box", "--state-dir", os.path.join(home, "s")],
                         env_for(home))
finally:
    deps.subprocess.run = real_run
check("a fix string is never run", not os.path.exists(fix_marker) and "touch" in out, out)
allowed = {os.path.join(BIN, "fake-present"), sys.executable}
check("every command is a resolved check or the recorder",
      all(argv[0] in allowed for argv, _ in calls), str([c[0] for c in calls]))
check("no call uses a shell and every call is bounded",
      all(kw.get("shell") is False and kw.get("timeout") for _, kw in calls), str(calls))
check("the check argv is exactly the manifest's", ([os.path.join(BIN, "fake-present"), "--version"]
                                                  in [argv for argv, _ in calls]))



tool("fake-a", 'echo "fake-a 3.0.0"')
home = new_home()
manifest = write_manifest(manifest_text())
state = os.path.join(home, "state")
code, out, err = run(["--manifest", manifest, "--machine", "box", "--state-dir", state], env_for(home))
with open(os.path.join(state, "deps.json")) as fh:
    doc = json.load(fh)
check("clean run writes deps.json with a clean fleet block",
      code == 0 and doc["fleet"] == {"ok": True, "missing": [], "broken": [], "below_floor": []}, str(doc))
check("the report carries its time", abs(doc["at"] - time.time()) < 60)

os.remove(os.path.join(BIN, "fake-a"))
health = os.path.join(home, ".local", "state", "agent-context", "health", "deps.json")
code, out, err = run(["--manifest", manifest, "--machine", "box", "--state-dir", state], env_for(home))
with open(os.path.join(state, "deps.json")) as fh:
    doc = json.load(fh)
check("a missing tool lands in the fleet block", doc["fleet"]["missing"] == ["fake-a"] and not doc["ok"], str(doc))
check("a problem is recorded for the session-start banner",
      os.path.isfile(health) and "fake-a missing on box (role base): install fake-a" in open(health).read())
tool("fake-a", 'echo "fake-a 3.0.0"')
code, out, err = run(["--manifest", manifest, "--machine", "box", "--state-dir", state], env_for(home))
check("a clean run clears the recorded problem", code == 0 and not os.path.exists(health))

blocker = os.path.join(home, "state-is-a-file")
with open(blocker, "w") as fh:
    fh.write("x")
code, out, err = run(["--manifest", manifest, "--machine", "box", "--state-dir", blocker], env_for(home))
check("an unwritable state directory reports it and exits 3", code == 3 and "state unwritable" in out, out + err)

lock_dir = os.path.join(home, "lock-state")
os.makedirs(lock_dir)
held = open(os.path.join(lock_dir, "deps-check.lock"), "w")
fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
code, out, err = run(["--manifest", manifest, "--machine", "box", "--state-dir", lock_dir], env_for(home))
check("a second run while the lock is held exits 4", code == 4 and "busy" in err, err)
held.close()
code, out, err = run(["--manifest", manifest, "--machine", "box", "--state-dir", lock_dir], env_for(home))
check("the lock is released after a run", code == 0, out + err)



tool("fake-shim", 'echo "shim: fake-target: not found" >&2; exit 127')
shim_manifest = manifest_text(
    machines={"lin1": ("ubuntu", ["base"])},
    tools={"fake-a": 'check = ["fake-a"]\nchannel = "manual"\nfix = "x"',
           "fake-shim": 'check = ["fake-shim"]\nchannel = "manual"\nfix = "x"\nshim = true'},
    roles={"base": ["fake-a"], "extra": ["fake-shim"]})
code, out, err, home = quick(shim_manifest, extra=["--json"], machine="lin1")
report = json.loads(out)
check("a broken launcher on a machine that does not carry it is unexpected, not a failure",
      code == 0 and report["unexpected"] == ["fake-shim"] and report["ok"], out)
tool("fake-shim", "exit 0")
code, out, err, home = quick(shim_manifest, extra=["--json"], machine="lin1")
check("a working launcher is not flagged", json.loads(out)["unexpected"] == [], out)



check("redact shortens the home directory", deps.redact("at /home/alice/x and /Users/bob/y", "/nope")
      == "at ~/x and ~/y")
check("redact handles a Synology home", deps.redact("/var/services/homes/user/.local", "") == "~/.local")
check("redact uses the real home first", deps.redact("/opt/h/user/bin", "/opt/h/user") == "~/bin")



odd = {
    "role tools is a number": '[role.r]\ntools = 5\n[machine.box]\nos = "ubuntu"\nroles = ["r"]\n',
    "role tool name is a list": '[role.r]\ntools = [["x"]]\n[machine.box]\nos = "ubuntu"\nroles = ["r"]\n',
    "machine role is a list": '[role.r]\ntools = ["fake-a"]\n[machine.box]\nos = "ubuntu"\nroles = [["r"]]\n',
    "no machine or role table": "",
}
for label, extra in odd.items():
    text = 'schema = 1\n[tool.fake-a]\ncheck = ["fake-a"]\nchannel = "manual"\nfix = "x"\n' + extra
    try:
        got = deps.validate(deps.tomllib.loads(text))
        crashed = ""
    except Exception as exc:
        got, crashed = [], repr(exc)
    check("validate reports, not raises: " + label, bool(got) and not crashed, crashed or str(got))
    code, out, err, home = quick(text)
    check("main exits 2 with no traceback: " + label, code == 2 and "Traceback" not in err, err)

check("schema = true is not schema 1", deps.validate({"schema": True}) == ["schema must be 1"])



def lint_manifest(extra=""):
    return manifest_text(
        tools={"fake-lint": 'check_any = [["fake-lint", "--version"], '
                            '["fake-runner", "--offline", "fake-lint", "--version"]]\n'
                            'floor = "1.5.0"\nchannel = "uv-tool"\nfix = "install fake-lint"' + extra},
        roles={"base": ["fake-lint"]})


def lint_status(text, extra=()):
    code, out, err, home = quick(text, extra=["--json"] + list(extra))
    report = json.loads(out) if out.strip().startswith("{") else None
    return code, (report["tools"]["fake-lint"] if report else None), home, out + err


def drop(*names):
    for n in names:
        try:
            os.remove(os.path.join(BIN, n))
        except OSError:
            pass


runner_log = os.path.join(ROOT, "runner.log")
runner_cwd = os.path.join(ROOT, "runner.cwd")
tool("fake-lint", 'echo "fake-lint 1.6.0"')
tool("fake-runner", 'echo "$@" >> "%s"; pwd > "%s"; echo "fake-lint 1.6.0"' % (runner_log, runner_cwd))
code, res, home, text = lint_status(lint_manifest())
check("check_any: the command on PATH decides", res and res["status"] == "ok" and res["found"] == "1.6.0", text)
check("check_any: the fallback is not run when the first command is found", not os.path.exists(runner_log))

tool("fake-lint", 'echo "fake-lint 1.2.0"')
code, res, home, text = lint_status(lint_manifest())
check("check_any: an old command on PATH is below the floor even when the fallback would pass",
      res and res["status"] == "below_floor" and res["found"] == "1.2.0" and code == 3, text)

tool("fake-lint", 'echo "fake-lint: broken install" >&2; exit 3')
code, res, home, text = lint_status(lint_manifest())
check("check_any: a broken command on PATH is broken and does not fall through",
      res and res["status"] == "broken" and not os.path.exists(runner_log), text)

drop("fake-lint")
code, res, home, text = lint_status(lint_manifest())
check("check_any: with the first command absent the fallback decides", res and res["status"] == "ok", text)
check("check_any: the fallback runs with the arguments the manifest names",
      os.path.exists(runner_log) and "--offline fake-lint --version" in open(runner_log).read())
check("check_any: the fallback runs from the home directory",
      os.path.realpath(open(runner_cwd).read().strip()) == os.path.realpath(home), open(runner_cwd).read())

tool("fake-runner", 'echo "error: not in the cache" >&2; exit 1')
code, res, home, text = lint_status(lint_manifest())
check("check_any: an emptied cache is broken and names the fix",
      code == 3 and res and res["status"] == "broken" and "install fake-lint" in text, text)

drop("fake-runner")
code, res, home, text = lint_status(lint_manifest())
check("check_any: with every command absent the tool is missing",
      code == 3 and res and res["status"] == "missing" and "fake-lint missing on box" in text, text)

tool("fake-lint", 'sleep 1; echo "fake-lint 1.6.0"')
code, res, home, text = lint_status(lint_manifest("\ntimeout = 5"), extra=["--timeout", "0.2"])
check("a tool's own timeout replaces the global one", res and res["status"] == "ok", text)
tool("fake-lint", "sleep 4")
code, res, home, text = lint_status(lint_manifest("\ntimeout = 0.3"))
check("a tool's own timeout can be short", res and res["status"] == "broken" and "timed out after 0.3s" in text, text)
drop("fake-lint")

both = 'check = ["fake-a"]\ncheck_any = [["fake-a"]]\nchannel = "manual"\nfix = "x"'
odd_any = {
    "check and check_any together": (both, "exactly one of check and check_any"),
    "neither check nor check_any": ('channel = "manual"\nfix = "x"', "exactly one of check and check_any"),
    "check_any is not a list": ('check_any = "fake-a"\nchannel = "manual"\nfix = "x"', "non-empty list of commands"),
    "check_any is empty": ('check_any = []\nchannel = "manual"\nfix = "x"', "non-empty list of commands"),
    "an alternative with shell syntax": ('check_any = [["fake-a"], ["sh -c x; y"]]\nchannel = "manual"\nfix = "x"',
                                          "check_any[1][0]"),
    "an alternative that is not a list": ('check_any = [["fake-a"], "fake-b"]\nchannel = "manual"\nfix = "x"',
                                           "check_any[1] must be"),
    "timeout of zero": (base_tool + "\ntimeout = 0", "timeout must be"),
    "timeout past the limit": (base_tool + "\ntimeout = 61", "timeout must be"),
    "timeout that is not a number": (base_tool + '\ntimeout = "9"', "timeout must be"),
    "timeout that is a boolean": (base_tool + "\ntimeout = true", "timeout must be"),
}
for label, (body, needle) in odd_any.items():
    got = errors_of(manifest_text(tools={"fake-a": body}))
    check("validate rejects: " + label, any(needle in line for line in got), str(got))
check("validate accepts check_any with a timeout",
      errors_of(manifest_text(tools={"fake-a": 'check_any = [["fake-a"], ["fake-b", "--x"]]\ntimeout = 30\n'
                                                'channel = "manual"\nfix = "x"'})) == [])


tool("fake-runner", 'echo "fake-lint 1.6.0"')
calls = []
deps.subprocess.run = recording_run
try:
    home = new_home()
    manifest = write_manifest(lint_manifest())
    run(["--manifest", manifest, "--machine", "box", "--no-record", "--state-dir", os.path.join(home, "s")],
        env_for(home))
finally:
    deps.subprocess.run = real_run
check("check_any runs only the manifest's commands, in the home directory, with a timeout",
      calls and all(argv[0] == os.path.join(BIN, "fake-runner") and kw.get("cwd") == home
                    and kw.get("timeout") and kw.get("shell") is False for argv, kw in calls), str(calls))
drop("fake-runner")



check("redact does not eat a longer name that starts with the home path",
      deps.redact("/home/userny/x", "/home/user") == "~/x")
check("redact shortens a Windows home", deps.redact("C:\\Users\\user\\a and D:/Users/bob/b", "")
      == "~\\a and ~/b")
absent = os.path.join(new_home(), "no-such.toml")
code, out, err = run(["--manifest", absent, "--machine", "box", "--no-record"], env_for(os.path.dirname(absent)))
check("the manifest path in an error is redacted", "~" in err and os.path.dirname(absent) not in err, err)

home = new_home()
os.makedirs(os.path.join(home, ".config", "chezmoi"))
with open(os.path.join(home, ".config", "chezmoi", "chezmoi.toml"), "w") as fh:
    fh.write('[data]\n    machine_id = "lin1"\n')
manifest = write_manifest(multi)
state = os.path.join(home, "state")
code, out, err = run(["--manifest", manifest, "--machine", "mac1", "--state-dir", state], env_for(home))
check("a report for another machine records nothing",
      "is not this machine (lin1)" in err and not os.path.exists(os.path.join(state, "deps.json"))
      and not os.path.exists(os.path.join(home, ".local", "state", "agent-context", "health", "deps.json")),
      err)
code, out, err = run(["--manifest", manifest, "--machine", "lin1", "--state-dir", state], env_for(home))
check("naming this machine's own id still records", os.path.isfile(os.path.join(state, "deps.json")), err)



if os.path.isfile(REAL_MANIFEST):
    real = deps.load_manifest(REAL_MANIFEST)
    check("the real manifest validates", deps.validate(real) == [], str(deps.validate(real)))
    check("the real manifest names all seven machines",
          sorted(real["machine"]) == ["laptop", "server-host", "m4", "pc", "rp", "mirror-a", "mirror-b"])
    sets = {}
    for mid in real["machine"]:
        report = deps.evaluate(real, mid, {"PATH": os.path.join(ROOT, "empty")}, timeout=1, home=ROOT)
        sets[mid] = set(report["tools"])
    check("language servers apply to the Macs only",
          "csharp-ls" in sets["laptop"] and "csharp-ls" in sets["m4"]
          and not any("csharp-ls" in s or "tsgo" in s for m, s in sets.items() if m not in ("laptop", "m4")))
    check("the Synology nodes carry no chezmoi or basedpyright requirement",
          not ({"chezmoi", "basedpyright"} & (sets["mirror-a"] | sets["mirror-b"])))
    check("every machine carries uv and python3.14",
          all({"uv", "python3.14"} <= s for s in sets.values()))
    check("every relay client carries the relay tool, and server-host (the daemon host) does not",
          all("agent-context" in s for m, s in sets.items() if m != "server-host")
          and "agent-context" not in sets["server-host"])
    check("ruff is required on server-host only, through the lint role",
          "ruff" in sets["server-host"] and not any("ruff" in s for m, s in sets.items() if m != "server-host"))
    ruff = real["tool"]["ruff"]
    check("ruff: PATH first, then the offline uvx fallback, with the measured floor and a longer timeout",
          ruff["check_any"] == [["ruff", "--version"], ["uvx", "--offline", "ruff", "--version"]]
          and ruff["floor"] == "0.16.8" and ruff["timeout"] == 30 and "uv tool install ruff" in ruff["fix"],
          str(ruff))
    path_env = {"PATH": BIN + os.pathsep + "/usr/bin:/bin"}

    def ruff_status():
        return deps.evaluate(real, "server-host", path_env, timeout=3, home=ROOT)["tools"]["ruff"]["status"]

    tool("ruff", 'echo "ruff 0.16.8"')
    check("ruff on PATH at the floor is ok", ruff_status() == "ok")
    tool("ruff", 'echo "ruff 0.15.0"')
    tool("uvx", 'echo "ruff 0.16.8"')
    check("an old ruff on PATH is below the floor although uvx could supply a good one",
          ruff_status() == "below_floor")
    drop("ruff")
    check("no ruff on PATH and a warm uv cache is ok", ruff_status() == "ok")
    tool("uvx", 'echo "error: ruff was not found in the cache" >&2; exit 2')
    check("no ruff on PATH and an emptied uv cache is broken", ruff_status() == "broken")
    drop("uvx")
    check("no ruff and no uvx is missing", ruff_status() == "missing")
    tool("ruff", 'echo "ruff 0.16.8"')
    check("a PATH ruff is ok with no uvx and no uv cache at all", ruff_status() == "ok")
    drop("ruff", "uvx")
    relay_tools = real["role"]["relay"]["tools"] + real["role"]["relay-client"]["tools"]
    check("the relay's own tools are verify-only",
          all(real["tool"][t]["verify_only"] is True for t in relay_tools))
else:
    print("note: %s not found; real-manifest checks skipped" % REAL_MANIFEST)

shutil.rmtree(ROOT, ignore_errors=True)
print("%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
