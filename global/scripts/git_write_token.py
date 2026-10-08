#!/usr/bin/env python3
'The git-write consent token, read one way by every hook that honors it.\n\nThe approval-question hook writes it when user picks Approve on a git-write approval\nquestion: a file of `expires=<epoch>` and `scope=<repo top level | any>` lines under\n$XDG_STATE_HOME/agent-context (default ~/.local/state). guard-git-write spends it;\nblock-deploy only checks it, and runs first in the dispatcher, so one approval lets a\npush-to-deploy through both gates (policy).'
import os
import time


def state_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME")
                        or os.path.join(os.path.expanduser("~"), ".local", "state"), "agent-context")


def path():
    return os.path.join(state_dir(), "git-write-consent")


def read():
    '(expires epoch, scope or None) for the token on disk, or None when there is none.\n    A missing or malformed expiry reads as 0: expired.'
    exp_str, scope = None, None
    try:
        with open(path(), encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if exp_str is None and line.startswith("expires="):
                    exp_str = line[len("expires="):].strip()
                elif scope is None and line.startswith("scope="):
                    scope = line[len("scope="):].strip()
    except OSError:
        return None
    return (int(exp_str) if exp_str and exp_str.isdigit() else 0), scope


def expired(token, now=None):
    return token[0] <= int(time.time() if now is None else now)


def covers(token, repo_top, now=None):
    'Is this token live and scoped to repo_top (or to any repo)?'
    scope = token[1]
    return not expired(token, now) and (not scope or scope == "any" or scope == repo_top)


def live_for(repo_top):
    token = read()
    return bool(token) and covers(token, repo_top)
