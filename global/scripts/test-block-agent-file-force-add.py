#!/usr/bin/env python3
'Case battery for block-agent-file-force-add.\n\nThe hook\'s first draft passed every one of the BLOCK cases below except the nested one,\nbecause `lstrip("./")` strips a CHARACTER SET rather than a prefix and therefore ate the\nleading dot of `.claude/settings.json`. It looked correct by reading. Run the cases.'
import json
import os
import subprocess
import sys

HOOK = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/.agent-context/global/hooks/block-agent-file-force-add.py")
CWD = os.environ.get("TEST_CWD") or os.path.expanduser("~/Developer/example-workspace/example-web")

pass_n = 0
fail_n = 0


def run(want, desc, cmd):
    global pass_n, fail_n
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": CWD})
    proc = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True, text=True)
    got = "BLOCK" if proc.returncode == 2 else "allow"
    if got == want:
        pass_n += 1
        print("  ok   %-46s %s" % (desc, got))
    else:
        fail_n += 1
        print("  FAIL %-46s want=%s got=%s" % (desc, want, got))


print("block-agent-file-force-add")


run("BLOCK", "bare -f", "git add -f .claude/settings.json")
run("BLOCK", "--force", "git add --force .agents/")
run("BLOCK", "bundled short flags", "git add -fv .agents/project-id")
run("BLOCK", "nested under a source dir", "git add -f src/.claude/foo.json")
run("BLOCK", "after a cd, chained", "cd /tmp && git add -f .agents/x")
run("BLOCK", "./-prefixed", "git add -f ./.claude/settings.json")
run("BLOCK", "smuggled through xargs", "echo x | xargs git add -f .agents/project-id")


run("allow", "plain git add .", "git add .")
run("allow", "force-add a real source file", "git add -f src/app/page.tsx")
run("allow", "git add -A", "git add -A")
run("allow", "prose that QUOTES the command", 'echo "never run git add -f .claude/settings.json"')
run("allow", "an unrelated command", "ls .claude/hooks")
run("allow", "a read", "git status --porcelain")
run("allow", "the store repo is exempt",
    "cd /Users/user/.agent-context && git add -f global/hooks/x.sh")

print()
print("  %d case(s), %d failure(s)" % (pass_n + fail_n, fail_n))
sys.exit(0 if fail_n == 0 else 1)
