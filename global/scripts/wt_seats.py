#!/usr/bin/env python3
"wt_seats: the seats a project's wt-new.py puts each task's branch in.\n\nA project's wt-new.py loads this file from the store's global/scripts and calls\n`take_seat`. Its wt-finish cleanup calls `park_seat` in wt_finish_core.py, which leaves\nthe seat on no branch at main. What a project adds to a checkout (a generated project, a\nlinked library, a build line) stays in that project's wt-new.py."

import os
import subprocess
import sys


SEATS = ("seat-1", "seat-2")


def git_out(directory: str, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", directory, *args], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def is_seat(worktree: str) -> bool:
    return os.path.basename(os.path.normpath(worktree)) in SEATS


def seat_is_idle(seat: str) -> bool:
    "A seat between tasks: on no branch (wt-finish leaves it detached at main) and with\n    nothing uncommitted. Anything else is somebody's work."
    return git_out(seat, "symbolic-ref", "-q", "HEAD") is None and git_out(seat, "status", "--porcelain") == ""


def take_seat(main_dir: str, desc: str) -> str:
    "Puts a new branch `desc` in the first free seat and returns the seat's path.\n\n    An idle seat switches to the branch, which rewrites only the files that differ from\n    what it last built. A seat that does not exist yet is created. With every seat taken\n    the task gets a checkout of its own, named for it, and a cold first build."
    worktrees = os.path.join(main_dir, ".agents", "worktrees")
    for name in SEATS:
        seat = os.path.join(worktrees, name)
        if not os.path.exists(seat):
            if subprocess.run(["git", "-C", main_dir, "worktree", "add", seat, "-b", desc],
                              stdout=subprocess.DEVNULL).returncode != 0:
                sys.exit(1)
            print(f"wt-new: created seat {seat} on branch {desc}")
            return seat
        if seat_is_idle(seat):
            if subprocess.run(["git", "-C", seat, "switch", "-q", "-c", desc, "main"]).returncode != 0:
                sys.exit(1)
            print(f"wt-new: {seat} is now on branch {desc}; its build picks up from the last one")
            return seat
    worktree = os.path.join(worktrees, desc)
    if os.path.exists(worktree):
        print(f"wt-new: {worktree} already exists: pick another name or remove it", file=sys.stderr)
        sys.exit(1)
    if subprocess.run(["git", "-C", main_dir, "worktree", "add", worktree, "-b", desc],
                      stdout=subprocess.DEVNULL).returncode != 0:
        sys.exit(1)
    print(f"wt-new: every seat is taken; created {worktree} on branch {desc} (cold first build)")
    return worktree
