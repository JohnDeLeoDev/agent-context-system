#!/usr/bin/env python3
'Battery for node-tools-sync.py and the store\'s pnpm tools manifest (Consolidation Phase 6a).\n\nContract under test:\n  node-tools-sync.py [--source DIR] [--root DIR] [--bin-dir DIR] [--check]\n  exit 0: installed, or already current. exit 3: --check found drift. exit 127: no pnpm\n  on PATH. Any other refusal or failure exits non-zero with the cause on stderr, and leaves\n  the previous root, the bin dir and the root\'s parent directory as they were.\n\nThe install cases need pnpm on PATH and the registry. Every case writes only inside a temp\ndir; pnpm\'s content store is the user\'s own cache. Negative assertions ("left untouched",\n"runs no pnpm") are joined to the positive result they qualify, so none of them can pass\nwhile the script is missing.'

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
G = os.path.join(STORE, "global")
SYNC = os.path.join(G, "scripts", "node-tools-sync.py")
SOURCE = os.path.join(G, "node-tools")
MCP = os.path.join(G, "mcp-servers.json")
CHEZ = os.environ.get("AGENT_CONTEXT_CHEZMOI_SOURCE") or os.path.expanduser("~/.local/share/chezmoi")

TOOLS = ("@mozilla/firefox-devtools-mcp", "@typescript/native-preview", "copilot-api", "xcodebuildmcp")
LINKED = ("copilot-api", "tsgo")
MCP_ROOT = "{HOME}/.local/share/agent-context/node-tools/node_modules/.bin/"
FROM = 'message.role === "user" ? handleUserMessage(message)'
TO = 'message.role === "user" || message.role === "system" ? handleUserMessage(message)'
MISSING_PKG = "@agent-context-fixture/does-not-exist-phase-6a"
DEAD = "http://127.0.0.1:9"
OFFLINE = {k: DEAD for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")}
OFFLINE.update({"NO_PROXY": "", "no_proxy": ""})

tmp = tempfile.mkdtemp(prefix="node-tools-sync-test-")
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


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def real(p):
    return os.path.realpath(p)


def sync(*args, env=None, timeout=900):
    
    print("    running node-tools-sync.py %s" % " ".join(args), flush=True)
    e = dict(os.environ)
    e.update(env or {})
    try:
        r = subprocess.run([sys.executable, SYNC] + list(args), capture_output=True, text=True, env=e,
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout
    return r.returncode, r.stdout, r.stderr


def failed_with(rc, err, needle):
    'A refusal: exit neither 0 nor 127, the cause named on stderr, and the script present.'
    return os.path.isfile(SYNC) and rc not in (0, 127) and needle in err


def snapshot(*roots):
    'Every entry under each root: mode, size, mtime and link target. None for a missing root.'
    out = {}
    for root in roots:
        if not os.path.lexists(root):
            out[root] = None
            continue
        st = os.lstat(root)
        out[root] = (st.st_mode, st.st_mtime_ns)
        for dirpath, dirnames, files in os.walk(root):
            for n in dirnames + files:
                p = os.path.join(dirpath, n)
                st = os.lstat(p)
                out[p] = (st.st_mode, st.st_size, st.st_mtime_ns, os.readlink(p) if os.path.islink(p) else None)
    return out


def first_diff(a, b):
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            return "%s: %r -> %r" % (k, a.get(k), b.get(k))
    return ""


def listing(d):
    return sorted(os.listdir(d)) if os.path.isdir(d) else None


def run_bin(path, args):
    if not os.path.lexists(path):
        return "missing"
    try:
        r = subprocess.run([path] + args, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, **OFFLINE), stdin=subprocess.DEVNULL)
        return r.returncode
    except (OSError, subprocess.TimeoutExpired) as ex:
        return repr(ex)


def registry_latest(name):
    "The registry's `latest` dist-tag for a package, or None."
    url = "https://registry.npmjs.org/-/package/%s/dist-tags" % urllib.parse.quote(name, safe="@")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")).get("latest")
    except (OSError, ValueError):
        pass
    try:
        r = subprocess.run(["npm", "view", name, "dist-tags.latest"], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def mcp_tools(path, args, wait=90):
    'Tool count from an MCP initialize + tools/list over stdio, behind a dead proxy; None on failure.'
    if not os.path.lexists(path):
        return "missing"
    proc = subprocess.Popen([path] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=dict(os.environ, **OFFLINE))
    stdin, stdout = proc.stdin, proc.stdout
    if stdin is None or stdout is None:
        proc.kill()
        proc.wait()
        return None
    timer = threading.Timer(wait, proc.kill)
    timer.start()

    def call(msg, want):
        stdin.write(json.dumps(msg) + "\n")
        stdin.flush()
        for line in stdout:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("id") == want:
                return data
        return None

    try:
        init = call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test-node-tools-sync", "version": "0"}}}, 1)
        if not init or "result" not in init:
            return None
        stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        stdin.flush()
        listed = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 2)
        return len(listed["result"]["tools"]) if listed and "result" in listed else None
    except (OSError, ValueError):
        return None
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()


def copy_source(name):
    "A copy of the store's tools source; an empty dir when the source does not exist yet."
    dst = os.path.join(tmp, name)
    if os.path.isdir(SOURCE):
        shutil.copytree(SOURCE, dst, ignore=shutil.ignore_patterns("node_modules"))
    else:
        os.makedirs(dst)
    return dst


def fake_pnpm():
    'PATH overrides whose pnpm only records that it was called, plus the call log path.'
    d = os.path.join(tmp, "fake-pnpm-bin")
    log = os.path.join(tmp, "fake-pnpm-calls.log")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "pnpm")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % log)
    os.chmod(path, 0o755)
    if os.path.exists(log):
        os.remove(log)
    return dict(OFFLINE, PATH=d + os.pathsep + os.environ.get("PATH", "")), log


