#!/usr/bin/env python3
'post-edit-format: PostToolUse hook, auto-format + auto-fix Roslyn analyzers\non edited .cs files. Runs `dotnet format` with `--severity info` against the\nsolution.\n\nA project with no .sln must exit 0, not error: this hook runs on every edit,\nso it must skip without failing the edit when there is nothing to format.'

import json
import os
import subprocess
import sys


def _jq_scalar(value):
    
    
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


def _tool_input_path(payload_text):
    
    
    
    parsed = json.loads(payload_text)
    tool_input = parsed.get("tool_input") if isinstance(parsed, dict) else None
    if not isinstance(tool_input, dict):
        tool_input = {}
    for key in ("file_path", "pathInProject"):
        value = tool_input.get(key)
        if value not in (None, False):
            return _jq_scalar(value)
    return ""


def main_entry():
    input_text = sys.stdin.read()
    file_path = _tool_input_path(input_text)

    
    if not file_path or not file_path.endswith(".cs"):
        return 0

    
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if file_path.startswith("/"):
        prefix = project_dir + "/"
        if file_path.startswith(prefix):
            file_path = file_path[len(prefix):]

    try:
        os.chdir(project_dir)
    except OSError as exc:
        print(
            "%s: line 19: cd: %s: %s" % (sys.argv[0], project_dir, exc.strerror),
            file=sys.stderr,
        )
        return 1

    
    
    matches = sorted(f for f in os.listdir(".") if f.endswith(".sln"))
    if not matches:
        return 0
    sln = matches[0]

    try:
        subprocess.run(
            ["dotnet", "format", sln, "--include", file_path, "--no-restore",
             "--severity", "info", "--verbosity", "quiet"],
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
