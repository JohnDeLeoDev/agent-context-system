#!/usr/bin/env python3
"unlanded-work -- report work that exists but has not landed.\n\nPorted from shell in Consolidation Phase 4. --help prints a short usage; the\nincident that motivated this script is in the store entity's description field,\nnot repeated here."
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.environ.get("HOME", "")
DASH = "—"

HELP_TEXT = """unlanded-work.py -- report work that exists but has not landed.

Usage:
  unlanded-work.py [--repo <path>]   # check one repo, default: cwd
  unlanded-work.py --hook <path>     # same check, quiet when clean (SessionStart hook)
  unlanded-work.py --fleet           # sweep every reachable machine, print a digest

Report-only: never lands, commits, pushes or deletes.
"""


def env_days(name, default):
    v = os.environ.get(name, "")
    return int(v) if v else default


WT_HOURS = env_days("UNLANDED_WT_HOURS", 12)
BRANCH_DAYS = env_days("UNLANDED_BRANCH_DAYS", 3)
PUSH_DAYS = env_days("UNLANDED_PUSH_DAYS", 1)
DIRTY_DAYS = env_days("UNLANDED_DIRTY_DAYS", 3)


def ignored_repos():
    extra = os.environ.get("UNLANDED_IGNORE", "")
    return (extra).split()


def always_scan_paths():
    if "UNLANDED_ALWAYS_SCAN" in os.environ:
        val = os.environ["UNLANDED_ALWAYS_SCAN"]
    else:
        val = os.path.join(HOME, ".local", "share", "chezmoi")
    return val.split()


def now():
    return int(time.time())


def git_run(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)


def commit_age(repo, ref="HEAD"):
    t = git_run(repo, "log", "-1", "--format=%ct", ref).stdout.strip()
    return now() - int(t) if t else 0


def dirty_age(repo):
    out = git_run(repo, "diff", "--name-only", "HEAD").stdout
    newest = 0
    for f in out.splitlines()[:50]:
        if not f:
            continue
        path = os.path.join(repo, f)
        if not os.path.exists(path):
            continue
        try:
            t = int(os.stat(path).st_mtime)
        except OSError:
            continue
        if t > newest:
            newest = t
    return now() - newest if newest > 0 else 0


def hostname_short():
    proc = subprocess.run(["hostname", "-s"], capture_output=True, text=True)
    out = proc.stdout.strip()
    if out:
        return out
    return subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()


def verify_ref(repo, ref):
    return git_run(repo, "rev-parse", "--verify", "-q", ref).returncode == 0


