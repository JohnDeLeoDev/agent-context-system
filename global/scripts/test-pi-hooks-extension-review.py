#!/usr/bin/env python3
'Regression tests for the adversarial review of the pi hooks extension (chunk 3b).\n\nReuses the helpers of test-pi-hooks-extension.py. Each test is a break the reviewer\nfound and this session reproduced against the first implementation.'
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("base", HERE / "test-pi-hooks-extension.py")
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

check = base.check

RUNNER_DESC_JS = r"""
const mod = await import(process.argv[2]);
const scenario = JSON.parse(process.argv[3]);
const handlers = {}, commands = {};
const pi = { on: (e, h) => { (handlers[e] ||= []).push(h); },
  registerCommand: (n, o) => { commands[n] = o.description; },
  registerTool() {}, sendMessage() {}, sendUserMessage() {} };
await mod.default(pi);
const ctx = { cwd: scenario.cwd, hasUI: false, sessionManager: { getSessionId: () => "s1" },
  ui: { notify() {}, confirm: async () => false, setStatus() {} } };
for (const step of scenario.steps) {
  for (const h of handlers[step.event] || []) await h(step.payload, ctx);
}
console.log(JSON.stringify({ events: Object.keys(handlers), commands }));
"""


def fake(body: str) -> str:
    return base.write_guard(base.tmpdir(), "hook-dispatch.py", body)


def pre(dispatcher: str, cmd: str = "ls", cwd: str | None = None) -> dict | None:
    src = base.render_fixture(dispatcher=dispatcher)
    return base.run_extension(src, [base.bash_call(cmd)], cwd or str(base.tmpdir()))["results"][0]


def test_large_output_still_blocks() -> None:
    d = fake("import sys\nsys.stdout.write('x' * 3000000)\nsys.stderr.write('nope')\nsys.exit(2)\n")
    res = pre(d)
    check(res is not None and res.get("block") is True, f"3 MB output disabled the block: {res}")


def test_json_after_text_lines_is_read() -> None:
    d = fake("import json\nprint('warning line')\nprint(json.dumps({'hookSpecificOutput': "
             "{'permissionDecision': 'deny', 'permissionDecisionReason': 'J'}}))\n")
    res = pre(d)
    check(res is not None and res.get("block") is True, f"JSON after a text line was ignored: {res}")


def test_exit_2_keeps_the_json_reason() -> None:
    d = fake("import json, sys\nprint(json.dumps({'hookSpecificOutput': "
             "{'permissionDecision': 'deny', 'permissionDecisionReason': 'FROMJSON'}}))\nsys.exit(2)\n")
    res = pre(d)
    check(res is not None and "FROMJSON" in res.get("reason", ""), f"JSON reason dropped: {res}")


def test_ask_without_ui_blocks() -> None:
    d = fake("import json\nprint(json.dumps({'hookSpecificOutput': "
             "{'permissionDecision': 'ask', 'permissionDecisionReason': 'asky'}}))\n")
    res = pre(d)
    check(res is not None and res.get("block") is True and "asky" in res.get("reason", ""),
          f"ask was allowed with no UI to answer it: {res}")


def test_dispatcher_runs_in_the_session_cwd() -> None:
    want = str(Path.home() / ".cache")
    d = fake(f"import os, sys\nsys.exit(0 if os.getcwd() == {want!r} else 2)\n")
    res = pre(d, cwd=want)
    check(res is None, f"dispatcher cwd differs from the session cwd: {res}")


def test_stray_command_file_keeps_hooks_alive() -> None:
    d = base.tmpdir()
    cmds = d / "commands"
    cmds.mkdir()
    (cmds / "dead.md").symlink_to(d / "missing.md")
    (cmds / "good.md").write_text("---\ndescription: Good\n---\nbody\n")
    guard = base.write_guard(d, "deny.py", "import sys\nsys.exit(2)\n")
    src = base.render_fixture(dispatcher=guard, commands=str(cmds))
    out = base.run_extension(src, [base.bash_call("ls")], str(d))
    check("good" in out["commands"], f"a good command was lost to a bad one: {out['commands']}")
    check(out["results"][0] and out["results"][0].get("block") is True,
          "the hooks stopped working after a stray command file")


def run_desc(src: str, steps: list[dict], cwd: str) -> dict:
    d = base.tmpdir()
    (d / "ext.ts").write_text(src)
    (d / "run.mjs").write_text(RUNNER_DESC_JS)
    proc = subprocess.run(["node", str(d / "run.mjs"), str(d / "ext.ts"),
                           json.dumps({"cwd": cwd, "steps": steps})],
                          capture_output=True, text=True, timeout=60)
    check(proc.returncode == 0, f"node failed: {proc.stderr[-400:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_crlf_and_quoted_description_are_clean() -> None:
    d = base.tmpdir()
    cmds = d / "commands"
    cmds.mkdir()
    (cmds / "crlf.md").write_bytes(b'---\r\ndescription: "quoted"\r\n---\r\nbody\r\n')
    guard = base.write_guard(d, "ok.py", "import sys\nsys.exit(0)\n")
    src = base.render_fixture(dispatcher=guard, commands=str(cmds))
    out = run_desc(src, [], str(d))
    check(out["commands"].get("crlf") == "quoted",
          f"description not cleaned: {out['commands'].get('crlf')!r}")


def test_reload_does_not_send_session_end() -> None:
    d = base.tmpdir()
    log = d / "events.log"
    guard = base.write_guard(d, "log.py",
                             "import sys, json\np = json.load(sys.stdin)\n"
                             f"open({str(log)!r}, 'a').write(p['hook_event_name'] + '\\n')\n")
    reg = base.registry_file(d, {e: [("", guard)] for e in base.dispatcher_events()})
    src = base.render_fixture()
    steps = [{"event": "session_shutdown", "payload": {"reason": "reload"}}]
    base.run_extension(src, steps, str(d), registry=reg)
    seen = log.read_text().split() if log.exists() else []
    check("SessionEnd" not in seen, f"reload sent SessionEnd: {seen}")


def test_corrupt_ownership_manifest_does_not_raise() -> None:
    hm = base.load_module(base.MATERIALIZER, "harness_materialize")
    mat = base.need(hm, "materialize_pi_hooks")
    home = base.tmpdir()
    ext_dir = home / ".pi" / "agent" / "extensions"
    ext_dir.mkdir(parents=True)
    (ext_dir / ".agent-context-extensions.json").write_text('[["x"], 1]')
    setattr(hm, "PI_EXT_DIR", str(ext_dir))
    mat({})
    check((ext_dir / "agent-context-hooks.ts").exists(), "no extension after a corrupt manifest")


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
