#!/usr/bin/env python3
'Acceptance battery for the pi-native hooks extension (harness-neutral cleanup, chunk 3b).\n\nDesign: the generated pi extension runs Claude\'s hook dispatcher once per event\n(`python3 <dispatcher> <Event>`, payload on stdin), exactly as Claude does.\n\nContract under test (harness-materialize.py):\n  render_pi_hooks(manifest, home, commands_dir) -> str   TypeScript extension source\n  materialize_pi_hooks(report)                            writes PI_EXT_DIR/agent-context-hooks.ts\nManifest entries bound to pi: kind "dispatcher", claude_event, script, timeout,\nand "pi" in harnesses.'
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MATERIALIZER = SCRIPTS / "harness-materialize.py"
SETTINGS_SYNC = SCRIPTS / "home-settings-sync.py"
MANIFEST = ROOT / "hooks-manifest.json"
HOME = Path.home()
TMP_BASE = HOME / ".cache" / "tmp"
DISPATCHER = HOME / ".agent-context" / "global" / "scripts" / "hook-dispatch.py"
REAL_REGISTRY = HOME / ".agent-context" / "hook-dispatch.json"

EVENT_STEPS = [
    ("session_start", "SessionStart", {"reason": "startup"}),
    ("input", "UserPromptSubmit", {"text": "hi", "source": "interactive"}),
    ("tool_call", "PreToolUse", {"toolName": "bash", "toolCallId": "c1",
                                 "input": {"command": "ls"}}),
    ("tool_result", "PostToolUse", {"toolName": "bash", "toolCallId": "c1",
                                    "input": {"command": "ls"}, "isError": False,
                                    "content": [{"type": "text", "text": "out"}]}),
    ("tool_result", "PostToolUseFailure", {"toolName": "bash", "toolCallId": "c2",
                                           "input": {"command": "false"}, "isError": True,
                                           "content": [{"type": "text", "text": "err"}]}),
    ("user_bash", "PreToolUse", {"command": "ls", "excludeFromContext": False}),
    ("agent_end", "Stop", {"messages": []}),
    ("session_shutdown", "SessionEnd", {}),
]

