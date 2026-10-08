#!/usr/bin/env python3
"Codex has no native WorktreeCreate event (harness-materialize.py's\nCLAUDE_ONLY_CODEX_EVENTS says so), so a `git worktree add` it runs through the shell was\nnever judged at all -- unlike Claude, whose native worktree-creation UI is kept honest by\nworktree-create.py. This hook closes that gap on the Bash door, the same PreToolUse(Bash)\ndoor require-worktree-edit-bash already uses, so it reaches Codex through the same\nClaude-settings-to-Codex-hooks.json translation (harness-materialize.render_codex_hooks)\nthat already carries every other MANAGED Bash guard there.\n\nRun: python3 test-require-worktree-add-location.py"
import json
import os
import subprocess
import sys
import tempfile

REAL_STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
HOOK = os.environ.get("WORKTREE_ADD_HOOK") or os.path.join(
    REAL_STORE, "global", "hooks", "require-worktree-add-location.py")

TMP = os.path.realpath(tempfile.mkdtemp(prefix="worktree-add-location-test-"))
REPO = os.path.join(TMP, "repo")

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:400]
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


def git(*args, cwd=REPO):
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True,
                          check=True)


def run(cmd, cwd, payload_override=None):
    payload = payload_override if payload_override is not None else json.dumps(
        {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": cmd}})
    return subprocess.run([sys.executable, HOOK], input=payload, capture_output=True,
                          text=True, timeout=30)


def setup():
    os.makedirs(REPO, exist_ok=True)
    git("init", "-q", "-b", "main")


def case_legacy_claude_worktrees_refused():
    p = run("git worktree add .claude/worktrees/x -b x", REPO)
    check("a git worktree add targeting .claude/worktrees/ is refused",
          p.returncode == 2, "rc=%s err=%s" % (p.returncode, p.stderr))
    check("the refusal names the offending path and a corrected command",
          ".claude/worktrees/x" in p.stderr and ".agents/worktrees/x" in p.stderr,
          p.stderr)


def case_agents_worktrees_allowed():
    p = run("git worktree add .agents/worktrees/x -b x", REPO)
    check("a git worktree add targeting .agents/worktrees/ is allowed",
          p.returncode == 0, "rc=%s err=%s" % (p.returncode, p.stderr))


def case_absolute_elsewhere_refused():
    p = run("git worktree add /tmp/elsewhere-%s -b x" % os.path.basename(TMP), REPO)
    check("a git worktree add to an absolute path outside .agents/worktrees/ is refused",
          p.returncode == 2, "rc=%s err=%s" % (p.returncode, p.stderr))


def case_dash_c_honored():
    p = run("git -C %s worktree add /tmp/elsewhere-c-%s -b x" % (REPO, os.path.basename(TMP)),
            "/")
    check("a leading `git -C DIR` resolves the target against DIR, still refused",
          p.returncode == 2, "rc=%s err=%s" % (p.returncode, p.stderr))
    p = run("git -C %s worktree add .agents/worktrees/y -b y" % REPO, "/")
    check("`git -C DIR worktree add .agents/worktrees/y` is allowed",
          p.returncode == 0, "rc=%s err=%s" % (p.returncode, p.stderr))


def case_leading_cd_honored():
    p = run("cd %s && git worktree add .agents/worktrees/z -b z" % REPO, "/")
    check("a leading `cd <repo> &&` is honored (still allowed under .agents/worktrees/)",
          p.returncode == 0, "rc=%s err=%s" % (p.returncode, p.stderr))
    p = run("cd %s && git worktree add .claude/worktrees/z2 -b z2" % REPO, "/")
    check("a leading `cd <repo> &&` is honored (still refused under .claude/worktrees/)",
          p.returncode == 2, "rc=%s err=%s" % (p.returncode, p.stderr))


def case_unrelated_commands_untouched():
    p = run("echo hello worktree party", REPO)
    check("a command that only mentions the word worktree is allowed",
          p.returncode == 0, "rc=%s err=%s" % (p.returncode, p.stderr))
    p = run("git worktree list", REPO)
    check("git worktree list (not add) is allowed", p.returncode == 0,
          "rc=%s err=%s" % (p.returncode, p.stderr))
    p = run("git worktree remove .claude/worktrees/x", REPO)
    check("git worktree remove of an existing legacy worktree is allowed (not judged)",
          p.returncode == 0, "rc=%s err=%s" % (p.returncode, p.stderr))
    p = run("git status", REPO)
    check("an ordinary git command is allowed", p.returncode == 0,
          "rc=%s err=%s" % (p.returncode, p.stderr))


def case_fails_open():
    p = run("git worktree add .claude/worktrees/nope -b nope", os.path.join(TMP, "not-a-repo"))
    check("a base directory that is not a repository fails open (allowed)",
          p.returncode == 0, "rc=%s err=%s" % (p.returncode, p.stderr))
    p = run(None, REPO, payload_override="{not json")
    check("a malformed payload fails open", p.returncode == 0,
          "rc=%s err=%s" % (p.returncode, p.stderr))
    p = run(None, REPO, payload_override=json.dumps({"tool_input": {}}))
    check("a payload with no command fails open", p.returncode == 0,
          "rc=%s err=%s" % (p.returncode, p.stderr))
    p = run(None, REPO, payload_override=json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x"}}))
    check("a non-Bash-shaped payload with no command fails open", p.returncode == 0,
          "rc=%s err=%s" % (p.returncode, p.stderr))


def main():
    setup()
    for case in (case_legacy_claude_worktrees_refused, case_agents_worktrees_allowed,
                 case_absolute_elsewhere_refused, case_dash_c_honored,
                 case_leading_cd_honored, case_unrelated_commands_untouched,
                 case_fails_open):
        try:
            case()
        except subprocess.CalledProcessError as exc:
            check("%s ran to the end" % case.__name__, False,
                  "%s: %s" % (exc.cmd, (exc.stderr or "").strip()))


if __name__ == "__main__":
    import shutil
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = results.count(False)
    print("\n%d passed, %d failed" % (results.count(True), failed))
    sys.exit(1 if failed else 0)
