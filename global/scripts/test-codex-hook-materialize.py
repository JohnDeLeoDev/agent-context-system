#!/usr/bin/env python3
'Acceptance battery for Codex hook parity materialization.'
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATERIALIZER = ROOT / "scripts" / "harness-materialize.py"
ADAPTER = ROOT / "scripts" / "codex-hook-adapter.py"


def load_materializer():
    spec = importlib.util.spec_from_file_location("harness_materialize", MATERIALIZER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sample_hooks() -> dict:
    return {
        "SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": "/bin/start", "timeout": 20},
        ]}],
        "PreToolUse": [{"matcher": "Bash|Edit", "hooks": [
            {"type": "command", "command": "/bin/pre", "timeout": 10},
        ]}],
        "PostToolUse": [{"hooks": [
            {"type": "command", "command": "/bin/post", "timeout": 30},
        ]}],
        "PostToolUseFailure": [{"hooks": [
            {"type": "command", "command": "/bin/failure", "timeout": 40},
        ]}],
        "Notification": [{"hooks": [
            {"type": "command", "command": "/bin/notify", "timeout": 5},
        ]}],
        "Stop": [{"hooks": [
            {"type": "command", "command": "/bin/stop", "timeout": 60, "async": True},
        ]}],
        "StopFailure": [],
    }


def test_render_is_dynamic_and_non_mutating() -> None:
    hm = load_materializer()
    source = sample_hooks()
    before = copy.deepcopy(source)
    rendered, unsupported = hm.render_codex_hooks(
        source, adapter=str(ADAPTER), interpreter=sys.executable
    )
    check(source == before, "render changed Claude hook input")
    check(not unsupported, f"unexpected unsupported hooks: {unsupported}")
    hooks = rendered["hooks"]
    check(set(hooks) == {"SessionStart", "PreToolUse", "PostToolUse",
                         "PermissionRequest", "Stop"},
          f"wrong Codex event set: {sorted(hooks)}")
    post_commands = [h["command"] for g in hooks["PostToolUse"] for h in g["hooks"]]
    check(any("PostToolUse " in c and "/bin/post" in c for c in post_commands),
          "success post-tool chain was not translated")
    check(any("PostToolUseFailure " in c and "/bin/failure" in c for c in post_commands),
          "failure post-tool chain was not translated")
    notify = hooks["PermissionRequest"][0]["hooks"][0]
    check("Notification " in notify["command"] and "/bin/notify" in notify["command"],
          "notification chain was not translated")
    stop = hooks["Stop"][0]["hooks"][0]
    check(stop["async"] is True and stop["timeout"] == 60,
          "handler behavior was not preserved")


def test_unsupported_nonempty_event_is_reported() -> None:
    hm = load_materializer()
    source = sample_hooks()
    source["StopFailure"] = [{"hooks": [
        {"type": "command", "command": "/bin/stop-failure"},
    ]}]
    _rendered, unsupported = hm.render_codex_hooks(
        source, adapter=str(ADAPTER), interpreter=sys.executable
    )
    check(unsupported == ["StopFailure"],
          f"unsupported hook disappeared silently: {unsupported}")


def run_adapter(event: str, payload: dict, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ADAPTER), "--event", event, "--", command],
        input=json.dumps(payload), text=True, capture_output=True, timeout=10,
    )


def test_adapter_routes_post_tool_success_and_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        sink = Path(td) / "sink.py"
        sink.write_text(
            "import json,sys\n"
            "p=json.load(sys.stdin)\n"
            "print(json.dumps({'event':p.get('hook_event_name'),'tool':p.get('tool_name')}))\n"
        )
        command = f"{sys.executable} {sink}"
        success = {"hook_event_name": "PostToolUse", "tool_name": "exec_command",
                   "tool_response": {"is_error": False}}
        failure = {"hook_event_name": "PostToolUse", "tool_name": "exec_command",
                   "tool_response": {"is_error": True, "error": "boom"}}
        r = run_adapter("PostToolUse", success, command)
        check(r.returncode == 0 and '"event": "PostToolUse"' in r.stdout,
              f"success route failed: {r.returncode} {r.stderr}")
        r = run_adapter("PostToolUseFailure", success, command)
        check(r.returncode == 0 and not r.stdout.strip(),
              "failure hook ran for a successful tool")
        r = run_adapter("PostToolUse", failure, command)
        check(r.returncode == 0 and not r.stdout.strip(),
              "success hook ran for a failed tool")
        r = run_adapter("PostToolUseFailure", failure, command)
        check(r.returncode == 0 and '"event": "PostToolUseFailure"' in r.stdout,
              f"failure route failed: {r.returncode} {r.stderr}")


def test_adapter_translates_permission_request_to_notification() -> None:
    with tempfile.TemporaryDirectory() as td:
        sink = Path(td) / "sink.py"
        sink.write_text(
            "import json,sys\n"
            "p=json.load(sys.stdin)\n"
            "print(json.dumps({'event':p.get('hook_event_name'),'type':p.get('notification_type')}))\n"
        )
        r = run_adapter(
            "Notification",
            {"hook_event_name": "PermissionRequest", "tool_name": "exec_command"},
            f"{sys.executable} {sink}",
        )
        check(r.returncode == 0 and '"event": "Notification"' in r.stdout,
              f"permission translation failed: {r.returncode} {r.stderr}")
        check('"type": "permission_prompt"' in r.stdout,
              "permission request lacks Claude notification type")


def test_materialize_changes_only_codex_files() -> None:
    hm = load_materializer()
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        claude = home / ".claude" / "settings.json"
        codex = home / ".codex" / "hooks" / "hooks.json"
        other = home / ".copilot" / "settings.json"
        claude.parent.mkdir(parents=True)
        other.parent.mkdir(parents=True)
        claude.write_text(json.dumps({"hooks": sample_hooks()}))
        other.write_text('{"sentinel":true}\n')
        report: dict = {}
        unsupported = hm.materialize_codex_hooks(
            report,
            home=str(home),
            claude_settings=str(claude),
            codex_hooks=str(codex),
            adapter=str(ADAPTER),
            interpreter=sys.executable,
        )
        check(not unsupported, f"unexpected unsupported hooks: {unsupported}")
        first = codex.read_bytes()
        check(other.read_text() == '{"sentinel":true}\n',
              "Codex materialization changed another harness")
        hm.materialize_codex_hooks(
            report,
            home=str(home),
            claude_settings=str(claude),
            codex_hooks=str(codex),
            adapter=str(ADAPTER),
            interpreter=sys.executable,
        )
        check(codex.read_bytes() == first, "Codex materialization is not idempotent")
        claude.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
        hm.materialize_codex_hooks(
            report,
            home=str(home),
            claude_settings=str(claude),
            codex_hooks=str(codex),
            adapter=str(ADAPTER),
            interpreter=sys.executable,
        )
        check(json.loads(codex.read_text()) == {"hooks": {}},
              "retired Claude hooks remained active in Codex")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"test-codex-hook-materialize: {len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
