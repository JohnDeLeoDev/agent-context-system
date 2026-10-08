#!/usr/bin/env python3
"State file (mtime = last /context-audit run for this project):\n  ~/.local/state/agent-context/audit/last-<encoded-cwd>\n/context-audit command's quick mode, step 1, touches this file on every run.\n\nInput (stdin JSON): { cwd, session_id, ... }\nOutput: { systemMessage } when overdue, silent otherwise.\n\nmtime is read with os.path.getmtime, which raises OSError on a missing or unreadable\npath on every platform. An unreadable mtime suppresses the nudge rather than guessing\nan age."
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp


def emit_suppress():
    print(json.dumps({"suppressOutput": True}))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    cwd = data.get("cwd") or ""

    if not cwd:
        emit_suppress()
        return 0

    home = os.environ.get("HOME") or ""
    if not home:
        emit_suppress()
        return 0

    dev_re = re.compile(r"^%s/Developer/(example-workspace|Personal)/[^/]+(/.*)?$" % re.escape(home))
    
    store_re = re.compile(r"^%s(/.*)?$" % re.escape(hp.store_root(home)))
    if not (dev_re.match(cwd) or store_re.match(cwd)):
        emit_suppress()
        return 0

    encoded = cwd.replace("/", "-")
    state_dir = os.path.join(hp.state_dir(home), "audit")
    state_file = os.path.join(state_dir, "last-" + encoded)

    os.makedirs(state_dir, exist_ok=True)

    if not os.path.isfile(state_file):
        
        open(state_file, "w").close()
        emit_suppress()
        return 0

    try:
        mtime = os.path.getmtime(state_file)
    except OSError:
        emit_suppress()
        return 0

    age_sec = int(__import__("time").time()) - int(mtime)
    threshold_sec = 14 * 86400

    if age_sec > threshold_sec:
        days = age_sec // 86400
        msg = ("\U0001F4CB Audit: last /context-audit was %d days ago. Review open "
               "audit observations (list_audit_observations status=open, also in "
               "get_session_context) and consider running /context-audit." % days)
        print(json.dumps({"systemMessage": msg}))
    else:
        emit_suppress()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            emit_suppress()
        except Exception:
            pass
        sys.exit(0)
