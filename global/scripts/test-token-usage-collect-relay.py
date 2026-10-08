#!/usr/bin/env python3
"Battery for token-usage-collect.py's upload behavior.\n\nTransport (HTTP, bearer header, JSON-RPC, redirects, the size cap, the deadline) is store_mcp's\nown contract and is covered by test-store-mcp.py; this battery runs the sibling\ntoken-usage-collect.py in this directory as a subprocess, with store_mcp.call intercepted via a\nsitecustomize.py shim (the subprocess never reaches a real daemon), and checks token-usage-\ncollect's own contract: the tool name and arguments it sends, and the never-raise / leave-cache-\nstale-on-failure discipline around upload_rollups. No case touches the real home."
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token-usage-collect.py")
TMP_BASE = os.path.expanduser("~/.cache/tmp")

TOKEN = "tok-relay-8b21f4-not-a-real-secret"
PLACEHOLDER_UUID = "00000000-0000-4000-8000-000000000000"
TIMEOUT_CEILING_SECONDS = 30.0

passed = 0
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))







SITECUSTOMIZE = '''
import ast, json, os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
import store_mcp

_log = os.environ.get("CALL_LOG")
_behavior_path = os.environ.get("CALL_BEHAVIOR")


def _fake_call(tool, arguments=None, env=None):
    if _log:
        with open(_log, "a") as fh:
            fh.write(json.dumps({"tool": tool, "arguments": arguments or {}}) + "\\n")
    behavior = {"result": {"machine_uuid": "unknown-machine", "written": True}}
    if _behavior_path and os.path.exists(_behavior_path):
        with open(_behavior_path) as fh:
            behavior = ast.literal_eval(fh.read())
    if behavior.get("raise") == "ToolError":
        raise store_mcp.ToolError(behavior.get("message", "boom"))
    if behavior.get("raise") == "StoreUnreachable":
        raise store_mcp.StoreUnreachable(behavior.get("message", "unreachable"))
    return behavior["result"]


store_mcp.call = _fake_call
'''



def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def usage_line(day: str, request_id: str, session: str = "s1") -> dict:
    return {
        "type": "assistant",
        "timestamp": f"{day}T00:00:00Z",
        "sessionId": session,
        "cwd": "/home/example/project",
        "gitBranch": "main",
        "effort": "medium",
        "isSidechain": False,
        "requestId": request_id,
        "message": {
            "model": "claude-sonnet-5",
            "id": "msg-" + request_id,
            "content": [],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            },
        },
    }


class Fixture:
    def __init__(self) -> None:
        os.makedirs(TMP_BASE, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="tucrelay.", dir=TMP_BASE)
        self.home = os.path.join(self.root, "home")
        self.store = os.path.join(self.home, ".agent-context")
        self.hook_dir = os.path.join(self.root, "pyhook")
        self.call_log = os.path.join(self.root, "calls.jsonl")
        os.makedirs(self.home)
        write_text(os.path.join(self.hook_dir, "sitecustomize.py"), SITECUSTOMIZE)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def seed_transcript(self, records: list[dict], name: str = "session") -> str:
        path = os.path.join(self.home, ".claude", "projects", f"{name}.jsonl")
        text = "".join(json.dumps(r) + "\n" for r in records)
        write_text(path, text)
        return path

    def mark_checkout(self) -> None:
        os.makedirs(os.path.join(self.store, ".git"), exist_ok=True)

    def env_file(self, text: str) -> None:
        write_text(os.path.join(self.home, ".config", "agent-context", "env"), text)

    def rollup_dir(self) -> str:
        return os.path.join(self.home, ".cache", "agent-context", "token-rollup")

    def store_rollup_dir(self, uuid: str = "unknown-machine") -> str:
        return os.path.join(self.store, "machines", uuid, "token-usage")

    def month_path(self, month: str, *, checkout: bool = False, uuid: str = "unknown-machine") -> str:
        base = self.store_rollup_dir(uuid) if checkout else self.rollup_dir()
        return os.path.join(base, f"{month}.json")

    def read_month(self, month: str, **kw) -> dict | None:
        try:
            with open(self.month_path(month, **kw), encoding="utf-8") as fh:
                return json.load(fh)
        except OSError:
            return None

    def machines_path(self, *parts: str) -> str:
        return os.path.join(self.store, "machines", *parts)

    def set_behavior(self, behavior: dict) -> None:
        self.behavior_path = os.path.join(self.root, "behavior.py")
        write_text(self.behavior_path, repr(behavior))

    def calls(self) -> list[dict]:
        try:
            with open(self.call_log, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []


@dataclass
class Result:
    rc: int
    out: str
    err: str
    elapsed: float

    @property
    def text(self) -> str:
        return self.out + self.err


def run_script(fx: Fixture, *, relay: str | None, extra_args: list[str] = []) -> Result:
    'relay: "token" (host+port+token), "no-token" (host+port, no token), or None.'
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": fx.home, "LANG": "C.UTF-8",
           "PYTHONPATH": fx.hook_dir, "CALL_LOG": fx.call_log,
           "SCRIPT_DIR": os.path.dirname(SCRIPT)}
    if getattr(fx, "behavior_path", None):
        env["CALL_BEHAVIOR"] = fx.behavior_path
    if relay == "token":
        env.update(AGENT_CONTEXT_HOST="127.0.0.1", AGENT_CONTEXT_PORT="1", AGENT_CONTEXT_TOKEN=TOKEN)
    elif relay == "no-token":
        env.update(AGENT_CONTEXT_HOST="127.0.0.1", AGENT_CONTEXT_PORT="1")
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, SCRIPT, *extra_args], cwd=fx.home, env=env,
                              capture_output=True, text=True, timeout=TIMEOUT_CEILING_SECONDS + 15)
        return Result(proc.returncode, proc.stdout, proc.stderr, time.time() - started)
    except subprocess.TimeoutExpired:
        return Result(-1, "", "TIMEOUT", time.time() - started)




