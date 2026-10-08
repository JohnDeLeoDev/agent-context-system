#!/usr/bin/env python3
'Ralph cleanup-audit setup script.\n\nCreates the state file `.claude/ralph-cleanup.local.md` that\nralph-cleanup-stop.py reads on every Stop event.\n\nUsage:\n  ralph-cleanup-setup.py <prompt-body>\n\nOptional env:\n  RALPH_CLEANUP_MAX_ITERATIONS  (default 0 = unlimited)\n  RALPH_CLEANUP_COMPLETION      (default "DONE")'

import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

MAX_ITERATIONS_RE = re.compile(r"^[0-9]+$")


def main():
    prompt_body = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt_body:
        print("ralph-cleanup-setup: no prompt body provided.", file=sys.stderr)
        return 1

    max_iterations = os.environ.get("RALPH_CLEANUP_MAX_ITERATIONS", "0")
    completion_promise = os.environ.get("RALPH_CLEANUP_COMPLETION", "DONE")

    if not MAX_ITERATIONS_RE.match(max_iterations):
        print("ralph-cleanup-setup: MAX_ITERATIONS must be a non-negative "
              "integer (got '%s')." % max_iterations, file=sys.stderr)
        return 1

    os.makedirs(hp.CLAUDE_DIRNAME, exist_ok=True)
    state_file = hp.CLAUDE_DIRNAME + "/ralph-cleanup.local.md"
    cancel_sentinel = hp.CLAUDE_DIRNAME + "/ralph-cleanup.cancel"

    
    try:
        os.remove(cancel_sentinel)
    except FileNotFoundError:
        pass

    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(state_file, "w", encoding="utf-8") as fh:
        fh.write("---\n")
        fh.write("active: true\n")
        fh.write("iteration: 1\n")
        fh.write("session_id: %s\n" % session_id)
        fh.write("max_iterations: %s\n" % max_iterations)
        fh.write('completion_promise: "%s"\n' % completion_promise)
        fh.write('started_at: "%s"\n' % started_at)
        fh.write("---\n")
        fh.write("\n")
        fh.write("%s\n" % prompt_body)

    max_iterations_display = max_iterations if int(max_iterations) > 0 else "unlimited"

    print("\U0001f504 ralph-cleanup loop activated.")
    print("")
    print("  Iteration:           1")
    print("  Max iterations:      %s" % max_iterations_display)
    print("  Completion sentinel: %s  (emit it inside promise tags when truly done)"
          % completion_promise)
    print("")
    print("  State file:          %s" % state_file)
    print("  Hard kill switches:")
    print("    • touch %s" % cancel_sentinel)
    print("    • set 'active: false' in %s" % state_file)
    print("    • delete %s entirely" % state_file)
    print("")
    print("The Stop hook (~/.agent-context/global/hooks/ralph-cleanup-stop.py) re-feeds the prompt on")
    print("each iteration. Unlike the original ralph-loop plugin, this one scans ANY")
    print("promise tag in your last text block — earlier inline mentions won't shadow")
    print("your real terminator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