RUNNER_JS = r"""
const path = process.argv[2];
const scenario = JSON.parse(process.argv[3]);
const mod = await import(path);
const handlers = {}, commands = {};
const pi = {
  on: (e, h) => { (handlers[e] ||= []).push(h); },
  registerCommand: (n, o) => { commands[n] = o; },
  registerTool: () => {},
  sendMessage: () => {},
  sendUserMessage: () => {},
};
await mod.default(pi);
const ctx = { cwd: scenario.cwd, hasUI: false, sessionManager: { getSessionId: () => "s1" },
  ui: { notify() {}, confirm: async () => false, setStatus() {} } };
const out = { events: Object.keys(handlers), commands: Object.keys(commands), results: [] };
for (const step of scenario.steps) {
  let res = null;
  for (const h of handlers[step.event] || []) {
    const r = await h(step.payload, ctx);
    if (r !== undefined && r !== null) { res = r; break; }
  }
  out.results.push(res);
}
console.log(JSON.stringify(out));
"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def need(module, attr: str):
    check(hasattr(module, attr), f"harness-materialize.py has no {attr}()")
    return getattr(module, attr)


def tmpdir() -> Path:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="pi-hooks-"))


def write_guard(directory: Path, name: str, body: str) -> str:
    p = directory / name
    p.write_text(body)
    return str(p)


def dispatcher_events() -> tuple[str, ...]:
    sync = load_module(SETTINGS_SYNC, "home_settings_sync")
    return tuple(sync.DISPATCHED_EVENTS)


def fixture_manifest(dispatcher: str = str(DISPATCHER)) -> dict:
    hooks = {}
    for event in dispatcher_events():
        hooks[f"dispatch-{event}"] = {
            "kind": "dispatcher", "run": "python3", "script": dispatcher,
            "claude_event": event, "blocking": event == "PreToolUse",
            "timeout": 30, "status": "active", "harnesses": ["pi"],
        }
    return {"hooks": hooks}


def registry_file(directory: Path, guards: dict[str, list[tuple[str, str]]]) -> str:
    'guards: event -> [(matcher, script)]. Returns registry path.'
    hooks: dict[str, list] = {}
    for event, entries in guards.items():
        for matcher, script in entries:
            group: dict = {"hooks": [{"type": "command",
                                      "command": f"{sys.executable} {script}",
                                      "timeout": 10}]}
            if matcher:
                group["matcher"] = matcher
            hooks.setdefault(event, []).append(group)
    path = directory / "registry.json"
    path.write_text(json.dumps({"hooks": hooks}))
    return str(path)


def run_extension(source: str, steps: list[dict], cwd: str,
                  registry: str | None = None) -> dict:
    d = tmpdir()
    ext = d / "agent-context-hooks.ts"
    ext.write_text(source)
    runner = d / "run.mjs"
    runner.write_text(RUNNER_JS)
    env = dict(os.environ)
    if registry:
        env["HOOK_DISPATCH_REGISTRY"] = registry
    proc = subprocess.run(
        ["node", str(runner), str(ext), json.dumps({"cwd": cwd, "steps": steps})],
        capture_output=True, text=True, timeout=120, env=env,
    )
    check(proc.returncode == 0, f"node failed: {proc.stderr[-600:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def bash_call(command: str, call_id: str = "c1") -> dict:
    return {"event": "tool_call",
            "payload": {"toolName": "bash", "toolCallId": call_id,
                        "input": {"command": command}}}


def render_fixture(dispatcher: str = str(DISPATCHER), commands: str | None = None) -> str:
    hm = load_module(MATERIALIZER, "harness_materialize")
    render = need(hm, "render_pi_hooks")
    return render(fixture_manifest(dispatcher), str(HOME),
                  commands or str(tmpdir() / "commands"))




def test_manifest_pi_events_equal_dispatched_events() -> None:
    manifest = json.loads(MANIFEST.read_text())
    pi_events = {h["claude_event"] for h in manifest["hooks"].values()
                 if "pi" in h.get("harnesses", []) and h.get("kind") == "dispatcher"}
    check(pi_events == set(dispatcher_events()),
          f"pi dispatcher events {sorted(pi_events)} != DISPATCHED_EVENTS "
          f"{sorted(dispatcher_events())}")


def test_pi_manifest_entries_are_complete() -> None:
    manifest = json.loads(MANIFEST.read_text())
    bad = []
    for name, h in manifest["hooks"].items():
        if "pi" not in h.get("harnesses", []):
            continue
        for key in ("kind", "claude_event", "script", "timeout"):
            if key not in h:
                bad.append(f"{name}.{key}")
    check(any("pi" in h.get("harnesses", []) for h in manifest["hooks"].values()),
          "no manifest entry lists pi")
    check(not bad, f"pi-bound manifest entries missing fields: {bad}")




def test_render_has_no_claude_dir_paths_of_its_own() -> None:
    d = tmpdir()
    guard_dir = d / "scripts"
    guard_dir.mkdir()
    src = render_fixture(dispatcher=str(guard_dir / "hook-dispatch.py"))
    check("export default" in src, "generated source has no default export")
    check(".claude" not in src, "generated extension hardcodes a .claude path")


def test_materialize_idempotent_and_foreign_safe() -> None:
    hm = load_module(MATERIALIZER, "harness_materialize")
    mat = need(hm, "materialize_pi_hooks")
    home = tmpdir()
    ext_dir = home / ".pi" / "agent" / "extensions"
    ext_dir.mkdir(parents=True)
    setattr(hm, "PI_EXT_DIR", str(ext_dir))
    target = ext_dir / "agent-context-hooks.ts"
    mat({})
    check(target.exists(), "first run wrote no extension")
    first = target.read_text()
    r2: dict = {}
    mat(r2)
    check(target.read_text() == first, "second run changed the file")
    check(not r2.get("pi"), f"second run reported a change: {r2}")
    check((ext_dir / ".agent-context-extensions.json").exists()
          and "agent-context-hooks.ts" in (ext_dir / ".agent-context-extensions.json").read_text(),
          "ownership manifest does not list the extension")
    foreign_dir = tmpdir() / ".pi" / "agent" / "extensions"
    foreign_dir.mkdir(parents=True)
    (foreign_dir / "agent-context-hooks.ts").write_text("// hand placed\n")
    setattr(hm, "PI_EXT_DIR", str(foreign_dir))
    mat({})
    check((foreign_dir / "agent-context-hooks.ts").read_text() == "// hand placed\n",
          "a hand-placed extension was overwritten")




def test_registers_every_mapped_pi_event() -> None:
    src = render_fixture()
    out = run_extension(src, [], str(tmpdir()))
    want = {"tool_call", "tool_result", "user_bash", "input", "agent_end",
            "session_start", "session_shutdown"}
    missing = sorted(want - set(out["events"]))
    check(not missing, f"pi events with no handler: {missing}")


def test_each_pi_event_invokes_its_claude_event() -> None:
    d = tmpdir()
    log = d / "events.log"
    guard = write_guard(d, "log.py",
                        "import sys, json\np = json.load(sys.stdin)\n"
                        f"open({str(log)!r}, 'a').write(p['hook_event_name'] + '\\n')\n")
    reg = registry_file(d, {e: [("", guard)] for e in dispatcher_events()})
    src = render_fixture()
    steps = [{"event": e, "payload": p} for e, _c, p in EVENT_STEPS]
    run_extension(src, steps, str(d), registry=reg)
    seen = set(log.read_text().split()) if log.exists() else set()
    want = {claude for _e, claude, _p in EVENT_STEPS}
    check(want <= seen, f"Claude events never dispatched: {sorted(want - seen)}")




def test_exit_2_blocks_and_exit_0_allows() -> None:
    d = tmpdir()
    guard = write_guard(d, "deny.py",
                        "import sys, json\np = json.load(sys.stdin)\n"
                        "if 'blockme' in p['tool_input']['command']:\n"
                        "    sys.stderr.write('nope')\n    sys.exit(2)\n")
    reg = registry_file(d, {"PreToolUse": [("Bash", guard)]})
    out = run_extension(render_fixture(),
                        [bash_call("echo blockme"), bash_call("ls")], str(d), registry=reg)
    blocked, allowed = out["results"]
    check(blocked and blocked.get("block") is True, f"exit 2 did not block: {blocked}")
    check("nope" in blocked.get("reason", ""), f"reason lost: {blocked}")
    check(allowed is None, f"exit 0 did not allow: {allowed}")


def test_json_deny_blocks() -> None:
    d = tmpdir()
    guard = write_guard(d, "jdeny.py",
                        "import json\nprint(json.dumps({'hookSpecificOutput': "
                        "{'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', "
                        "'permissionDecisionReason': 'jd'}}))\n")
    reg = registry_file(d, {"PreToolUse": [("Bash", guard)]})
    res = run_extension(render_fixture(), [bash_call("ls")], str(d), registry=reg)["results"][0]
    check(res and res.get("block") is True and "jd" in res.get("reason", ""),
          f"JSON deny did not block: {res}")


def test_payload_uses_claude_names() -> None:
    d = tmpdir()
    guard = write_guard(d, "shape.py",
                        "import sys, json\np = json.load(sys.stdin)\n"
                        "ok = (p['tool_name'] == 'Bash' and 'command' in p['tool_input']"
                        " and p['hook_event_name'] == 'PreToolUse' and p.get('cwd'))\n"
                        "sys.exit(0 if ok else 2)\n")
    reg = registry_file(d, {"PreToolUse": [("Bash", guard)]})
    res = run_extension(render_fixture(), [bash_call("ls")], str(d), registry=reg)["results"][0]
    check(res is None, f"payload not in Claude shape (guard refused it): {res}")


def test_matcher_is_left_to_the_dispatcher() -> None:
    d = tmpdir()
    guard = write_guard(d, "deny.py", "import sys\nsys.exit(2)\n")
    reg = registry_file(d, {"PreToolUse": [("Bash", guard)]})
    read_call = {"event": "tool_call",
                 "payload": {"toolName": "read", "toolCallId": "c2",
                             "input": {"path": "/x"}}}
    res = run_extension(render_fixture(), [read_call], str(d), registry=reg)["results"][0]
    check(res is None, f"a Bash-only guard blocked a Read: {res}")


def test_dispatcher_failure_fails_open_like_claude() -> None:
    d = tmpdir()
    crash = write_guard(d, "crash_dispatch.py", "raise SystemExit(1)\n")
    for label, script in (("crashing", crash), ("missing", str(d / "nope.py"))):
        src = render_fixture(dispatcher=script)
        res = run_extension(src, [bash_call("ls")], str(d))["results"][0]
        check(res is None, f"{label} dispatcher blocked instead of failing open: {res}")


def test_post_tool_context_reaches_the_result() -> None:
    d = tmpdir()
    guard = write_guard(d, "ctx.py",
                        "import json\nprint(json.dumps({'hookSpecificOutput': "
                        "{'hookEventName': 'PostToolUse', "
                        "'additionalContext': 'CTX-MARK'}}))\n")
    reg = registry_file(d, {"PostToolUse": [("Bash", guard)]})
    step = {"event": "tool_result",
            "payload": {"toolName": "bash", "toolCallId": "c1",
                        "input": {"command": "ls"}, "isError": False,
                        "content": [{"type": "text", "text": "out"}]}}
    res = run_extension(render_fixture(), [step], str(d), registry=reg)["results"][0]
    check(res is not None and "CTX-MARK" in json.dumps(res),
          f"PostToolUse additionalContext lost: {res}")




def test_commands_registered_from_store_dir() -> None:
    d = tmpdir()
    cmds = d / "commands"
    cmds.mkdir()
    (cmds / "alpha.md").write_text("---\ndescription: Alpha\n---\nDo alpha $ARGUMENTS\n")
    src = render_fixture(commands=str(cmds))
    out = run_extension(src, [], str(d))
    check("alpha" in out["commands"], f"command not registered: {out['commands']}")




def _real_manifest_src() -> str:
    hm = load_module(MATERIALIZER, "harness_materialize")
    render = need(hm, "render_pi_hooks")
    return render(json.loads(MANIFEST.read_text()), str(HOME), str(ROOT / "commands"))


def test_real_dispatcher_blocks_git_stash() -> None:
    check(REAL_REGISTRY.exists(), "real registry missing on this machine")
    res = run_extension(_real_manifest_src(), [bash_call("git stash")], str(HOME))["results"][0]
    check(res and res.get("block") is True, f"git stash was not blocked: {res}")


def test_real_dispatcher_blocks_write_outside_home() -> None:
    call = {"event": "tool_call",
            "payload": {"toolName": "write", "toolCallId": "c3",
                        "input": {"path": "/etc/agent-test-not-real", "content": "x"}}}
    res = run_extension(_real_manifest_src(), [call], str(HOME))["results"][0]
    check(res and res.get("block") is True, f"write outside $HOME not blocked: {res}")


def test_real_dispatcher_allows_a_harmless_command() -> None:
    res = run_extension(_real_manifest_src(), [bash_call("ls -la")], str(HOME))["results"][0]
    check(res is None, f"harmless ls was blocked: {res}")




def test_other_harness_entries_unchanged() -> None:
    manifest = json.loads(MANIFEST.read_text())
    for name in ("block-git-stash", "guard-git-write", "require-worktree-edit",
                 "plain-language-check", "block-write-outside-home"):
        check(name in manifest["hooks"], f"guard {name} vanished from manifest")
    stash = manifest["hooks"]["block-git-stash"]
    check({"copilot", "antigravity"} <= set(stash["harnesses"]),
          "block-git-stash lost copilot/antigravity")
    check("pi" not in stash["harnesses"],
          "per-script guards must not also be wired to pi (the dispatcher runs them)")
    check(manifest["hooks"]["harness-materialize"]["script"].endswith("harness-materialize.py"),
          "harness-materialize entry changed")


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
