#!/usr/bin/env python3
'Tests for deps-sync.py (fleet dependency management, phase D).'

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
SCRIPT = os.path.join(HERE, "deps-sync.py")

_spec = importlib.util.spec_from_file_location("deps_sync_under_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
ds: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)
deps_check = ds.deps_check

BASE = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-tests")
os.makedirs(BASE, exist_ok=True)
ROOT = tempfile.mkdtemp(dir=BASE, prefix="deps-sync-")
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
    return tempfile.mkdtemp(dir=ROOT, prefix="home-")


def env_for(home, path=BIN):
    return {"HOME": home, "PATH": path + os.pathsep + "/usr/bin:/bin"}


def manifest_text(machines=None, roles=None, tools=None):
    machines = machines or {"box": ("ubuntu", ["base"])}
    roles = roles or {"base": ["fake-uv"]}
    tools = tools or {"fake-uv": 'check = ["fake-uv", "--version"]\nfloor = "2.0.0"\n'
                                 'channel = "uv-tool"\nfix = "uv tool install fake-uv"'}
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


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ds.main(argv, env=env)
    return code, out.getvalue(), err.getvalue()


def drop(*names):
    for n in names:
        try:
            os.remove(os.path.join(BIN, n))
        except OSError:
            pass




check("strip_comment drops a trailing parenthetical",
      ds.strip_comment("uv tool install ruff (needs uv)") == "uv tool install ruff")
check("strip_comment leaves a plain line alone",
      ds.strip_comment("uv tool install basedpyright") == "uv tool install basedpyright")
check("parse_fix returns an argv", ds.parse_fix("uv tool install ruff (needs uv)")
      == ["uv", "tool", "install", "ruff"])
check("parse_fix on unbalanced quoting returns None", ds.parse_fix('uv tool install "ruff') is None)
check("parse_fix on empty text returns None", ds.parse_fix("") is None)
check("strip_comment leaves a mid-string parenthetical alone when real content follows",
      ds.strip_comment('foo install "pkg (beta)" --flag') == 'foo install "pkg (beta)" --flag')



uv_tool = {"channel": "uv-tool", "fix": "uv tool install fake-uv"}
check("a plain uv-tool is eligible", ds.eligible(uv_tool, "ubuntu") == (True, ""))
check("verify_only is never eligible",
      ds.eligible(dict(uv_tool, verify_only=True), "ubuntu")[0] is False)
check("a tool with a package key is never eligible",
      ds.eligible(dict(uv_tool, package="fake-uv"), "ubuntu")[0] is False)
check("a manual-channel tool is not eligible",
      ds.eligible({"channel": "manual", "fix": "install it"}, "ubuntu")[0] is False)
check("a brew-channel tool is not eligible",
      ds.eligible({"channel": "brew", "fix": "brew install x"}, "mac")[0] is False)
check("a relay-update channel tool is not eligible",
      ds.eligible({"channel": "relay-update", "fix": "x", "verify_only": True}, "ubuntu")[0] is False)
check("an os-restricted tool is ineligible off its os",
      ds.eligible(dict(uv_tool, os=["mac"]), "ubuntu")[0] is False)
check("an os-restricted tool is eligible on its os",
      ds.eligible(dict(uv_tool, os=["mac"]), "mac")[0] is True)
node_tool = {"channel": "node-tools-sync", "fix": "python3.14 ~/.agent-context/global/scripts/node-tools-sync.py"}
check("node-tools-sync is eligible", ds.eligible(node_tool, "mac")[0] is True)



home = new_home()
argv, why = ds.fix_argv(uv_tool, "ubuntu", home)
check("fix_argv returns the parsed argv for an eligible tool", argv == ["uv", "tool", "install", "fake-uv"], why)
argv, why = ds.fix_argv(dict(uv_tool, verify_only=True), "ubuntu", home)
check("fix_argv refuses a verify_only tool", argv is None and "verify_only" in why, why)
brewish = {"channel": "uv-tool", "fix": "brew install fake-uv"}
argv, why = ds.fix_argv(brewish, "mac", home)
check("fix_argv is not fooled by a brew command on an allowed channel",
      argv is None and "brew" in why, why)
sudoish = {"channel": "uv-tool", "fix": "sudo uv tool install fake-uv"}
argv, why = ds.fix_argv(sudoish, "ubuntu", home)
check("fix_argv refuses sudo", argv is None and "sudo" in why, why)
argv, why = ds.fix_argv(node_tool, "mac", home)
check("fix_argv expands a tilde in every argument, not only argv[0]",
      argv == ["python3.14", os.path.join(home, ".agent-context/global/scripts/node-tools-sync.py")],
      str(argv))
