#!/usr/bin/env python3
"Universal PreToolUse(Bash) hook: block the git subcommands that DISCARD\nworking-tree or history state, by orchestrator OR subagents, in any project.\n\nPython port of the shell original of block-destructive-git.py.\n\nWHAT IS BLOCKED\n  git restore ...          -- its entire purpose is discarding\n  git reset ...            -- including --soft; see the note below\n  git clean ...            -- deletes untracked files outright\n  git revert ...           -- rewrites forward, still a history write\n  git checkout -- <path>   -- the spelling the mandate names\n  git checkout <path>      -- same operation, resolved by testing the path\n  git checkout -f / .      -- the force and whole-tree spellings\n  git switch --discard-changes / -f  -- checkout's destructive half, renamed\n\nWHAT IS ALLOWED, because it moves no file content\n  git checkout <branch>, -b, -B, --detach, git switch <branch>\n  every read: log, show, diff, status, rev-parse, worktree list\n\n`git reset` is blocked in full even though `--soft` and a bare `reset` only\nmove the index. Two reasons: the mandate names `reset` flatly, and this hook\ndoes not get to narrow a rule the user wrote; and the failure mode is one\ncharacter -- `--soft` and `--hard` differ by four letters in a command an\nagent is composing while it is already confused about state. Unstaging is the\none legitimate need it takes away -- ask the user, or leave the file staged\nand let the commit gate sort it."
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

HOME = hp.home()


ALWAYS = {
    "restore": "discards working-tree or index content",
    "reset": "moves HEAD and can discard the index and working tree",
    "clean": "deletes untracked files outright",
    "revert": "writes a new commit to undo history",
}



OPTS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}


FORCE_FLAGS = {"-f", "--force", "--discard-changes", "--ours", "--theirs"}

TRIGGER_WORDS = ("checkout", "restore", "reset", "clean", "revert", "switch")


def _git_at(argv, i):
    ' git at.'
    t = argv[i]
    return (not t.quoted) and os.path.basename(t.text) == "git"


def _checkout_verdict(rest, cwd):
    "Why this checkout/switch is destructive, or None if it only moves HEAD.\n\n    Three ways to be destructive, in the order they are cheap to test: an\n    explicit `--` pathspec separator, a force/discard flag, or an operand that\n    resolves to a real path in the segment's own directory -- the bare\n    `git checkout src/app/helpers/format.ts` shape, indistinguishable from a\n    branch name until the filesystem is checked."
    for t in rest:
        if t.quoted:
            continue
        if t.text == "--":
            return "names a pathspec after `--`"
        if t.text in FORCE_FLAGS:
            return "carries %s" % t.text
        if t.text == ".":
            return "targets the whole tree with `.`"
    for t in rest:
        if t.quoted or t.text.startswith("-"):
            continue
        if os.path.exists(os.path.join(cwd or ".", t.text)):
            return "names an existing path (%s)" % t.text
    return None


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
    '[(subcommand, reason), ...] for every destructive git invocation in `command`.'
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
            if j < len(argv) and not argv[j].quoted:
                sub = argv[j].text
                if sub in ALWAYS:
                    blocked.append((sub, ALWAYS[sub]))
                elif sub in ("checkout", "switch"):
                    why = _checkout_verdict(argv[j + 1:], _dir)
                    if why:
                        blocked.append((sub, why))
                i = j + 1
                continue
            i += 1
    return blocked


PARSE_FAILED_MSG = """BLOCKED by block-destructive-git.py:

This command mentions a destructive git verb but could not be parsed,
so the hook cannot tell a read from a write. It fails closed.

Split it into plain, separate commands and re-run.
"""

MISSING_SCAN_MSG = """BLOCKED by block-destructive-git.py:

The shared shell parser is missing, so this command cannot be judged:
  %s

That is a broken projection, not a problem with your command.
Repair it with:  python3 ~/.agent-context/global/scripts/home-materialize.py
"""

BLOCKED_TRAILER = """
Reason: these verbs destroy state that nothing else can recover, and prose alone
has not held -- this guard exists because the rule was recited and then broken in
the same session.

WHAT TO DO INSTEAD
  Undo an edit you just made      Edit/Write it back; you know what you changed
  Remove a file you just created  rm <path> -- it is yours, and rm is not gated
  See a pre-existing state        git show HEAD:<path>, git log -p -- <path>
  Abandon a whole worktree        ExitWorktree(action: "remove") -- that is its job
  Need one of these               ask user to run it in his own terminal with `!`

Branch movement is NOT blocked: git checkout <branch>, -b, --detach and
git switch <branch> all still work. Only the spellings that touch file content
are refused.
"""


def evaluate(raw_text):
    '(allowed: bool, message: str).'
    if not any(w in raw_text for w in TRIGGER_WORDS):
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

    lines = ["BLOCKED by block-destructive-git.py:", "", "This command discards work:", ""]
    for sub, why in blocked[:5]:
        lines.append("  git %s -- %s" % (sub, why))
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
        sys.stderr.write("BLOCKED by block-destructive-git.py: internal error, failing closed: %r\n" % (exc,))
        sys.exit(2)
