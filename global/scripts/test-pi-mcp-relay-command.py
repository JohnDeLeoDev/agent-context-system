#!/usr/bin/env python3
"Battery for the pi agent-context bridge's choice of server command (C11 prerequisite b).\n\nThe extension runs under node with a fake `pi`, against a fake relay and a fake `uv`\nthat log how they were started and speak newline-delimited JSON-RPC.\n\nThe extension source comes from BATTERY_EXTENSION_SRC (default: the store's script).\n\nRun: python3 test-pi-mcp-relay-command.py"
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

STORE = Path(os.environ.get("AGENT_CONTEXT_STORE") or Path.home() / ".agent-context")
EXTENSION = Path(os.environ.get("BATTERY_EXTENSION_SRC")
                 or STORE / "global" / "scripts" / "agent-context-mcp.ts")
MATERIALIZER = STORE / "global" / "scripts" / "harness-materialize.py"
TYPEBOX_MODULES = Path.home() / ".pi" / "agent" / "npm" / "node_modules"
TMP_BASE = Path.home() / ".cache" / "tmp"

FAKE_SERVER = r"""#!__PYTHON__
import json, os, sys
role = "__ROLE__"
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"role": role, "argv": sys.argv[1:],
                         "probe": os.environ.get("BATTERY_PROBE")}) + "\n")
served_call = False
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-" + role, "version": "0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "ping", "description": "fake",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "pong from " + role}]}
        served_call = True
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
    if served_call and os.environ.get("FAKE_EXIT_AFTER_CALL"):
        sys.exit(0)
"""

RUNNER_JS = r"""
import { copyFileSync, chmodSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
const scenario = JSON.parse(process.argv[3]);
const mod = await import(process.argv[2]);
const handlers = {}, tools = {};
const pi = {
  on: (e, h) => { (handlers[e] ||= []).push(h); },
  registerCommand: () => {},
  registerTool: (t) => { tools[t.name] = t; },
};
await mod.default(pi);
const notes = [];
const ctx = { hasUI: true, ui: { notify: (m, l) => notes.push([l, m]) } };
for (const h of handlers["session_start"] || []) await h({}, ctx);
const out = { tools: Object.keys(tools), notes, calls: [] };
const names = Object.keys(tools);
if (names.length) {
  out.calls.push(await tools[names[0]].execute("c1", {}));
  if (scenario.installAfterFirstCall) {
    mkdirSync(dirname(scenario.installAfterFirstCall.to), { recursive: true });
    copyFileSync(scenario.installAfterFirstCall.from, scenario.installAfterFirstCall.to);
    chmodSync(scenario.installAfterFirstCall.to, 0o755);
    await new Promise((r) => setTimeout(r, 300));
    out.calls.push(await tools[names[0]].execute("c2", {}));
  }
}
for (const h of handlers["session_shutdown"] || []) await h({}, ctx);
console.log(JSON.stringify(out));
process.exit(0);
"""

results = []


def check(name: str, ok: object, detail: object = "") -> None:
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:700]
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


class Sandbox:
    "A scratch HOME with a fake `uv` on PATH and a copy of the extension beside a\n    node_modules link, so `typebox` resolves the way pi's own install does."

    def __init__(self):
        TMP_BASE.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="pi-mcp-relay-")).resolve()
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.log = self.root / "fake.log"
        self.home.mkdir()
        self.bin.mkdir()
        (self.home / ".local" / "bin").mkdir(parents=True)
        ext_dir = self.root / "ext"
        ext_dir.mkdir()
        (ext_dir / "node_modules").symlink_to(TYPEBOX_MODULES)
        self.ext = ext_dir / "agent-context-mcp.ts"
        shutil.copyfile(EXTENSION, self.ext)
        (ext_dir / "package.json").write_text('{"type": "module"}\n')
        (ext_dir / "runner.mjs").write_text(RUNNER_JS)
        self.runner = ext_dir / "runner.mjs"
        self.relay = self.home / ".local" / "bin" / "agent-context"
        self.relay_source = self.root / "relay-source.py"
        self.write_fake(self.relay_source, "relay")
        self.write_fake(self.bin / "uv", "uv", executable=True)
        self.log.write_text("")

    def write_fake(self, path, role, executable=False):
        path.write_text(FAKE_SERVER.replace("__PYTHON__", sys.executable)
                        .replace("__ROLE__", role))
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def install_relay(self, executable=True):
        shutil.copyfile(self.relay_source, self.relay)
        self.relay.chmod(0o755 if executable else 0o644)

    def launches(self):
        rows = []
        for line in self.log.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows

    def run(self, scenario=None, extra_env=None):
        env = dict(os.environ)
        env.update({"HOME": str(self.home), "FAKE_LOG": str(self.log),
                    "PATH": "%s:%s" % (self.bin, os.environ.get("PATH", ""))})
        env.update(extra_env or {})
        proc = subprocess.run(["node", str(self.runner), str(self.ext),
                               json.dumps(scenario or {})],
                              capture_output=True, text=True, env=env, timeout=90,
                              cwd=str(self.root))
        try:
            doc = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            doc = None
        return proc, doc

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def uv_argv(box):
    return ["run", "--directory", str(box.home / ".agent-context" / "server"), "agent-context"]


def first_text(doc, i=0):
    try:
        return doc["calls"][i]["content"][0]["text"]
    except (TypeError, KeyError, IndexError):
        return None