unparseable = {"channel": "uv-tool", "fix": 'uv tool install "fake-uv'}
argv, why = ds.fix_argv(unparseable, "ubuntu", home)
check("fix_argv refuses a fix line it cannot parse", argv is None and "does not parse" in why, why)



for label, fix in (
    ("a shell -c wrapper", 'sh -c "sudo apt install x"'),
    ("a full path to sudo", "/usr/bin/sudo apt install x"),
    ("a wrapper binary (env) hiding sudo", "env sudo apt install x"),
    ("bash instead of sh", 'bash -c "sudo apt install x"'),
    ("a python -c code injection", "python3.14 -c \"import os; os.system('rm -rf ~')\""),
):
    argv, why = ds.fix_argv({"channel": "uv-tool", "fix": fix}, "ubuntu", home)
    check("fix_argv refuses: " + label, argv is None, "fix=%r got argv=%r why=%r" % (fix, argv, why))
argv, why = ds.fix_argv(node_tool, "mac", home)
check("fix_argv still allows the real node-tools-sync fix", argv is not None, why)
argv, why = ds.fix_argv(uv_tool, "ubuntu", home)
check("fix_argv still allows the real uv-tool fix", argv is not None, why)



check("process_running is false for a name nothing runs",
      ds.process_running("no-such-process-xyz-deps-sync-test") is False)



home = new_home()
lock = ds.take_relay_lock(home)
check("take_relay_lock creates the exact path relay_update.py uses",
      lock == os.path.join(home, ".cache", "agent-context", "relay-update.lock")
      and os.path.isfile(lock), lock)
second = ds.take_relay_lock(home)
check("a second take while the first is held (fresh) returns None", second is None)
ds.release_relay_lock(lock)
check("release_relay_lock removes the file", not os.path.exists(lock))
third = ds.take_relay_lock(home)
check("the lock can be taken again after release", third is not None)
os.utime(third, (time.time() - ds.RELAY_LOCK_STALE_SECONDS - 5,) * 2)
fourth = ds.take_relay_lock(home)
check("a stale lock is reclaimed, not treated as held",
      fourth is not None and fourth == third)
if fourth:
    ds.release_relay_lock(fourth)



home = new_home()
lock = ds.take_relay_lock(home)
old = time.time() - (ds.RELAY_LOCK_STALE_SECONDS - 5)
os.utime(lock, (old, old))
ds.refresh_relay_lock(lock)
check("refresh_relay_lock moves the lock's mtime back to now",
      abs(os.stat(lock).st_mtime - time.time()) < 5)
ds.release_relay_lock(lock)



check("a normal writable home is not read-only", ds.home_is_read_only(new_home()) is False)



tool("fake-uv", 'echo "fake-uv 1.0.0"')
tool("uv", 'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then touch "%s"; fi' % os.path.join(ROOT, "uv-ran"))
home = new_home()
manifest = deps_check.load_manifest(write_manifest(manifest_text()))
report, rows = ds.plan(manifest, "box", env_for(home), home)
check("plan: a below-floor tool is planned for apply",
      len(rows) == 1 and rows[0]["tool"] == "fake-uv" and rows[0]["action"] == "apply", str(rows))
check("plan never runs the fix", not os.path.exists(os.path.join(ROOT, "uv-ran")))
drop("uv")


manifest2 = deps_check.load_manifest(write_manifest(manifest_text(
    tools={"fake-manual": 'check = ["fake-manual", "--version"]\nfloor = "2.0.0"\n'
                          'channel = "manual"\nfix = "install fake-manual by hand"'},
    roles={"base": ["fake-manual"]})))
report2, rows2 = ds.plan(manifest2, "box", env_for(home), home)
check("plan: a manual-channel problem is skip, never apply",
      len(rows2) == 1 and rows2[0]["tool"] == "fake-manual" and rows2[0]["action"] == "skip", str(rows2))


real_process_running = ds.process_running
ds.process_running = lambda name: name == "fake-uv"
try:
    report3, rows3 = ds.plan(manifest, "box", env_for(home), home)
finally:
    ds.process_running = real_process_running
check("plan: a live process holds the tool back from apply",
      rows3[0]["action"] == "skip" and "running" in rows3[0]["reason"], str(rows3))



