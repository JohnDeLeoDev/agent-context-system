#!/usr/bin/env python3
'Translate Codex native hook payloads into the canonical Claude hook contract.\n\nharness-materialize renders each Codex hook as `hook-client <python> this --event E -- CMD`,\nso this runs warm in hook-server.py (policy). When CMD is itself a hook-dispatch call on this\nPython, the dispatcher runs in this process on the translated payload: no shell, no second\nPython and no second server round trip.'
import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_paths as hp  

DISPATCHER = os.path.join(HERE, "hook-dispatch.py")

_PLAIN = re.compile(r"^[A-Za-z0-9_.,:=/@%+-]+$")


TOOL_NAMES = {
    "exec_command": "Bash",
    "shell": "Bash",
    "write_stdin": "Bash",
    "apply_patch": "Edit",
}


def failed_tool(payload):
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return False
    if response.get("is_error") is True or response.get("success") is False:
        return True
    if str(response.get("status", "")).lower() in {"error", "failed", "failure"}:
        return True
    return bool(response.get("error"))


def translate(event, payload):
    is_failure = failed_tool(payload)
    if event == "PostToolUse" and is_failure:
        return None
    if event == "PostToolUseFailure" and not is_failure:
        return None
    out = dict(payload)
    out["hook_event_name"] = event
    if out.get("tool_name") == "request_user_input_async":
        if event == "PostToolUse":
            return None  
        if event == "PreToolUse":
            questions = out.get("tool_input", {}).get("questions", [])
            out["tool_name"] = "AskUserQuestion"
            out["tool_input"] = {"questions": [
                {"question": q.get("title"), "header": "Approval",
                 "multiSelect": False,
                 "options": [{"label": label} for label in q.get("options", [])]}
                for q in questions if isinstance(q, dict)
                and "[approval:" in str(q.get("title", ""))]}
            if not out["tool_input"]["questions"]:
                return None
    tool_name = out.get("tool_name")
    if isinstance(tool_name, str):
        out["tool_name"] = TOOL_NAMES.get(tool_name, tool_name)
    if event == "Notification":
        out.setdefault("notification_type", "permission_prompt")
        out.setdefault("message", "Codex is waiting for permission")
    return out





REPLACEMENT_MAX_BYTES = 8000


def codex_output(printed):
    'The dispatcher\'s PostToolUse output as Codex reads it. Codex has no\n    updatedToolOutput: its output schema refuses unknown fields, and the one way a hook\n    replaces a tool result is decision "block", whose reason the model receives as the\n    result (codex-rs core/src/tools/registry.rs). So the field always goes, and a\n    replacement that fits becomes that block. Anything else passes unchanged.'
    try:
        doc = json.loads(printed)
    except ValueError:
        return printed
    specific = doc.get("hookSpecificOutput") if isinstance(doc, dict) else None
    if not isinstance(specific, dict) or "updatedToolOutput" not in specific:
        return printed
    new = specific.pop("updatedToolOutput")
    text = new.get("stdout") if isinstance(new, dict) else new
    if list(specific) == ["hookEventName"]:
        del doc["hookSpecificOutput"]
    if (isinstance(text, str) and text.strip() and "decision" not in doc
            and len(text.encode("utf-8", "replace")) <= REPLACEMENT_MAX_BYTES):
        doc["decision"], doc["reason"] = "block", text
    return json.dumps(doc) + "\n" if doc else ""


def project_dir(cwd):
    'The checkout holding `cwd` (a `.git` directory or worktree file), else `cwd`: what\n    `git rev-parse --show-toplevel` answers, without starting git.'
    path = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return cwd
        path = parent


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def dispatch_args(command):
    "hook-dispatch's arguments when `command` runs this host's hook-dispatch.py on this\n    Python, bare or through hook-client; else None and the command runs in a shell."
    words = command.split()
    if not words or not all(_PLAIN.match(w) for w in words):
        return None
    at = 0
    while at < len(words) - 1 and hp.is_interpreter(words[at]):
        at += 1
    pythons = [w for w in words[:at] if os.path.basename(w) != hp.HOOK_LAUNCHER]
    if len(pythons) != 1 or not _same(pythons[0], sys.executable):
        return None
    if not _same(words[at], DISPATCHER):
        return None
    return words[at + 1:]


def load_dispatcher():
    "hook-server's loaded copy when this runs there, else a fresh load."
    mod = sys.modules.get("hook_dispatch")
    if mod is not None and _same(getattr(mod, "__file__", ""), DISPATCHER):
        return mod
    spec = importlib.util.spec_from_file_location("hook_dispatch", DISPATCHER)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + DISPATCHER)
    mod = importlib.util.module_from_spec(spec)
    saved = list(sys.argv)
    sys.argv[:] = [DISPATCHER]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv[:] = saved
    return mod


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if len(args.command) != 1:
        parser.error("expected one canonical hook command after --")
    return args


def main(argv=None):
    args = parse_args(argv)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        payload = {}
    translated = translate(args.event, payload)
    if translated is None:
        return 0
    cwd = translated.get("cwd")
    if isinstance(cwd, str) and cwd:
        os.environ.setdefault("CLAUDE_PROJECT_DIR", project_dir(cwd))
    body = json.dumps(translated)
    rest = dispatch_args(args.command[0])
    if rest:
        sys.stdout.flush()
        sys.stdin = io.TextIOWrapper(io.BytesIO(body.encode("utf-8")), encoding="utf-8")
        sys.argv[:] = [DISPATCHER] + rest
        if args.event != "PostToolUse":
            code = load_dispatcher().run(sys.argv)
            sys.stdout.flush()
            return code
        real, sys.stdout = sys.stdout, io.StringIO()
        try:
            code = load_dispatcher().run(sys.argv)
            printed = sys.stdout.getvalue()
        finally:
            sys.stdout = real
        sys.stdout.write(codex_output(printed))
        sys.stdout.flush()
        return code
    if args.event == "PostToolUse":
        proc = subprocess.run(args.command[0], shell=True, input=body, text=True,
                              stdout=subprocess.PIPE)
        sys.stdout.write(codex_output(proc.stdout or ""))
        sys.stdout.flush()
        return proc.returncode
    proc = subprocess.run(args.command[0], shell=True, input=body, text=True)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
