#!/usr/bin/env python3
'Tests for deps-check.py\'s client-side upload (fleet dependency management, phase A2 client).\n\nA relay-only host (no store checkout) has no machines/<uuid>/ of its own to publish into, so\nafter writing its local deps.json it uploads the same report through the daemon\'s relay_report\nMCP tool (kind="deps"), the one pathway (policy, policy) store_mcp.py reaches it by. Modeled on\ntoken-usage-collect.py\'s upload_month() pattern: same "never block the local run" discipline.\n\nTransport (HTTP, bearer header, JSON-RPC, redirects, the size cap, the deadline) is store_mcp\'s\nown contract and is covered by test-store-mcp.py; this battery stubs store_mcp.call in-process\nand checks deps-check\'s own contract: the tool name and arguments it sends, and what a raised\nToolError/StoreUnreachable does to the local run.'

import contextlib
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

sys.path.insert(0, HERE)
import store_mcp  

_spec = importlib.util.spec_from_file_location("deps_check_upload_under_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
deps: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deps)

BASE = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-tests")
os.makedirs(BASE, exist_ok=True)
ROOT = tempfile.mkdtemp(dir=BASE, prefix="deps-upload-")

passed = 0
failures = []


def check(label, ok, detail: object = ""):
    global passed
    if ok:
        passed += 1
    else:
        failures.append("%s %s" % (label, detail))
        print("FAIL:", label, detail)


def new_home():
    return tempfile.mkdtemp(dir=ROOT, prefix="home-")


