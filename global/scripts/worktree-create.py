#!/usr/bin/env python3
"Place Claude-created git worktrees under the repo's neutral .agents tree."

import json
import os
import re
import subprocess
import sys


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", cwd, *args], text=True, capture_output=True, check=True
    ).stdout.strip()


def main():
    try:
        payload = json.load(sys.stdin)
        name = payload["name"]
        cwd = payload["cwd"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ValueError("invalid worktree name")
        if ".." in name:
            raise ValueError("invalid worktree name")
        if not isinstance(cwd, str) or not os.path.isdir(cwd):
            raise ValueError("invalid working directory")
        common = git(cwd, "rev-parse", "--git-common-dir")
        root = os.path.dirname(os.path.realpath(os.path.join(cwd, common)))
        if not os.path.isdir(os.path.join(root, ".git")):
            raise ValueError("a non-bare main checkout is required")
        target = os.path.join(root, ".agents", "worktrees", name)
        if os.path.lexists(target):
            raise ValueError("worktree path already exists: " + target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        git(root, "worktree", "add", "-b", name, target, "HEAD")
        print(target)
        return 0
    except (KeyError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("worktree-create: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