home = new_home()
manifest_path = write_manifest(manifest_text())
tool("fake-uv", 'echo "fake-uv 1.0.0"')
code, out, err = run(["--manifest", manifest_path, "--machine", "box",
                      "--state-dir", os.path.join(home, "s")], env_for(home))
check("main with no --apply changes nothing and reports the plan",
      code == 3 and json.loads(out)["plan"][0]["tool"] == "fake-uv", out + err)
check("no state file is written by a dry run",
      not os.path.exists(os.path.join(home, "s", ds.STATE_FILE)))


tool("fake-uv", 'echo "fake-uv 3.0.0"')
code, out, err = run(["--manifest", manifest_path, "--machine", "box",
                      "--state-dir", os.path.join(home, "s")], env_for(home))
check("dry run with nothing to fix exits 0", code == 0, out + err)



code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--apply",
                      "--state-dir", os.path.join(home, "s")], env_for(home))
check("--apply with no tool and no --all is refused",
      code == 2 and "needs --all" in err, err)


tool("fake-uv", 'echo "fake-uv 3.0.0"')  
code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--apply", "bogus-tool-name",
                      "--state-dir", os.path.join(home, "s")], env_for(home))
check("--apply naming a tool absent from the plan is not a quiet success",
      code != 0 and "bogus-tool-name" in (out + err), out + err)



home = new_home()
manifest_path = write_manifest(manifest_text())
tool("fake-uv", 'echo "fake-uv 1.0.0"')
marker = os.path.join(ROOT, "fake-uv-fixed")
tool("uv", 'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then touch "%s"; fi' % marker)
code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--apply", "--all",
                      "--state-dir", os.path.join(home, "s")], env_for(home))
check("--apply --all runs the fix command", os.path.exists(marker), out + err)
doc = json.loads(out)
check("the applied entry reports the argv actually run",
      doc["applied"][0]["argv"] == ["uv", "tool", "install", "fake-uv"], out)
check("the run is verified after applying: fake-uv itself did not change, so it is not ok yet",
      doc["applied"][0]["result"] == "failed" and doc["applied"][0]["after"] != "ok", out)
check("--apply exits 3 when the post-check does not show ok", code == 3, out + err)
log_path = os.path.join(home, "s", ds.STATE_FILE)
check("a run log is written under the state directory", os.path.isfile(log_path))
with open(log_path) as fh:
    logged = json.load(fh)
check("the log names the tool, the command and the outcome",
      logged["runs"][0]["tool"] == "fake-uv" and logged["runs"][0]["result"] == "failed", str(logged))


home = new_home()
manifest_path = write_manifest(manifest_text())
tool("fake-uv", 'echo "fake-uv 1.0.0"')
target = os.path.join(BIN, "fake-uv")
tool("uv", 'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then '
           'printf "#!/bin/sh\\necho \\"fake-uv 3.0.0\\"\\n" > "%s"; chmod +x "%s"; fi' % (target, target))
code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--apply", "--all",
                      "--state-dir", os.path.join(home, "s2")], env_for(home))
doc = json.loads(out)
check("a fix that clears the problem is reported applied, exit 0",
      code == 0 and doc["applied"][0]["result"] == "applied" and doc["applied"][0]["after"] == "ok", out)
drop("uv", "fake-uv")



home = new_home()
two_tools = manifest_text(
    tools={"fake-uv": 'check = ["fake-uv", "--version"]\nfloor = "2.0.0"\nchannel = "uv-tool"\n'
                      'fix = "uv tool install fake-uv"',
           "fake-node": 'check = ["fake-node", "--version"]\nfloor = "2.0.0"\n'
                        'channel = "node-tools-sync"\nnode_package = "fake-node"\n'
                        'fix = "touch %s"' % os.path.join(ROOT, "should-not-run")},
    roles={"base": ["fake-uv", "fake-node"]})
manifest_path = write_manifest(two_tools)
tool("fake-uv", 'echo "fake-uv 1.0.0"')
tool("fake-node", 'echo "fake-node 1.0.0"')
tool("uv", 'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then echo done; fi')
code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--apply", "fake-uv",
                      "--state-dir", os.path.join(home, "s")], env_for(home))
doc = json.loads(out)
check("naming one tool touches only that tool",
      [e["tool"] for e in doc["applied"]] == ["fake-uv"], out)
check("an unselected problem tool's fix never runs",
      not os.path.exists(os.path.join(ROOT, "should-not-run")))
