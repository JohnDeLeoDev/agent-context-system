#!/usr/bin/env python3
'remind-critical-rules.'
import json
import sys


def main():
    msg = ('Standing orders still apply: worktree mandate, deploy gate, git-write '
           'limits: full text in Global Agent Instructions '
           '§"Worktrees" and §"Filesystem and git safety" (bootstrap), and AGENTS.md '
           '§"Critical rules". After '
           'a compaction, re-read them with get_instructions() before editing '
           'source, deploying, or writing git.')
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                              "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
