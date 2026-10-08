#!/usr/bin/env python3
'Run it after ANY change to either worktree hook:\n  python3 ~/.agent-context/global/scripts/test-require-worktree-edit-bash.py\n\nIt needs a real git repo with real files, because the hook\'s decisions depend on git\n(is this tracked? ignored? a linked worktree?) and on the filesystem (does this path\'s\nparent directory exist?). It builds that repo itself under ~/.cache and removes it after,\nso it runs anywhere and asserts nothing about any one project. See the fixture comment\nbelow for why NOT a temp dir.\n\nTwo failures this battery has already caught, both worth keeping honest:\n  1. `sed -i \'\' \'s/a/b/\' file` was ALLOWED, because the extractor blanked quoted spans\n     before tokenizing, which erased the script argument and shifted the filename out of\n     position -- the exact construct the observation was filed about.\n  2. Two "failures" that were the TEST\'s fault: invented source paths that do not exist\n     in the repo. The hook was right to allow them. A test battery asserting on paths\n     nobody checked proves nothing.'
import json
import os
import shutil
import subprocess
import sys

HOOKS = os.environ.get("HOOKS_DIR") or os.path.expanduser("~/.agent-context/global/hooks")
BASH_HOOK = os.path.join(HOOKS, "require-worktree-edit-bash.py")
if not os.path.isfile(BASH_HOOK):
    print("missing or not executable: %s" % BASH_HOOK)
    sys.exit(1)







ROOT = os.path.join(os.environ.get("WT_HOOK_TEST_ROOT") or os.path.expanduser("~/.cache/agent-context"),
                     "wt-hook-test.%d" % os.getpid())
R = os.path.join(ROOT, "repo")
shutil.rmtree(ROOT, ignore_errors=True)
for sub in ("src/deep", ".claude/worktrees/wip/src", ".agents", "build"):
    os.makedirs(os.path.join(R, sub), exist_ok=True)

subprocess.run(["git", "init", "-q", "."], cwd=R, check=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=R, check=True)
subprocess.run(["git", "config", "user.name", "t"], cwd=R, check=True)
with open(os.path.join(R, ".gitignore"), "w") as fh:
    fh.write("build/\n")
with open(os.path.join(R, "src/App.swift"), "w") as fh:
    fh.write("let x = 1\n")
with open(os.path.join(R, "src/deep/Nested.swift"), "w") as fh:
    fh.write("let y = 2\n")
with open(os.path.join(R, "README.md"), "w") as fh:
    fh.write("# doc\n")
with open(os.path.join(R, ".agents/STATE.md"), "w") as fh:
    fh.write("note\n")
subprocess.run(["git", "add", "-A"], cwd=R, check=True)
subprocess.run(["git", "commit", "-qm", "init"], cwd=R, check=True)

failed = 0


def run(expect, command):
    global failed
    payload = json.dumps({"tool_name": "Bash", "cwd": R, "tool_input": {"command": command}})
    proc = subprocess.run([sys.executable, BASH_HOOK], input=payload, capture_output=True,
                           text=True, cwd=R)
    got = "ALLOW" if proc.returncode == 0 else "BLOCK"
    label = command.splitlines()[0][:62]
    if got == expect:
        print("  ok   %-5s %s" % (got, label))
    else:
        print("  FAIL want=%s got=%s :: %s" % (expect, got, label))
        failed = 1


print("--- must BLOCK: source written through the shell ---")
run("BLOCK", "cat > src/New.swift <<'EOF'\nlet z = 3\nEOF")
run("BLOCK", "cat >> src/App.swift <<'EOF'\nappended\nEOF")
run("BLOCK", "sed -i '' 's/a/b/' src/App.swift")
run("BLOCK", "sed -i.bak -e 's/a/b/' src/App.swift")
run("BLOCK", "sed -i '' 's/x/y/' src/App.swift src/deep/Nested.swift")
run("BLOCK", "perl -i -pe 's/a/b/' src/App.swift")
run("BLOCK", "echo hi >> src/App.swift")
run("BLOCK", "echo hi > src/App.swift")
run("BLOCK", "tee src/App.swift <<EOF\nbody\nEOF")
run("BLOCK", "cp /etc/hosts src/App.swift")
run("BLOCK", "mv /etc/hosts src/deep/Nested.swift")
run("BLOCK", "dd of=src/App.swift if=/dev/zero")
run("BLOCK", "python3 gen.py > src/Generated.swift")
run("BLOCK", "python3 - <<'PY' > src/App.swift\nprint(1)\nPY")