def break_patch(src_dir):
    pdir = os.path.join(src_dir, "patches")
    for p in (os.listdir(pdir) if os.path.isdir(pdir) else []):
        path = os.path.join(pdir, p)
        text = read(path) or ""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("handleAssistantMessage(message)", "handleAssistantMessageGone(message)"))



print("[1] the manifest")
check("node-tools-sync.py exists", os.path.isfile(SYNC))
check("global/node-tools has package.json and pnpm-workspace.yaml and no lockfile",
      all(os.path.isfile(os.path.join(SOURCE, f)) for f in ("package.json", "pnpm-workspace.yaml"))
      and not os.path.lexists(os.path.join(SOURCE, "pnpm-lock.yaml")))
try:
    pkg = json.loads(read(os.path.join(SOURCE, "package.json")) or "{}")
except ValueError:
    pkg = {}
deps = {}
for field in ("dependencies", "devDependencies", "optionalDependencies"):
    deps.update(pkg.get(field) or {})
check("the manifest names exactly the four tools", sorted(deps) == sorted(TOOLS), repr(sorted(deps)))
check("every tool is `latest`", bool(deps) and all(v == "latest" for v in deps.values()), repr(deps))
check("agentContextBins names copilot-api and tsgo", sorted(pkg.get("agentContextBins") or []) == sorted(LINKED),
      repr(pkg.get("agentContextBins")))
ws = read(os.path.join(SOURCE, "pnpm-workspace.yaml")) or ""
m = re.search(r"(?m)^\s+'?copilot-api'?:\s*'?(patches/[^'\s]+)'?\s*$", ws)
check("pnpm-workspace.yaml patches copilot-api with no version in the key", m is not None, repr(ws[-300:]))
check("the copilot-api patch file exists", bool(m) and os.path.isfile(os.path.join(SOURCE, m.group(1))))
check("pnpm-workspace.yaml decides every build script", bool(ws) and "set this to true or false" not in ws)


print("[2] no pnpm on PATH")
empty = os.path.join(tmp, "empty-path")
os.makedirs(empty)
root0, bin0 = os.path.join(tmp, "share0", "node-tools"), os.path.join(tmp, "bin0")
rc, out, err = sync("--source", SOURCE, "--root", root0, "--bin-dir", bin0, env={"PATH": empty})
check("with no pnpm on PATH, sync exits 127, names pnpm and creates no root or bin dir",
      rc == 127 and "pnpm" in err and not os.path.lexists(root0) and not os.path.lexists(bin0),
      "rc=%s err=%r" % (rc, err[-300:]))


