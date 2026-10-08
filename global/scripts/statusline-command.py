#!/usr/bin/env python3
'statusline-command.py -- Claude Code status line, mirrors a Starship-style prompt.\n\nReceives JSON on stdin from Claude Code.'

import json
import os
import subprocess
import sys


def _git(args, cwd, env):
    proc = subprocess.run(["git", "-C", cwd] + args, env=env,
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout.rstrip("\n")


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    cwd = data.get("cwd") or ""
    if not cwd:
        workspace = data.get("workspace")
        if isinstance(workspace, dict):
            cwd = workspace.get("current_dir") or ""

    
    
    
    home = os.environ.get("HOME", "")
    if home and (cwd == home or cwd.startswith(home + os.sep)):
        cwd_display = "~" + cwd[len(home):]
    else:
        cwd_display = cwd

    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    code, branch = _git(["symbolic-ref", "--short", "HEAD"], cwd, env)
    if code != 0 or not branch:
        code, branch = _git(["rev-parse", "--short", "HEAD"], cwd, env)
        if code != 0:
            branch = ""

    model_field = data.get("model")
    model = ""
    if isinstance(model_field, dict):
        model = model_field.get("display_name") or ""

    used_pct = None
    ctx_field = data.get("context_window")
    if isinstance(ctx_field, dict):
        raw_pct = ctx_field.get("used_percentage")
        if raw_pct is not None:
            used_pct = raw_pct

    parts = ["\033[1;36m%s\033[0m" % cwd_display]

    if branch:
        parts.append("\033[35m %s\033[0m" % branch)

    if model:
        parts.append("\033[2m%s\033[0m" % model)

    if used_pct is not None:
        used_int = int(format(float(used_pct), ".0f"))
        if used_int >= 75:
            color = "\033[31m"
        elif used_int >= 50:
            color = "\033[33m"
        else:
            color = "\033[32m"
        parts.append("%sctx:%d%%\033[0m" % (color, used_int))

    sys.stdout.write(parts[0])
    for part in parts[1:]:
        sys.stdout.write(" \033[2m|\033[0m %s" % part)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