def scan_repo(repo):
    lines = []
    name = os.path.basename(repo)
    if name in ignored_repos():
        return lines
    host = hostname_short()

    if not os.path.isdir(repo):
        lines.append("%s|%s|UNSCANNABLE|no such path: %s" % (host, name, repo))
        return lines
    if git_run(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        lines.append("%s|%s|UNSCANNABLE|not a git work tree: %s" % (host, name, repo))
        return lines
    if git_run(repo, "rev-parse", "--is-bare-repository").stdout.strip() == "true":
        return lines

    proc = git_run(repo, "symbolic-ref", "--short", "HEAD")
    main_branch = proc.stdout.strip() if proc.returncode == 0 else "main"

    base = "main"
    if not verify_ref(repo, "main"):
        base = "master"
    if not verify_ref(repo, base):
        base = main_branch

    
    d = len(git_run(repo, "status", "--porcelain", "--untracked-files=no").stdout.splitlines())
    if d > 0:
        age = dirty_age(repo)
        if age >= DIRTY_DAYS * 86400:
            lines.append("%s|%s|DIRTY_CHECKOUT|%d tracked file(s) uncommitted for %dd in the main checkout"
                          % (host, name, d, age // 86400))

    
    ahead = len(git_run(repo, "log", "--oneline", "@{u}..").stdout.splitlines())
    if ahead > 0:
        age = commit_age(repo, "HEAD")
        if age >= PUSH_DAYS * 86400:
            lines.append("%s|%s|UNPUSHED|%d commit(s) on %s never pushed, newest %dd old"
                          % (host, name, ahead, main_branch, age // 86400))

    
    worktrees = [line[len("worktree "):] for line
                 in git_run(repo, "worktree", "list", "--porcelain").stdout.splitlines()
                 if line.startswith("worktree ")]
    for w in worktrees:
        if not w or not os.path.isdir(w):
            continue
        if w == repo:
            continue
        wn = os.path.basename(w)
        wb = git_run(w, "branch", "--show-current").stdout.strip()
        wd = len(git_run(w, "status", "--porcelain").stdout.splitlines())
        age = commit_age(w, "HEAD")
        if wd > 0 and age >= WT_HOURS * 3600:
            lines.append("%s|%s|WORKTREE_DIRTY|%s: %d uncommitted file(s), last commit %dh ago"
                          % (host, name, wn, wd, age // 3600))
        if wb:
            un = len(git_run(repo, "log", "--oneline", "%s..%s" % (base, wb)).stdout.splitlines())
            if un > 0 and age >= BRANCH_DAYS * 86400:
                lines.append("%s|%s|WORKTREE_UNLANDED|%s (%s): %d commit(s) not on %s, last commit %dd ago"
                              % (host, name, wn, wb, un, base, age // 86400))

    
    
    branches = [b for b in git_run(repo, "branch", "--format=%(refname:short)").stdout.splitlines() if b]
    wt_list_text = git_run(repo, "worktree", "list").stdout
    for b in branches:
        if b == base:
            continue
        n = len(git_run(repo, "log", "--oneline", "%s..%s" % (base, b)).stdout.splitlines())
        if n <= 0:
            continue
        if ("[" + b + "]") in wt_list_text:
            continue
        age = commit_age(repo, b)
        if age < BRANCH_DAYS * 86400:
            continue
        lines.append("%s|%s|BRANCH_UNLANDED|%s: %d commit(s) ahead of %s, no worktree, last commit %dd ago"
                      % (host, name, b, n, base, age // 86400))

    return lines


def find_repos():
    bases = [os.path.join(HOME, "Developer"), os.path.join(HOME, "dev"),
              os.path.join(HOME, "src"), os.path.join(HOME, "projects"), HOME]
    found = set()
    for base in bases:
        if not os.path.isdir(base):
            continue
        proc = subprocess.run(
            ["find", base, "-maxdepth", "3",
             "(", "-name", "node_modules", "-o", "-name", "Library", "-o", "-name", "worktrees", ")",
             "-prune", "-o", "-name", ".git", "-print"],
            capture_output=True, text=True)
        for line in proc.stdout.splitlines():
            if not line:
                continue
            found.add(line[:-5] if line.endswith("/.git") else line)
    return sorted(found)


def field(line, idx):
    parts = line.split("|")
    return parts[idx] if len(parts) > idx else ""


def repo_or_hook_mode(mode, target):
    r = target if target else os.getcwd()
    proc = subprocess.run(["git", "-C", r, "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        r = proc.stdout.strip()

    out = scan_repo(r)
    if mode == "hook":
        out = [l for l in out if "UNSCANNABLE|not a git work tree" not in l]
        for extra in always_scan_paths():
            if not os.path.isdir(extra):
                continue
            proc = subprocess.run(["git", "-C", extra, "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                continue
            extra_top = proc.stdout.strip()
            if extra_top == r:
                continue
            eout = [l for l in scan_repo(extra_top) if "UNSCANNABLE" not in l]
            if eout:
                out += eout

    if out:
        if all("UNSCANNABLE" in l for l in out):
            if mode == "hook":
                out = [l for l in out if "UNSCANNABLE|not a git work tree" not in l]
            if out:
                for l in out:
                    print("unlanded-work: could not check %s %s" % (DASH, field(l, 3)))
        else:
            print("unlanded-work: work that exists but has not landed %s" % DASH)
            for l in out:
                print("    [%s] %s: %s" % (field(l, 2), field(l, 1), field(l, 3)))
            print("    Report only. Land it, or say so; nothing here acts on its own.")
    elif mode == "repo":
        print("unlanded-work: nothing unlanded in %s" % os.path.basename(r))


def fleet_mode():
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    print("# Unlanded work %s fleet sweep %s" % (DASH, stamp))
    print()
    print("Thresholds: worktree dirty >%dh · branch unlanded >%dd · unpushed >%dd · checkout dirty >%dd"
          % (WT_HOURS, BRANCH_DAYS, PUSH_DAYS, DIRTY_DAYS))
    print()
    out = []
    for r in find_repos():
        out += scan_repo(r)
    if out:
        for l in out:
            print("- **%s** `%s` %s %s: %s" % (field(l, 0), field(l, 1), DASH, field(l, 2), field(l, 3)))
    else:
        print("Nothing unlanded on this host.")


def main(argv):
    mode = "repo"
    target = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--fleet":
            mode = "fleet"
            i += 1
        elif a == "--repo":
            mode = "repo"
            target = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        elif a == "--hook":
            mode = "hook"
            target = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        elif a in ("-h", "--help"):
            print(HELP_TEXT)
            return 0
        else:
            target = a
            i += 1

    if mode in ("repo", "hook"):
        repo_or_hook_mode(mode, target)
    else:
        fleet_mode()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