print("[3] install")
ROOT, BIN = os.path.join(tmp, "share", "node-tools"), os.path.join(tmp, "bin")
rc, out, err = sync("--source", SOURCE, "--root", ROOT, "--bin-dir", BIN)
installed_ok = rc == 0
check("sync into an empty root exits 0", installed_ok, "rc=%s err=%r" % (rc, err[-600:]))
for name in TOOLS:
    try:
        got = json.loads(read(os.path.join(ROOT, "node_modules", name, "package.json")) or "{}").get("version")
    except ValueError:
        got = None
    want = registry_latest(name)
    check("installed %s is the registry's latest" % name, got is not None and got == want,
          "installed=%r latest=%r" % (got, want))
dotbin = os.path.join(ROOT, "node_modules", ".bin")
for name, args in (("copilot-api", ["--help"]), ("tsgo", ["--version"]),
                   ("xcodebuildmcp", ["--help"]), ("firefox-devtools-mcp", ["--help"])):
    got = run_bin(os.path.join(dotbin, name), args)
    check("installed %s runs %s offline" % (name, args[0]), got == 0, "got %r" % got)
for name, args in (("copilot-api", ["--help"]), ("tsgo", ["--version"])):
    link = os.path.join(BIN, name)
    body = "" if os.path.islink(link) else (read(link) or "")
    into_root = (os.path.islink(link) and real(link).startswith(real(ROOT) + os.sep)) or ROOT in body
    check("%s in the bin dir points into the root" % name, into_root, "lexists=%s" % os.path.lexists(link))
    got = run_bin(link, args)
    check("%s runs %s from the bin dir" % (name, args[0]), got == 0, "got %r" % got)
main = os.path.join(real(os.path.join(ROOT, "node_modules", "copilot-api")), "dist", "main.js")
src = read(main) or ""
check("installed copilot-api carries the system-role fix exactly once", src.count(TO) == 1, "count=%d" % src.count(TO))
check("installed copilot-api has no unpatched routing left", bool(src) and src.count(FROM) == 0,
      "count=%d" % src.count(FROM))


print("[4] --check and a second sync")
env, log = fake_pnpm()
rc, out, err = sync("--check", "--source", SOURCE, "--root", ROOT, "--bin-dir", BIN, env=env)
check("--check right after a sync exits 0 and runs no pnpm", installed_ok and rc == 0 and not os.path.exists(log),
      "rc=%s pnpm calls=%r err=%r" % (rc, read(log), err[-300:]))
before = snapshot(ROOT, BIN)
rc, out, err = sync("--source", SOURCE, "--root", ROOT, "--bin-dir", BIN)
check("a second sync with no new release exits 0 and changes nothing in the root or the bin dir",
      installed_ok and rc == 0 and snapshot(ROOT, BIN) == before,
      "rc=%s %s" % (rc, first_diff(before, snapshot(ROOT, BIN))))


print("[5] MCP servers")
for name, args in (("xcodebuildmcp", ["mcp"]), ("firefox-devtools-mcp", [])):
    n = mcp_tools(os.path.join(dotbin, name), args)
    check("%s answers initialize and tools/list behind a dead proxy" % name, isinstance(n, int) and n > 0,
          "tools=%r" % n)
try:
    servers = (json.loads(read(MCP) or "{}").get("servers")) or {}
except ValueError:
    servers = {}
for server, (binname, args) in (("xcodebuild-mcp", ("xcodebuildmcp", ["mcp"])),
                                ("firefox-devtools", ("firefox-devtools-mcp", []))):
    spec = servers.get(server) or {}
    check("mcp-servers.json %s launches the installed %s" % (server, binname),
          spec.get("command") == MCP_ROOT + binname and (spec.get("args") or []) == args,
          "command=%r args=%r" % (spec.get("command"), spec.get("args")))


print("[6] a failed install")
nopkg = copy_source("src-missing")
try:
    npkg = json.loads(read(os.path.join(nopkg, "package.json")) or "{}")
except ValueError:
    npkg = {}
npkg.setdefault("dependencies", {})[MISSING_PKG] = "latest"
with open(os.path.join(nopkg, "package.json"), "w", encoding="utf-8") as fh:
    json.dump(npkg, fh, indent=2)
env, log = fake_pnpm()
rc, out, err = sync("--check", "--source", nopkg, "--root", ROOT, "--bin-dir", BIN, env=env)
check("--check against a changed manifest exits 3 and runs no pnpm", rc == 3 and not os.path.exists(log),
      "rc=%s pnpm calls=%r err=%r" % (rc, read(log), err[-300:]))