def write_env_file(home, host=None, token=None, port=None):
    d = os.path.join(home, ".config", "agent-context")
    os.makedirs(d, exist_ok=True)
    lines = []
    if host is not None:
        lines.append("AGENT_CONTEXT_HOST=%s" % host)
    if token is not None:
        lines.append('AGENT_CONTEXT_TOKEN="%s"' % token)
    if port is not None:
        lines.append("AGENT_CONTEXT_PORT=%s" % port)
    with open(os.path.join(d, "env"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


SAMPLE_REPORT = {
    "schema": 1, "machine": "pc", "os": "ubuntu", "at": 1700000000, "ok": True,
    "tools": {"uv": {"status": "ok", "found": "0.11.24", "detail": "", "role": "relay",
                     "floor": "0.11.24"}},
    "problems": [], "unexpected": [],
    "fleet": {"ok": True, "missing": [], "broken": [], "below_floor": []},
}




home = new_home()
write_env_file(home, host="ls.example", token="tok123", port="8765")
got = deps.relay_env(home, {})
check("relay_env reads the env file when the environment is empty",
      got == {"AGENT_CONTEXT_HOST": "ls.example", "AGENT_CONTEXT_TOKEN": "tok123",
              "AGENT_CONTEXT_PORT": "8765"}, str(got))

got = deps.relay_env(home, {"AGENT_CONTEXT_HOST": "override.example"})
check("relay_env: the environment wins over the file",
      got["AGENT_CONTEXT_HOST"] == "override.example" and got["AGENT_CONTEXT_TOKEN"] == "tok123",
      str(got))

home2 = new_home()
got = deps.relay_env(home2, {})
check("relay_env with no file and no environment returns all-None",
      got == {"AGENT_CONTEXT_HOST": None, "AGENT_CONTEXT_TOKEN": None, "AGENT_CONTEXT_PORT": None},
      str(got))

home3 = new_home()
write_env_file(home3, host="ls.example")  
got = deps.relay_env(home3, {})
check("relay_env tolerates a file missing some keys",
      got["AGENT_CONTEXT_HOST"] == "ls.example" and got["AGENT_CONTEXT_TOKEN"] is None, str(got))



old_call = store_mcp.call


def restore_call():
    store_mcp.call = old_call
    deps.store_mcp.call = old_call


calls = []


def fake_call_ok(tool, args, env=None):
    calls.append((tool, args, env))
    return {"machine_uuid": "x", "written": True}


deps.store_mcp.call = fake_call_ok
try:
    env = {"AGENT_CONTEXT_HOST": "127.0.0.1", "AGENT_CONTEXT_PORT": "8765", "AGENT_CONTEXT_TOKEN": "sekrit"}
    deps.upload_deps(env, SAMPLE_REPORT, "/home/pc-user")
finally:
    restore_call()
check("upload_deps calls relay_report with kind=deps", calls and calls[0][0] == "relay_report",
      str(calls))
tool, args, sent_env = calls[0]
check("upload_deps passes the placeholder uuid_hint",
      args.get("uuid_hint") == deps.UPLOAD_PLACEHOLDER_UUID, str(args))
check("upload_deps sends no month (deps has none)", "month" not in args, str(args))
sent_body = json.loads(args.get("body", "{}"))
check("upload_deps: the body carries hostname and home_dir alongside the report",
      sent_body.get("home_dir") == "/home/pc-user" and isinstance(sent_body.get("hostname"), str)
      and sent_body.get("hostname"), str(sent_body))
check("upload_deps: the report's own fields ride along unchanged",
      sent_body.get("at") == SAMPLE_REPORT["at"] and sent_body.get("fleet") == SAMPLE_REPORT["fleet"],
      str(sent_body))
check("upload_deps: the relay env is passed through to store_mcp.call",
      sent_env == env, str(sent_env))

calls = []
deps.store_mcp.call = fake_call_ok


def fake_call_tool_error(tool, args, env=None):
    calls.append((tool, args, env))
    raise store_mcp.ToolError("no known machine matches hostname/home_dir")


deps.store_mcp.call = fake_call_tool_error
raised = None
try:
    deps.upload_deps(env, SAMPLE_REPORT, "/home/pc-user")
except Exception as exc:
    raised = exc
finally:
    restore_call()
check("upload_deps raises when store_mcp.call raises ToolError",
      isinstance(raised, store_mcp.ToolError), str(raised))


def fake_call_unreachable(tool, args, env=None):
    raise store_mcp.StoreUnreachable("the daemon is unreachable (ConnectionRefusedError)")


deps.store_mcp.call = fake_call_unreachable
raised = None
started = time.monotonic()
try:
    deps.upload_deps(env, SAMPLE_REPORT, "/home/pc-user")
except Exception as exc:
    raised = exc
finally:
    restore_call()
check("upload_deps raises when store_mcp.call raises StoreUnreachable",
      isinstance(raised, store_mcp.StoreUnreachable), str(raised))
check("upload_deps raises promptly (no blocking wait of its own)",
      time.monotonic() - started < 1.0)



checkout_root = tempfile.mkdtemp(dir=ROOT, prefix="checkout-")
os.makedirs(os.path.join(checkout_root, "server"))
check("store_is_checkout: a root with a server/ dir is a checkout",
      deps.store_is_checkout(checkout_root) is True)

relay_root = tempfile.mkdtemp(dir=ROOT, prefix="relay-")
os.makedirs(os.path.join(relay_root, "global"))
check("store_is_checkout: a materialized relay root (no server/, no .git) is not a checkout",
      deps.store_is_checkout(relay_root) is False)

git_root = tempfile.mkdtemp(dir=ROOT, prefix="git-")
os.makedirs(os.path.join(git_root, ".git"))
check("store_is_checkout: a bare .git also counts as a checkout",
      deps.store_is_checkout(git_root) is True)




worktree_root = tempfile.mkdtemp(dir=ROOT, prefix="worktree-")
with open(os.path.join(worktree_root, ".git"), "w") as fh:
    fh.write("gitdir: /somewhere/else/.git/worktrees/x\n")
check("store_is_checkout: a worktree's .git FILE also counts as a checkout",
      deps.store_is_checkout(worktree_root) is True)



BIN = os.path.join(ROOT, "bin")
os.makedirs(BIN, exist_ok=True)


def tool(name, body):
    path = os.path.join(BIN, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


def manifest_text():
    return ('schema = 1\n[machine.box]\nos = "ubuntu"\nroles = ["base"]\n'
            '[role.base]\ntools = ["fake-a"]\n[tool.fake-a]\n'
            'check = ["fake-a", "--version"]\nchannel = "manual"\nfix = "x"\n')


def write_manifest(text):
    p = os.path.join(ROOT, "manifest-%d.toml" % time.monotonic_ns())
    with open(p, "w") as fh:
        fh.write(text)
    return p


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = deps.main(argv, env=env)
    return code, out.getvalue(), err.getvalue()


tool("fake-a", 'echo "fake-a 1.0.0"')




real_store_is_checkout = deps.store_is_checkout
real_upload_deps = deps.upload_deps

upload_calls = []


def fake_upload_ok(env, report, home):
    upload_calls.append((env, report, home))


deps.store_is_checkout = lambda root: False
deps.upload_deps = fake_upload_ok
try:
    home = new_home()
    write_env_file(home, host="ls.example", port="8765")
    manifest = write_manifest(manifest_text())
    code, out, err = run(["--manifest", manifest, "--machine", "box",
                          "--state-dir", os.path.join(home, "s")],
                         {"HOME": home, "PATH": BIN + os.pathsep + "/usr/bin:/bin"})
finally:
    deps.store_is_checkout = real_store_is_checkout
    deps.upload_deps = real_upload_deps
check("main uploads on a relay-only host with a configured relay env",
      len(upload_calls) == 1, str(upload_calls))
check("a successful upload does not change the ok exit code", code == 0, str((code, out, err)))

upload_calls = []
deps.store_is_checkout = lambda root: False


def fake_upload_raises(env, report, home):
    upload_calls.append((env, report, home))
    raise RuntimeError("HTTP 500")


deps.upload_deps = fake_upload_raises
try:
    home = new_home()
    write_env_file(home, host="ls.example", port="8765")
    manifest = write_manifest(manifest_text())
    code, out, err = run(["--manifest", manifest, "--machine", "box",
                          "--state-dir", os.path.join(home, "s")],
                         {"HOME": home, "PATH": BIN + os.pathsep + "/usr/bin:/bin"})
finally:
    deps.store_is_checkout = real_store_is_checkout
    deps.upload_deps = real_upload_deps
check("a failed upload still exits 0 (the local check itself is clean)", code == 0, str((code, out, err)))
check("a failed upload is noted on stderr, not stdout",
      "upload" in err and "HTTP 500" in err and "upload" not in out, str((out, err)))
check("a failed upload does not change the printed report", "all 1 tools ok" in out, out)

upload_calls = []
deps.store_is_checkout = lambda root: False
deps.upload_deps = fake_upload_ok
try:
    home = new_home()  
    manifest = write_manifest(manifest_text())
    code, out, err = run(["--manifest", manifest, "--machine", "box",
                          "--state-dir", os.path.join(home, "s")],
                         {"HOME": home, "PATH": BIN + os.pathsep + "/usr/bin:/bin"})
finally:
    deps.store_is_checkout = real_store_is_checkout
    deps.upload_deps = real_upload_deps
check("no upload is attempted with no relay host configured", len(upload_calls) == 0, str(upload_calls))
check("no error is printed for an unconfigured relay host", err == "", err)

upload_calls = []
deps.store_is_checkout = lambda root: True  
deps.upload_deps = fake_upload_ok
try:
    home = new_home()
    write_env_file(home, host="ls.example", port="8765")
    manifest = write_manifest(manifest_text())
    code, out, err = run(["--manifest", manifest, "--machine", "box",
                          "--state-dir", os.path.join(home, "s")],
                         {"HOME": home, "PATH": BIN + os.pathsep + "/usr/bin:/bin"})
finally:
    deps.store_is_checkout = real_store_is_checkout
    deps.upload_deps = real_upload_deps
check("a daemon host (real checkout) never attempts the upload", len(upload_calls) == 0, str(upload_calls))

upload_calls = []
deps.store_is_checkout = lambda root: False
deps.upload_deps = fake_upload_ok
try:
    home = new_home()
    write_env_file(home, host="ls.example", port="8765")
    manifest = write_manifest(manifest_text())
    code, out, err = run(["--manifest", manifest, "--machine", "box", "--no-record",
                          "--state-dir", os.path.join(home, "s")],
                         {"HOME": home, "PATH": BIN + os.pathsep + "/usr/bin:/bin"})
finally:
    deps.store_is_checkout = real_store_is_checkout
    deps.upload_deps = real_upload_deps
check("--no-record uploads nothing either, matching the local no-write contract",
      len(upload_calls) == 0, str(upload_calls))

shutil.rmtree(ROOT, ignore_errors=True)
print("%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
