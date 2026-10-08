#!/usr/bin/env python3
"wt-sweep.py — remove leftover worktree artifacts so .agents/worktrees/ and legacy .claude/worktrees/ never\naccumulates husks. Project-agnostic: this is the ONE copy. Project wt-finish\nscripts run it here directly; the per-project .agents/scripts/ shim that the\nsettings.json worktree hooks name only execs it.\n\nBackground: ExitWorktree(remove) deregisters a worktree, but a build tool holding\na lock during removal (a Gradle daemon, an MSBuild node) can leave behind an empty\nshell directory, often just an empty .gradle/. These husks are harmless (0 bytes)\nbut pile up. This prunes git's worktree metadata and deletes any directory under\n.agents/worktrees/ or .claude/worktrees/ that is NOT a registered worktree and contains no real content.\n\nSAFETY: a directory is removed ONLY when ALL hold:\n  - it lives under <main>/.agents/worktrees/ or <main>/.claude/worktrees/\n  - it is not in `git worktree list` (never touch a live worktree)\n  - it has no .git entry and zero regular files (never touch real work)\nA worktree with checked-out source or uncommitted edits has files and is\ntherefore always skipped. Note a symlinked node_modules counts as a symlink,\nnot a regular file, so a husk holding only that symlink is still swept.\n\nWired as SessionStart and PostToolUse(ExitWorktree) hooks. Safe to run anytime.\n\nArg $1: the main project dir (passed as $CLAUDE_PROJECT_DIR by the hook).\n        Falls back to git discovery from the cwd."

import glob
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp


def _git_stdout(args):
    return subprocess.run(["git"] + args, capture_output=True, text=True)


def _first_worktree_path(porcelain_stdout):
    for line in porcelain_stdout.splitlines():
        if line.startswith("worktree "):
            parts = line.split(maxsplit=2)
            return parts[1] if len(parts) >= 2 else ""
    return ""


def _registered_worktree_paths(porcelain_stdout):
    registered = set()
    for line in porcelain_stdout.splitlines():
        if line.startswith("worktree "):
            parts = line.split(maxsplit=2)
            if len(parts) >= 2:
                registered.add(parts[1])
    return registered


def _has_any_file(root):
    
    
    
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry.path)
                elif entry.is_file():
                    return True
            except OSError:
                continue
    return False


def _prune_sln(sln, wt):
    with open(sln, newline="") as fh:
        text = fh.read()
    match = re.search(r"\tGlobalSection\(RiderSharedRunConfigurations\) = postSolution\n", text)
    if not match:
        return
    start = match.start()
    end = text.index("\tEndGlobalSection\n", start)
    kept = []
    changed = False
    for line in text[start:end].splitlines(keepends=True):
        wm = re.search(r"File = \.(?:agents|claude)\\worktrees\\([^\\]+)\\", line)
        if wm and not os.path.isdir(os.path.join(wt, wm.group(1))):
            changed = True
            continue
        kept.append(line)
    if changed:
        with open(sln, "w", newline="") as fh:
            fh.write(text[:start] + "".join(kept) + text[end:])
        print("wt-sweep: pruned dead Rider run-config pointer(s) from %s"
              % os.path.basename(sln))


def main(argv):
    
    
    
    
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    unknown = [a for a in argv if a.startswith("-")]
    if unknown:
        print("wt-sweep: unknown flag %s" % unknown[0], file=sys.stderr)
        return 2
    main_dir = argv[0] if argv else ""
    if not main_dir:
        proc = _git_stdout(["rev-parse", "--show-toplevel"])
        main_dir = proc.stdout.rstrip("\n") if proc.returncode == 0 else ""

    if main_dir:
        proc = _git_stdout(["-C", main_dir, "worktree", "list", "--porcelain"])
        if proc.returncode != 0:
            
            
            
            print("wt-sweep: %s is not a git repository" % main_dir, file=sys.stderr)
            return 1
        topmain = _first_worktree_path(proc.stdout)
        if topmain:
            main_dir = topmain

    if not main_dir:
        print("wt-sweep: cannot resolve main checkout", file=sys.stderr)
        return 0

    wts = [os.path.join(main_dir, scope, "worktrees")
           for scope in hp.HARNESS_DIRNAMES
           if os.path.isdir(os.path.join(main_dir, scope, "worktrees"))]
    if not wts:
        return 0

    subprocess.run(["git", "-C", main_dir, "worktree", "prune"], stderr=subprocess.DEVNULL)

    proc = _git_stdout(["-C", main_dir, "worktree", "list", "--porcelain"])
    if proc.returncode != 0:
        print("wt-sweep: %s is not a git repository" % main_dir, file=sys.stderr)
        return 1
    registered = _registered_worktree_paths(proc.stdout)

    removed = 0
    for wt in wts:
        for name in sorted(os.listdir(wt)):
            d = os.path.join(wt, name)
            if not os.path.isdir(d):
                continue
            
            if os.path.islink(d):
                continue
            if d in registered:
                continue
            if os.path.exists(os.path.join(d, ".git")):
                continue
            if _has_any_file(d):
                continue
            shutil.rmtree(d)
            removed += 1

    if removed > 0:
        print("wt-sweep: removed %d empty worktree husk(s)" % removed)

    for sln in sorted(glob.glob(os.path.join(main_dir, "*.sln"))):
        for wt in wts:
            try:
                _prune_sln(sln, wt)
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