drop("uv", "fake-uv", "fake-node")



home = new_home()
manifest_path = write_manifest(two_tools.replace(
    'fix = "touch %s"' % os.path.join(ROOT, "should-not-run"),
    'fix = "touch %s"' % os.path.join(ROOT, "should-run-node")))
tool("fake-uv", 'echo "fake-uv 1.0.0"')
tool("fake-node", 'echo "fake-node 1.0.0"')
tool("uv", "echo done")
refresh_calls = []
real_refresh = ds.refresh_relay_lock
ds.refresh_relay_lock = lambda lock: (refresh_calls.append(lock), real_refresh(lock))
try:
    code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--apply", "--all",
                          "--state-dir", os.path.join(home, "s")], env_for(home))
finally:
    ds.refresh_relay_lock = real_refresh
check("the relay lock is refreshed at least once per applied tool",
      len(refresh_calls) >= 2, "%d calls, out=%s err=%s" % (len(refresh_calls), out, err))
drop("uv", "fake-uv", "fake-node")



home = new_home()
manifest_path = write_manifest(manifest_text())
tool("fake-uv", 'echo "fake-uv 1.0.0"')
marker = os.path.join(ROOT, "should-not-run-live")
tool("uv", 'touch "%s"' % marker)
ds.process_running = lambda name: name == "fake-uv"
try:
    code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--apply", "--all",
                          "--state-dir", os.path.join(home, "s")], env_for(home))
finally:
    ds.process_running = real_process_running
check("a fix is never run while the tool's process is live",
      not os.path.exists(marker), out + err)
check("main reports the skip and exits 3", code == 3
      and json.loads(out)["applied"][0]["result"] == "skipped", out)
drop("uv", "fake-uv")



home = new_home()
manifest_path = write_manifest(manifest_text())
tool("fake-uv", 'echo "fake-uv 1.0.0"')
tool("uv", 'touch "%s"' % os.path.join(ROOT, "should-not-run-ro"))
real_ro = ds.home_is_read_only
ds.home_is_read_only = lambda h: True
try:
    code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--apply", "--all",
                          "--state-dir", os.path.join(home, "s")], env_for(home))
finally:
    ds.home_is_read_only = real_ro
check("a read-only home refuses --apply and runs nothing",
      code == 3 and "read-only" in err and not os.path.exists(os.path.join(ROOT, "should-not-run-ro")),
      out + err)
ds.home_is_read_only = lambda h: True
try:
    code, out, err = run(["--manifest", manifest_path, "--machine", "box",
                          "--state-dir", os.path.join(home, "s2")], env_for(home))
finally:
    ds.home_is_read_only = real_ro
check("a dry run still reports normally on a read-only home", code == 3 and "plan" in out, out + err)
drop("uv", "fake-uv")



home = new_home()
manifest_path = write_manifest(manifest_text())
tool("fake-uv", 'echo "fake-uv 1.0.0"')
marker = os.path.join(ROOT, "should-not-run-relaylock")
tool("uv", 'touch "%s"' % marker)
held = ds.take_relay_lock(home)
code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--apply", "--all",
                      "--state-dir", os.path.join(home, "s")], env_for(home))
check("--apply refuses while the relay-update lock is held, exit 4",
      code == 4 and "busy" in err and not os.path.exists(marker), out + err)
ds.release_relay_lock(held)
drop("uv", "fake-uv")



home = new_home()
manifest_path = write_manifest(manifest_text())
lock_dir = os.path.join(home, "s")
os.makedirs(lock_dir)
own_held = open(os.path.join(lock_dir, ds.LOCK_FILE), "w")
fcntl.flock(own_held, fcntl.LOCK_EX | fcntl.LOCK_NB)
code, out, err = run(["--manifest", manifest_path, "--machine", "box", "--state-dir", lock_dir],
                     env_for(home))
check("a second deps-sync run while the first holds the lock exits 4",
      code == 4 and "busy" in err, err)
own_held.close()



garbage = write_manifest("this is not = = toml [")
code, out, err = run(["--manifest", garbage, "--machine", "box"], env_for(new_home()))
check("a manifest that is not TOML exits 2", code == 2 and "not valid TOML" in err, err)

code, out, err = run(["--manifest", manifest_path, "--machine", "ghost"], env_for(new_home()))
check("an unknown machine id exits 2", code == 2 and "ghost" in err, err)

shutil.rmtree(ROOT, ignore_errors=True)
print("%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