def case_no_checkout_relocates_rollup() -> None:
    print("T3 rollup lands under ~/.cache/agent-context/token-rollup; no relay env means the local daemon")
    fx = Fixture()
    try:
        fx.seed_transcript([usage_line("2026-08-15", "r1")])
        result = run_script(fx, relay=None)
        month = fx.read_month("2026-08")
        check("rollup written under the cache dir", month is not None, result.text[-300:])
        check("nothing written under the store's machines/ tree",
              not os.path.exists(fx.machines_path()), "")
        check("uploaded to the local daemon (store_mcp's default)", len(fx.calls()) == 1,
              str(fx.calls()))
        check("rc 0", result.rc == 0, f"rc={result.rc} {result.text[-300:]}")
    finally:
        fx.cleanup()


def case_upload_call_shape() -> None:
    print("T3 relay_report call shape (token configured)")
    fx = Fixture()
    try:
        fx.seed_transcript([usage_line("2026-08-15", "r1")])
        result = run_script(fx, relay="token")
        month = fx.read_month("2026-08")
        check("local rollup written", month is not None, result.text[-300:])
        calls = fx.calls()
        check("exactly one call", len(calls) == 1, f"{len(calls)} calls")
        call = calls[0] if calls else None
        check("tool is relay_report", call is not None and call["tool"] == "relay_report", str(call))
        args = call["arguments"] if call else {}
        check("kind is token_usage", args.get("kind") == "token_usage", str(args))
        check("uuid_hint is the placeholder", args.get("uuid_hint") == PLACEHOLDER_UUID, str(args))
        check("month matches the rollup file", args.get("month") == "2026-08", str(args))
        body = json.loads(args.get("body", "{}"))
        check("body has hostname and home_dir",
              isinstance(body.get("hostname"), str) and body.get("home_dir") == fx.home, str(body))
        check("body's usage equals the local rollup's usage block",
              month is not None and body.get("usage") == month.get("usage"), str(body))
        check("body's tools equals the local rollup's tools block (policy chunk 3)",
              month is not None and body.get("tools") == month.get("tools"), str(body))
        check("rc 0", result.rc == 0, result.text[-300:])
    finally:
        fx.cleanup()


def case_upload_no_token() -> None:
    print("T3 upload with a relay env that has no token: still attempted (store_mcp owns auth)")
    fx = Fixture()
    try:
        fx.seed_transcript([usage_line("2026-08-15", "r1")])
        run_script(fx, relay="no-token")
        check("exactly one call", len(fx.calls()) == 1, f"{len(fx.calls())} calls")
    finally:
        fx.cleanup()


def case_unchanged_month_not_reuploaded() -> None:
    print("T3 an unchanged month is not re-uploaded on a second run")
    fx = Fixture()
    try:
        fx.seed_transcript([usage_line("2026-08-15", "r1")])
        run_script(fx, relay="token")
        check("first run: one call", len(fx.calls()) == 1, f"{len(fx.calls())} calls")
        run_script(fx, relay="token", extra_args=["--force-rollup"])
        check("second run (forced, unchanged content): still one call total",
              len(fx.calls()) == 1, f"{len(fx.calls())} calls")
    finally:
        fx.cleanup()


