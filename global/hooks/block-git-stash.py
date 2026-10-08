#!/usr/bin/env python3
'Universal PreToolUse(Bash) hook: block the `git stash` subcommands that can\nLOSE work, by orchestrator OR subagents, in any project.\n\nPython port of the shell original of block-git-stash.py.\n\nEverything not on the allowlist is blocked, including a bare `git stash`\n(which is `push`). New subcommands therefore fail closed.'
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

HOME = hp.home()

ALLOWED = {"list", "show", "drop"}



OPTS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}


def _git_at(argv, i):
    'Is argv[i] an unquoted `git` executable word?'
    t = argv[i]
    if t.quoted:
        return False
    return os.path.basename(t.text) == "git"


def _load_scan():
    path = os.environ.get("SHELL_COMMAND_SCAN") or os.path.join(hp.scripts_dir(HOME), "shell-command-scan.py")
    if not os.path.isfile(path):
        return None, path
    spec = importlib.util.spec_from_file_location("scs", path)
    if spec is None or spec.loader is None:
        return None, path
    scs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scs)
    return scs, path


def _find_blocked(scs, command, cwd):
    '[subcommand, ...] for every blocked stash invocation in `command`.'
    _, segments = scs.parse(command, cwd)
    blocked = []
    for _dir, argv, _sep in segments:
        i = 0
        while i < len(argv):
            if not _git_at(argv, i):
                i += 1
                continue
            j = i + 1
            while j < len(argv):
                t = argv[j].text
                if t in OPTS_WITH_VALUE:
                    j += 2
                    continue
                if t.startswith("--") and "=" in t:
                    j += 1
                    continue
                break
            if j < len(argv) and argv[j].text == "stash" and not argv[j].quoted:
                sub = argv[j + 1].text if j + 1 < len(argv) else None
                if sub is None or sub.startswith("-"):
                    blocked.append("push")
                elif sub not in ALLOWED:
                    blocked.append(sub)
                i = j + 1
                continue
            i += 1
    return blocked


PARSE_FAILED_MSG = """BLOCKED by block-git-stash.py:

This command mentions "stash" but could not be parsed, so the hook
cannot tell a read from a write. It fails closed.

Split it into plain, separate commands and re-run.
"""

MISSING_SCAN_MSG = """BLOCKED by block-git-stash.py:

The shared shell parser is missing, so this command cannot be judged:
  %s

That is a broken projection, not a problem with your command.
Repair it with:  python3 ~/.agent-context/global/scripts/home-materialize.py
"""

BLOCKED_TRAILER = """
Reason: parallel agents have lost in-flight work to stash/pop merge conflicts.
push/pop/apply/branch/clear move working-tree state and stay blocked for
everyone, including the user's `!` escape hatch.

ALLOWED, and enough for every legitimate need:
  git stash list          -- see what is stashed
  git stash show [-p] ... -- see what an entry contains
  git stash drop <ref>    -- remove an entry you have decided is dead

To check whether an error is pre-existing, do not stash:
  - Read git history: git log -p -- <path>, git show HEAD:<path>
  - Read the file directly with the Read tool
  - Ask the user

This hook cannot be bypassed.
"""


def evaluate(raw_text):
    '(allowed: bool, message: str).'
    if "stash" not in raw_text:
        return True, ""

    scs, scan_path = _load_scan()
    if scs is None:
        return False, MISSING_SCAN_MSG % scan_path

    try:
        data = json.loads(raw_text)
        command, cwd = scs.payload_command(data)
    except Exception:
        return False, PARSE_FAILED_MSG

    try:
        blocked = _find_blocked(scs, command, cwd)
    except Exception:
        return False, PARSE_FAILED_MSG

    if not blocked:
        return True, ""

    lines = ["BLOCKED by block-git-stash.py:", "", "This command runs a forbidden stash subcommand:", ""]
    for sub in blocked[:5]:
        lines.append("  git stash %s" % sub)
    msg = "\n".join(lines) + "\n" + BLOCKED_TRAILER
    return False, msg


def main(argv):
    raw_text = sys.stdin.read()
    allowed, msg = evaluate(raw_text)
    if msg:
        sys.stderr.write(msg)
    return 0 if allowed else 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  
        sys.stderr.write("BLOCKED by block-git-stash.py: internal error, failing closed: %r\n" % (exc,))
        sys.exit(2)