print("--- must ALLOW: reads, builds, exempt paths, redirect look-alikes ---")
run("ALLOW", "cat src/App.swift")
run("ALLOW", 'grep "a>b" src/App.swift')
run("ALLOW", "sed -n '1,5p' src/App.swift")
run("ALLOW", "sed 's/a/b/' src/App.swift | head")
run("ALLOW", "diff src/App.swift /etc/hosts")
run("ALLOW", "ls > /dev/null")
run("ALLOW", "make build 2>&1 | tail -5")
run("ALLOW", "cat > README.md <<EOF")          
run("ALLOW", "echo x > .agents/STATE.md")      
run("ALLOW", "echo x > .claude/scratch.txt")   
run("ALLOW", "echo x > build/out.o")           
run("ALLOW", "cat > .claude/worktrees/wip/src/App.swift <<EOF")   
run("ALLOW", "echo x > /tmp/scratch.txt")      
run("ALLOW", "git log --oneline | head -20")
run("ALLOW", "echo done && echo more")
run("ALLOW", "find . -name '*.swift' | head")



run("ALLOW", "ln -sf /etc/hosts \\\n  /tmp/linked-hosts")
run("ALLOW", "git worktree add .claude/worktrees/x -b x \\\n  && echo made")
run("BLOCK", "cp /etc/hosts \\\n  src/App.swift")







print("--- must ALLOW: policy false positives ---")


run("ALLOW", 'X=/tmp/ssh-probe.txt; ssh -vvv host true > "$X" 2>&1; grep -iE \'sig\' "$X"')
run("ALLOW", "ls 2>&1 | tee /tmp/probe.log")




run("ALLOW", "cat >> .agents/STATE.md <<'EOF'\nnote: elapsedMs >= HARD_TIMEOUT_MS - 500\nEOF")
run("ALLOW", "cat >> .agents/STATE.md <<'EOF'\nthe factory returns () -> T for each entry\nEOF")
run("ALLOW", "cat >> .agents/STATE.md <<'EOF'\nit reads distanceMap.value[key] on every frame\nEOF")


run("ALLOW", "gh run list --json createdAt --jq '.[] | select(.createdAt > \"2026-08-26\")'")



run("ALLOW", "cd /tmp && echo x > probe-out.json")




run("ALLOW", "ls /tmp # NO guard here -> the block is skipped")


run("ALLOW", "sed -i.bak 's|a|b|' /tmp/probe-script.sh")

run("ALLOW", "python3 -c \"print('a', '|', {}.get('m'), '->', 'b')\"")

run("ALLOW", "par=$(/bin/ps -o command= -p 1 2>/dev/null | head -c 90 || echo '<gone>'); echo \"$par\"")

print("--- and the controls that must still BLOCK ---")
run("BLOCK", "cd %s && echo x > src/App.swift" % R)        
run("BLOCK", "echo hi >| src/App.swift")               
run("BLOCK", "echo hi &> src/App.swift")               
run("BLOCK", "echo hi 2> src/App.swift")               
run("BLOCK", "cat >> src/App.swift <<'EOF'\nelapsedMs >= HARD_TIMEOUT_MS\nEOF")
run("BLOCK", "echo x > src/App.swift # with a trailing comment")
run("BLOCK", "sed -i.bak 's|a|b|' src/App.swift")

print()
print("ALL PASS" if failed == 0 else "SOME FAILED")
shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(failed)
