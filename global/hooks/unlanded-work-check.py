#!/usr/bin/env python3
'SessionStart: does THIS repo hold work that exists but never landed?\n\nThe asymmetry this closes: the agent-context store is committed on every\nPostToolUse and pushed on every Stop, and a health probe reports sync failure.\nProject repos had nothing. Every protection that looked like one was a\nlanding-time guard -- wt-finish.sh refusing a dirty worktree, guard-git-write\nforcing work into a worktree -- and none of them ever asked whether the work came\nback out. wt-sweep.py, the only thing that walks .agents/worktrees/ and legacy .claude/worktrees/, skips any\nworktree holding files BY DESIGN, which is right for deleting husks and is exactly\nwhy the loaded ones were invisible.\n\nSilent when clean (--hook), so it costs a line only when there is something to say.\nReport-only: landing needs a gate and a judgment call, and destroying unlanded\nwork cannot be undone.'
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp


def main():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    script = os.path.join(hp.scripts_dir(home), "unlanded-work.py")
    if not (os.access(script, os.X_OK) or os.path.isfile(script)):
        return 0

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        proc = subprocess.run(
            [sys.executable, script, "--hook", project_dir],
            capture_output=True, text=True, timeout=60,
        )
        if proc.stdout:
            sys.stdout.write(proc.stdout)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