def criterion_1_and_3():
    print("1. an executable relay is started directly; 3. the bridge behaves the same")
    box = Sandbox()
    try:
        box.install_relay()
        proc, doc = box.run()
        rows = box.launches()
        check("the relay is spawned with no arguments and uv is not",
              [r["role"] for r in rows] == ["relay"] and rows[0]["argv"] == [],
              "launches=%s rc=%s err=%s" % (rows, proc.returncode, proc.stderr))
        check("tools/list registers the server's tools and tools/call returns its text",
              doc is not None and doc["tools"] == ["ping"]
              and first_text(doc) == "pong from relay",
              "doc=%s out=%s err=%s" % (doc, proc.stdout, proc.stderr))
        check("the ready notice reports the tool count",
              doc is not None and any("1 tool" in m for _l, m in doc["notes"]),
              doc and doc["notes"])
    finally:
        box.cleanup()


def criterion_2_and_3():
    print("2. no usable relay falls back to uv; 3. the bridge behaves the same")
    cases = []
    box = Sandbox()
    cases.append(("no relay file", box, lambda: None))
    box2 = Sandbox()
    cases.append(("a relay that is not executable", box2, lambda: box2.install_relay(False)))
    box3 = Sandbox()

    def broken_link():
        box3.relay.symlink_to(box3.root / "does-not-exist")
    cases.append(("a broken symlink", box3, broken_link))
    box4 = Sandbox()

    def directory():
        box4.relay.mkdir()
    cases.append(("a directory named agent-context", box4, directory))
    try:
        for label, box, prepare in cases:
            prepare()
            proc, doc = box.run()
            rows = box.launches()
            check("%s: uv is spawned with the clone arguments" % label,
                  [r["role"] for r in rows] == ["uv"] and rows[0]["argv"] == uv_argv(box),
                  "launches=%s rc=%s err=%s" % (rows, proc.returncode, proc.stderr))
            check("%s: tools register and a call returns" % label,
                  doc is not None and doc["tools"] == ["ping"]
                  and first_text(doc) == "pong from uv",
                  "doc=%s out=%s err=%s" % (doc, proc.stdout, proc.stderr))
    finally:
        for _label, box, _prepare in cases:
            box.cleanup()


def criterion_4():
    print("4. the environment passes through and the extension reads no credentials")
    box = Sandbox()
    try:
        box.install_relay()
        proc, doc = box.run(extra_env={"BATTERY_PROBE": "probe-value-1",
                                       "AGENT_CONTEXT_TOKEN": "tok-secret-for-battery",
                                       "AGENT_CONTEXT_HOST": "battery.invalid"})
        rows = box.launches()
        check("the child sees the parent's environment unchanged",
              rows and rows[0]["probe"] == "probe-value-1", rows)
        check("no token or host value appears in the extension's own output or notices",
              doc is not None and "tok-secret-for-battery" not in proc.stdout + proc.stderr
              and "battery.invalid" not in json.dumps(doc["notes"]),
              "out=%s err=%s" % (proc.stdout, proc.stderr))
        source = EXTENSION.read_text(encoding="utf-8")
        check("the extension source never reads AGENT_CONTEXT_TOKEN or AGENT_CONTEXT_HOST",
              "AGENT_CONTEXT_TOKEN" not in source and "AGENT_CONTEXT_HOST" not in source)
    finally:
        box.cleanup()


def criterion_5():
    print("5. the command is chosen again at each respawn")
    box = Sandbox()
    try:
        
        
        proc, doc = box.run(
            scenario={"installAfterFirstCall": {"from": str(box.relay_source),
                                                "to": str(box.relay)}},
            extra_env={"FAKE_EXIT_AFTER_CALL": "1"})
        roles = [r["role"] for r in box.launches()]
        check("the first start is uv and the respawn after installing the relay is the relay",
              roles == ["uv", "relay"] and first_text(doc, 0) == "pong from uv"
              and first_text(doc, 1) == "pong from relay",
              "roles=%s doc=%s err=%s" % (roles, doc, proc.stderr))
    finally:
        box.cleanup()


def criterion_6():
    print("6. shipped through materialize_pi_extensions")
    spec = importlib.util.spec_from_file_location("harness_materialize", MATERIALIZER)
    check("harness-materialize.py is importable", spec is not None and spec.loader is not None)
    if spec is None or spec.loader is None:
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("PI_EXTENSIONS still names agent-context-mcp.ts",
          "agent-context-mcp.ts" in getattr(mod, "PI_EXTENSIONS", ()),
          getattr(mod, "PI_EXTENSIONS", None))
    header = EXTENSION.read_text(encoding="utf-8")[:2500]
    check("the header still says the store is the canonical source",
          "CANONICAL SOURCE IS THE AGENT-CONTEXT STORE" in header)


def main():
    if not TYPEBOX_MODULES.is_dir() or shutil.which("node") is None:
        print("  skip  node or pi's typebox install is missing (%s)" % TYPEBOX_MODULES)
        return
    criterion_1_and_3()
    criterion_2_and_3()
    criterion_4()
    criterion_5()
    criterion_6()


if __name__ == "__main__":
    main()
    failed = results.count(False)
    print("\n%d passed, %d failed" % (results.count(True), failed))
    sys.exit(1 if failed else 0)
