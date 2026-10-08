#!/usr/bin/env python3
'post-edit-verify: PostToolUse(Edit|Write): lint the file that was just edited.\n\nWhy this exists. Two points meet here:\n\n  1. A common failure in agent-written changes is ordinary oversight: the null\n     check, the unused import, the shadowed binding, the unreachable branch.\n     Every one of those is something a linter knows how to find.\n  2. An instruction is a suggestion; a hook always runs. "Fix every warning before\n     committing" and "verify with the stack\'s build/test" are rules that get\n     dropped late in a long session, which is when the sloppy edits happen.\n\nSo the check stops being something the model must remember and becomes something\nthat runs after every edit.\n\nDesign constraints:\n\n  File-scoped, never project-wide. Hook output the agent has to re-read compounds\n  badly in a large refactor. A project-wide lint after every edit would dump\n  hundreds of pre-existing findings into context and train the reader to skim.\n\n  Lint only, no type-check or test, unless the project asks. A whole-project tsc or\n  test run is slow and would make every edit painful, so it is opt-in via\n  the config below. Verification of the full build stays step 4\n  of the coding workflow, where a human-visible failure belongs.\n\n  Never blocks. Exit is always 0 and the finding arrives as additionalContext. A lint\n  warning is information, not grounds for refusing an edit that may be one of five in\n  a sequence -- and a PostToolUse deny cannot un-write the file anyway.\n\n  Silent when clean, and silent when the project has no linter installed. A hook that\n  says "nothing to do" on every edit is noise that costs context every time.\n\n  No invented commands. Auto-detection fires only when the tool is present\n  in the project (node_modules/.bin) or on PATH and the project carries its config.\n  Anything else needs an explicit declaration in .agents/post-edit-verify.json:\n\n      { "\\.py$": "ruff check {file}", "\\.swift$": "swiftlint lint --path {file}" }\n\n  Keys are regexes matched against the path; {file} is substituted. An empty command\n  disables checking for that pattern.'
import json
import os
import re
import shlex
import subprocess
import sys
TYPE_CHECKING = False  
if TYPE_CHECKING:
    from typing import NoReturn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

CMD_TIMEOUT = 20          
MAX_LINES = 40            
CONFIG = os.path.join(".agents", "post-edit-verify.json")



SKIP_DIRS = (
    os.path.expanduser("~/.agent-context") + os.sep,
    hp.claude_home() + os.sep,
)
SKIP_PARTS = (os.sep + ".git" + os.sep, os.sep + "node_modules" + os.sep,
              os.sep + ".venv" + os.sep, os.sep + "build" + os.sep)


def out(context=None) -> "NoReturn":
    'Emit and exit. Always rc 0 -- see "Never blocks" above.'
    if context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": context}}))
    else:
        print(json.dumps({"suppressOutput": True}))
    sys.exit(0)


def project_root(path):
    "Nearest ancestor holding a .git, or None.\n\n    A worktree's .git is a file, so test existence and not isdir. Under the worktree\n    mandate that is the common case, and an isdir test here would disable this hook\n    for the edits it most needs to see."
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def declared(root, path):
    "The project's own command for this file, or None. Explicit beats detected.\n\n    `{file}` is substituted shell-quoted. The command runs through `shell=True`, so a\n    raw path is syntax there. A path holding a route group, a space or a `$`\n    (`src/app/(app)/page.tsx` is the ordinary Next.js case) would make `sh` report a\n    syntax error, which this hook would inject as if it were the project's own lint\n    output. That is worse than no check: it says the edit failed verification when\n    nothing was verified."
    try:
        with open(os.path.join(root, CONFIG)) as fh:
            table = json.load(fh)
    except (OSError, ValueError):
        return None
    for pattern, cmd in (table or {}).items():
        try:
            if re.search(pattern, path):
                
                return cmd.replace("{file}", shlex.quote(path)) if cmd else ""
        except re.error:
            continue
    return None


def detected(root, path):
    "A linter that is installed here, or None.\n\n    Both halves are required. A tool on PATH with no project config lints against its\n    own defaults and ignores the project's, which produces findings the project does not\n    want fixed -- and a rule nobody agreed to is how a guardrail gets ignored."
    ext = os.path.splitext(path)[1]
    
    
    
    q = shlex.quote(path)

    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        eslint = os.path.join(root, "node_modules", ".bin", "eslint")
        if os.access(eslint, os.X_OK):
            return "%s --no-error-on-unmatched-pattern %s" % (shlex.quote(eslint), q)
        return None

    if ext == ".py":
        has_cfg = any(os.path.exists(os.path.join(root, f))
                      for f in ("ruff.toml", ".ruff.toml", "pyproject.toml"))
        if has_cfg and which("ruff"):
            return "ruff check %s" % q
        return None

    if ext == ".swift":
        if os.path.exists(os.path.join(root, ".swiftlint.yml")) and which("swiftlint"):
            return "swiftlint lint --quiet --path %s" % q
        return None

    return None


def which(prog):
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if d and os.access(os.path.join(d, prog), os.X_OK):
            return True
    return False


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        out()

    ti = payload.get("tool_input") or {}
    path = ti.get("file_path") or ""
    if not path or not os.path.isfile(path):
        out()
    path = os.path.abspath(path)

    if path.startswith(SKIP_DIRS) or any(p in path for p in SKIP_PARTS):
        out()

    root = project_root(path)
    if not root:
        out()

    cmd = declared(root, path)
    if cmd == "":
        out()                       
    if cmd is None:
        cmd = detected(root, path)
    if not cmd:
        out()                       

    try:
        proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True,
                              text=True, timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        
        
        out("post-edit-verify: `%s` exceeded %ds and was abandoned; this edit is "
            "unverified.\nRun the project's own check before treating it as done."
            % (cmd, CMD_TIMEOUT))
    except OSError:
        out()

    if proc.returncode == 0:
        out()                       

    report = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not report:
        out()
    lines = report.splitlines()
    clipped = "\n".join(lines[:MAX_LINES])
    if len(lines) > MAX_LINES:
        clipped += "\n... +%d more line(s); re-run `%s` to see them all." % (
            len(lines) - MAX_LINES, cmd)

    out("post-edit-verify: the project's linter failed on %s:\n\n%s\n\n"
        "Fix every finding before moving on, whoever wrote the code. Name any false "
        "positive."
        % (os.path.relpath(path, root), clipped))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        
        
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "post-edit-verify crashed; this edit was not "
                                 "linted. Treat it as unverified."}}))
    sys.exit(0)