def case_failed_upload_retries(label: str, behavior: dict) -> None:
    print("T3 failed upload: " + label)
    fx = Fixture()
    try:
        fx.set_behavior(behavior)
        fx.seed_transcript([usage_line("2026-08-15", "r1")])
        result = run_script(fx, relay="token")
        month = fx.read_month("2026-08")
        check(label + ": local rollup still written", month is not None, result.text[-300:])
        check(label + ": exit 0 (never crashes)", result.rc == 0, f"rc={result.rc} {result.text[-300:]}")
        check(label + ": stderr names the failure", "upload" in result.err and "2026-08" in result.err,
              result.err[-300:])
        check(label + ": the attempt was made", len(fx.calls()) == 1, f"{len(fx.calls())} calls")

        fx.set_behavior({"result": {"machine_uuid": "unknown-machine", "written": True}})
        result2 = run_script(fx, relay="token", extra_args=["--force-rollup"])
        check(label + ": retried and succeeded on the next run", len(fx.calls()) == 2,
              f"{len(fx.calls())} calls; {result2.text[-300:]}")
    finally:
        fx.cleanup()


def case_checkout_uploads_too() -> None:
    print("T4 a checkout host (ls) uploads like every machine and never writes the store itself")
    fx = Fixture()
    try:
        fx.mark_checkout()
        fx.seed_transcript([usage_line("2026-08-15", "r1")])
        result = run_script(fx, relay="token")
        check("one relay_report call", len(fx.calls()) == 1, f"{len(fx.calls())} calls")
        check("nothing written under the store's machines/ tree",
              not os.path.exists(fx.machines_path()), "")
        check("rollup written under ~/.cache/agent-context/token-rollup",
              fx.read_month("2026-08") is not None, result.text[-300:])
        check("rc 0", result.rc == 0, result.text[-300:])
    finally:
        fx.cleanup()


def case_sweep_no_checkout() -> None:
    print("T5 sweep on a no-checkout store")
    fx = Fixture()
    try:
        os.makedirs(os.path.join(fx.home, ".claude", "projects"), exist_ok=True)
        write_text(fx.machines_path("uuid1", "token-usage", "2026-08.json"), '{"usage":{}}\n')
        write_text(fx.machines_path("uuid2", "token-usage", "2026-07.json"), '{"usage":{}}\n')
        write_text(fx.machines_path("uuid1", "usage.json"), '{"unrelated":true}\n')
        result = run_script(fx, relay=None)
        check("uuid1/token-usage removed",
              not os.path.exists(fx.machines_path("uuid1", "token-usage")), "")
        check("uuid2/token-usage removed",
              not os.path.exists(fx.machines_path("uuid2", "token-usage")), "")
        check("uuid1/usage.json untouched",
              os.path.isfile(fx.machines_path("uuid1", "usage.json")), "")
        check("machines/ root survives", os.path.isdir(fx.machines_path()), "")
        check("rc 0", result.rc == 0, result.text[-300:])
    finally:
        fx.cleanup()


def case_sweep_checkout_noop() -> None:
    print("T5 sweep does nothing on a checkout store")
    fx = Fixture()
    try:
        fx.mark_checkout()
        os.makedirs(os.path.join(fx.home, ".claude", "projects"), exist_ok=True)
        write_text(fx.machines_path("uuid1", "token-usage", "2026-08.json"), '{"usage":{}}\n')
        write_text(fx.machines_path("uuid1", "usage.json"), '{"unrelated":true}\n')
        run_script(fx, relay=None)
        check("uuid1/token-usage untouched",
              os.path.isfile(fx.machines_path("uuid1", "token-usage", "2026-08.json")), "")
        check("uuid1/usage.json untouched",
              os.path.isfile(fx.machines_path("uuid1", "usage.json")), "")
    finally:
        fx.cleanup()


def main() -> int:
    case_no_checkout_relocates_rollup()
    case_upload_call_shape()
    case_upload_no_token()
    case_unchanged_month_not_reuploaded()
    case_failed_upload_retries("ToolError", {"raise": "ToolError", "message": "no known machine"})
    case_failed_upload_retries("StoreUnreachable",
                               {"raise": "StoreUnreachable", "message": "the daemon is unreachable"})
    case_checkout_uploads_too()
    case_sweep_no_checkout()
    case_sweep_checkout_noop()
    total = passed + len(failures)
    print(f"\n{passed}/{total} passed")
    for label in failures:
        print("FAILED: " + label)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
