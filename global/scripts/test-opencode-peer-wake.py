#!/usr/bin/env python3
"test-opencode-peer-wake: the opencode plugin's peer-message watcher (policy).\n\nThe plugin text is OPENCODE_GUARD_PLUGIN in harness-materialize.py. This renders it with\nempty guard lists, loads it under node with a stand-in plugin context, and checks what the\nwatcher does with the files a bridge leaves in the spool (server: peer_wake._wake_opencode):\n\n  - setup makes the spool directory, which is what turns the bridge's wake route on;\n  - a spool left by a service that has exited is removed;\n  - a message waits until a session has been seen in the directory;\n  - it then goes to that session through ctx.session.synthetic, and so does the next one;\n  - the atomic writer's partial file is never read.\n\nThe spool's place must agree with the server: <state>/peer-spool/<pid>/<sha256(dir)[:16]>.\nExit 0 when every check holds, or when node is absent (said on stdout)."
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

DRIVER = r"""
import { mkdirSync, writeFileSync, readdirSync, existsSync } from "node:fs"
import { join } from "node:path"

const [plugin_path, cwd, state] = process.argv.slice(2)
process.env.XDG_STATE_HOME = state
const root = join(state, "agent-context", "peer-spool")
mkdirSync(join(root, "999999991", "x"), { recursive: true })
const plugin = (await import(plugin_path)).default
const hooks = {}
const sent = []
const ctx = {
  location: { directory: cwd },
  tool: { hook: async (n, f) => { hooks[n] = f } },
  session: { hook: async (n, f) => { hooks["session." + n] = f },
             synthetic: async (a) => { sent.push(a) } },
}
await plugin.setup(ctx)
const wait = (ms) => new Promise((r) => setTimeout(r, ms))
const mine = join(root, String(process.pid))
const out = { pid: process.pid, dirs: existsSync(mine) ? readdirSync(mine) : [],
              pruned: !existsSync(join(root, "999999991")) }
const dir = join(mine, out.dirs[0] ?? "missing")
writeFileSync(join(dir, "1-a.json"), JSON.stringify({ text: "first" }))
await wait(300)
out.before = { sent: sent.length, waiting: readdirSync(dir).length }
await hooks["session.context"]({ sessionID: "ses_1", system: [] })
await wait(300)
out.after = { sent: [...sent], waiting: readdirSync(dir).length }
writeFileSync(join(dir, "2-b.json"), JSON.stringify({ text: "second" }))
writeFileSync(join(dir, ".ac-tmp-x.part"), "partial")
await wait(400)
out.later = { sent: sent.map((s) => s.text), left: readdirSync(dir) }
// policy: the store-loaded gate. A lead that has not loaded the store is refused; a
// worker, which cannot load it, is not.
const tryRead = async (agent, sessionID) => {
  try { await hooks["execute.before"]({ tool: "read", sessionID, agent, input: { filePath: "/x" } }); return "allowed" }
  catch (e) { return "refused" }
}
out.gate = { lead: await tryRead("build", "ses_lead"), worker: await tryRead("worker-explore", "ses_worker"),
             unnamed: await tryRead(undefined, "ses_other") }
console.log(JSON.stringify(out))
process.exit(0)
"""

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, "" if ok else "  :: %s" % (detail,)))


def plugin_text():
    spec = importlib.util.spec_from_file_location(
        "harness_materialize", os.path.join(HERE, "harness-materialize.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    empty = json.dumps([])
    return module.OPENCODE_GUARD_PLUGIN.format(
        shell=empty, edit=empty, claude_shell=empty, claude_edit=empty,
        approval=json.dumps(""), drift=json.dumps(""), compress=json.dumps(""),
        brief=json.dumps("rules"),
        launcher=json.dumps(None), python=json.dumps(sys.executable),
        dispatcher=json.dumps(""))


def main():
    node = shutil.which("node")
    if not node:
        print("test-opencode-peer-wake: node not found; nothing run")
        return 0
    tmp = tempfile.mkdtemp(prefix="oc-peer-wake-")
    try:
        cwd = os.path.join(tmp, "project")
        os.makedirs(cwd)
        plugin = os.path.join(tmp, "plugin.mjs")
        driver = os.path.join(tmp, "driver.mjs")
        with open(plugin, "w", encoding="utf-8") as fh:
            fh.write(plugin_text())
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(DRIVER)
        run = subprocess.run([node, driver, plugin, cwd, os.path.join(tmp, "state")],
                             capture_output=True, text=True, timeout=60)
        try:
            out = json.loads(run.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            check("the plugin loads and the driver finishes", False,
                  (run.stderr or run.stdout)[-400:])
            return 1
        want = hashlib.sha256(os.path.realpath(cwd).encode("utf-8")).hexdigest()[:16]
        check("setup makes the spool the server names", out["dirs"] == [want], out["dirs"])
        check("a dead service's spool is removed", out["pruned"])
        check("a message waits while no session has been seen",
              out["before"] == {"sent": 0, "waiting": 1}, out["before"])
        check("it goes to the session once one is seen",
              out["after"] == {"sent": [{"sessionID": "ses_1", "text": "first"}], "waiting": 0},
              out["after"])
        check("the next message is delivered and the partial file is left alone",
              out["later"] == {"sent": ["first", "second"], "left": [".ac-tmp-x.part"]},
              out["later"])
        check("policy: a worker's read is not refused for an unloaded store; a lead's is",
              out["gate"] == {"lead": "refused", "worker": "allowed", "unnamed": "refused"},
              out["gate"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d passed, %d failed" % (sum(RESULTS), len(RESULTS) - sum(RESULTS)))
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