parent = os.path.dirname(ROOT)
before, parent_before = snapshot(ROOT, BIN), listing(parent)
rc, out, err = sync("--source", nopkg, "--root", ROOT, "--bin-dir", BIN)
named = failed_with(rc, err, MISSING_PKG)
check("a package that does not exist fails the sync and is named", named, "rc=%s err=%r" % (rc, err[-400:]))
check("that failure leaves the root and the bin dir untouched",
      named and installed_ok and snapshot(ROOT, BIN) == before, first_diff(before, snapshot(ROOT, BIN)))
check("that failure leaves nothing new beside the root",
      named and installed_ok and listing(parent) == parent_before, "%r -> %r" % (parent_before, listing(parent)))


print("[7] a copilot-api patch that no longer applies")
bad = copy_source("src-badpatch")
break_patch(bad)
root3, bin3 = os.path.join(tmp, "share3", "node-tools"), os.path.join(tmp, "bin3")
rc, out, err = sync("--source", bad, "--root", root3, "--bin-dir", bin3)
named = failed_with(rc, err, "copilot-api")
check("a patch that fails to apply fails a first sync and names copilot-api", named,
      "rc=%s err=%r" % (rc, err[-400:]))
check("a failed first sync links no copilot-api and leaves no root behind",
      named and not os.path.lexists(os.path.join(bin3, "copilot-api")) and not os.path.lexists(root3))
before, parent_before = snapshot(ROOT, BIN), listing(parent)
rc, out, err = sync("--source", bad, "--root", ROOT, "--bin-dir", BIN)
named = failed_with(rc, err, "copilot-api")
check("a patch that fails to apply fails a sync over a working root", named, "rc=%s err=%r" % (rc, err[-400:]))
check("the working root and its bin links stay as they were, with nothing left beside them",
      named and installed_ok and snapshot(ROOT, BIN) == before and listing(parent) == parent_before,
      first_diff(before, snapshot(ROOT, BIN)) or "%r -> %r" % (parent_before, listing(parent)))


print("[8] a foreign file at a bin link target")
bin4 = os.path.join(tmp, "bin4")
os.makedirs(bin4)
foreign = os.path.join(bin4, "copilot-api")
FOREIGN = "#!/bin/sh\necho foreign\n"
with open(foreign, "w", encoding="utf-8") as fh:
    fh.write(FOREIGN)
rc, out, err = sync("--source", SOURCE, "--root", ROOT, "--bin-dir", bin4)
check("a foreign file at a link target is refused by name and left as it was",
      failed_with(rc, err, foreign) and not os.path.islink(foreign) and read(foreign) == FOREIGN,
      "rc=%s err=%r" % (rc, err[-300:]))


print("[9] nothing installs at session start")
hooks_dir = os.path.join(G, "hooks")
scan = [os.path.join(hooks_dir, f) for f in sorted(os.listdir(hooks_dir))
        if not f.endswith((".meta.toml", ".meta.json"))]
scan += [os.path.join(G, "scripts", f) for f in ("home-materialize.py", "harness-materialize.py",
                                                 "home-settings-sync.py", "project-materialize.py",
                                                 "agents-materialize.py")]
offenders = []
for path in scan:
    for i, line in enumerate((read(path) or "").splitlines(), 1):
        code = line.strip()
        if code.startswith("#"):
            continue
        if re.search(r"\bpnpm\b\W+(?:\w+\W+)*?install\b", code) or ("node-tools-sync" in code and "--check" not in code):
            offenders.append("%s:%d" % (os.path.basename(path), i))
check("no hook or materialize step runs a pnpm install or a non-check sync", not offenders, "; ".join(offenders[:6]))


print("[10] chezmoi")
if os.path.isdir(CHEZ):
    bindir = os.path.join(CHEZ, "dot_local", "bin")
    check("chezmoi no longer ships build-copilot-api",
          not os.path.lexists(os.path.join(bindir, "executable_build-copilot-api")))
    check(".chezmoiremove removes the installed build-copilot-api",
          bool(re.search(r"(?m)^\.local/bin/build-copilot-api\s*$", read(os.path.join(CHEZ, ".chezmoiremove")) or "")))
    ip = read(os.path.join(CHEZ, "run_onchange_after_install-packages.sh.tmpl")) or ""
    check("install-packages runs node-tools-sync.py and no longer installs tsgo through npm -g",
          "node-tools-sync.py" in ip and "npm install -g @typescript/native-preview" not in ip)
else:
    check("chezmoi source present at %s" % CHEZ, False)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
