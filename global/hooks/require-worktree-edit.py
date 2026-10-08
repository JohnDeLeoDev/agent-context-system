#!/usr/bin/env python3

'PreToolUse(Edit|Write|MultiEdit|NotebookEdit): block edits to project source\noutside a git worktree. Enforces "every change to project source must be made\ninside an isolated git worktree."\n\nThe store is edited from the store. The repo check below asks "which repo is\nthe file in", never "which repo is the session in", so without this rule a project\nsession could hand-edit ~/.agent-context files. The store changes from a session\nopened inside ~/.agent-context, or through the MCP tools from anywhere; anything\nelse is refused outright.\n\nStore entities are MCP-only. A store entity is a file on disk and a\nrow in the server\'s in-memory index. Edit/Write updates only the file, and the\nnext MCP write rewrites it from the index, discarding the change with no error.\nSkill SKILL.md is an entity; everything else under skills/<name>/ is a bundled\nfile no MCP tool addresses, so it stays writable: it has no other route to a fix.\n\nThe store\'s own server is project source (server/, unlike every other store\npath): gated by pytest, self-deployed, landed through store-wt-finish.py. Its\ndocs and worktree copies stay exempt like any repo\'s.\n\nCorrect where possible. A session with a claimed worktree for this repo has its\nmain-checkout edit moved there and not refused: a new Write goes straight\nthere, an Edit goes there once the worktree copy has been read this session (the\nharness refuses to edit what was never read), otherwise it is still refused but\nnames the exact worktree path to Read first.\n\nScratch clones under a temp root are exempt (a decoy worktree inside\none just to satisfy a path check protects nothing). Gitignored paths are exempt\n(git itself says they are not tracked source).'
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

PATH_KEYS = ("file_path", "filePath", "path", "notebook_path", "notebookPath",
             "target_file", "targetFile")

ENTITY_PATTERNS = (
    "*/.agent-context/*/hooks/*", "*/.agent-context/*/scripts/*",
    "*/.agent-context/*/agents/*", "*/.agent-context/*/commands/*",
    "*/.agent-context/*/skills/*", "*/.agent-context/*/docs/*",
    "*/.agent-context/*/memory/*", "*/.agent-context/*/instructions/*",
)





TEMPLATE_RE = re.compile(r"/\.agent-context/(?:\.agents/worktrees/[^/]+/)?templates/")

TEMP_ROOTS = ("/tmp/*", "/private/tmp/*", "/var/tmp/*", "/private/var/tmp/*",
              "/var/folders/*", "/private/var/folders/*")


def glob_match(pattern, s):
    'Match a shell `case` glob (only `*` is special, and it matches `/` too).'
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, s) is not None


def any_glob(patterns, s):
    return any(glob_match(p, s) for p in patterns)


def extract_path(data):
    'The file_path under any harness\'s key spelling; "" if there is none.'
    ti = data.get("tool_input") or data.get("tool_args") or data.get("params") or {}
    if not isinstance(ti, dict):
        ti = {}
    for k in PATH_KEYS:
        if ti.get(k):
            return ti[k]
    return data.get("file_path") or data.get("filePath") or ""


def cksum(s):
    return subprocess.run(["cksum"], input=s, capture_output=True, text=True).stdout.split()[0]


def worktree_redirect(data, top, fp):
    'None (no redirect possible), a dict to print as the allow+redirect JSON, or\n    the string "hint:<worktree path>" when the file must be read there first.'
    sid = str(data.get("session_id") or "").strip()
    agent = str(data.get("agent_id") or "").strip()
    if not (top and fp and sid):
        return None
    home = os.path.expanduser("~")
    state = os.environ.get("AGENT_CONTEXT_STATE_DIR")
    if not state:
        state = (os.path.join(home, "Library", "Application Support", "agent-context")
                 if sys.platform == "darwin" else
                 os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"),
                              "agent-context"))
    try:
        with open(os.path.join(state, "claims", re.sub(r"[^\w.-]", "_", sid) + ".json")) as fh:
            name = (json.load(fh) or {}).get("worktree")
    except Exception:
        return None
    if not name:
        return None
    
    
    
    logical = os.path.abspath(fp)
    resolved = os.path.join(os.path.realpath(os.path.dirname(logical)), os.path.basename(logical))
    rel = os.path.relpath(resolved, os.path.realpath(top))
    if rel.startswith(".."):
        return None
    suffix = os.sep + rel
    logical_top = logical[: -len(suffix)] if logical.endswith(suffix) else top
    wt = next((os.path.join(logical_top, scope, "worktrees", name)
               for scope in hp.HARNESS_DIRNAMES
               if os.path.exists(os.path.join(logical_top, scope, "worktrees", name, ".git"))), None)
    if wt is None:
        return None
    target = os.path.join(wt, rel)

    ledger = os.path.join(home, ".local", "state", "agent-context", "read-ledger", sid or "nosession")
    if agent:
        ledger = os.path.join(ledger, "agent-" + agent)
    key = cksum(os.path.abspath(fp))
    sig = cksum(target)
    was_read = any(os.path.exists(os.path.join(ledger, p + ".sig." + sig)) for p in (key, "b_" + key))
    tool = data.get("tool_name") or ""
    if (tool == "Write" and not os.path.exists(target)) or was_read:
        ti = dict(data.get("tool_input") or {})
        for k in PATH_KEYS:
            if ti.get(k):
                ti[k] = target
                break
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": ti,
            "additionalContext": (
                "REDIRECTED by require-worktree-edit: `%s` -> `%s`. This session's worktree is "
                "%s; the edit was made there, not in the main checkout. Address the worktree "
                "path yourself from now on, and land with wt-finish.sh when done." % (fp, target, wt))}}
    return "hint:" + target


CROSS_PROJECT_DENY = """BLOCKED by require-worktree-edit: a session working in
  %s
is writing a file inside the agent-context store:
  %s
The store is changed from a session opened inside ~/.agent-context (its server code, in
a worktree there), or through the MCP tools (upsert_*/edit_body, for entities) from
anywhere. A project session editing store files by hand can leave the store unable
to sync. If this change is needed, say so to
user and do it from a store session.
"""

ENTITY_DENY = """BLOCKED by require-worktree-edit: this is an agent-context STORE ENTITY, and store
entities are written through the MCP tools, never as files on disk.
  %s
A store entity is two things: the file, and a row in the server's in-memory index. An
Edit/Write updates only the file, so this session keeps serving the stale body -- and
the next MCP write rewrites the file from the index, discarding your change outright.

Use the tool for the kind:
  hooks/        upsert_hook   |  scripts/      upsert_script
  agents/       upsert_agent_definition       |  commands/  upsert_command
  docs/         upsert_doc                    |  memory/    upsert_memory
  instructions/ upsert_instruction            |  skills/    upsert_skill

Prefer edit_body(kind, key, old_string, new_string) for a surgical change: upsert_*
resets fields you omit, with no error (a hook's language, for example).
Note the key carries no extension: "require-worktree-edit", not the
".sh" filename.
"""

SERVER_DENY = """BLOCKED by require-worktree-edit hook: editing the agent-context SERVER in the main checkout.
  %s
server/ is gated, self-deployed code: every change is made in a worktree and landed
from outside it (get_doc("worktrees.md"), section "The agent-context store itself"):
  git -C ~/.agent-context worktree add .agents/worktrees/<short-desc> -b <short-desc>
  # edit there, commit (signed), then, from the main checkout:
  python3 ~/.agent-context/global/scripts/store-wt-finish.py ~/.agent-context/.agents/worktrees/<short-desc>
  python3 ~/.agent-context/global/scripts/store-landed.py <sha>   # landed is not shipped
Store entities (hooks, scripts, docs, memory) are not this: they go through the MCP tools.
"""

GENERIC_DENY = """BLOCKED by require-worktree-edit hook: editing project source in the main checkout.
  %s
Every project-source change must be made in a git worktree, then landed:
  git worktree add .agents/worktrees/<short-desc> -b <short-desc>
  # cd into the worktree, edit there, then: bash <main>/.agents/scripts/wt-finish.sh
Exempt: *.md docs, .claude/, .agents/, .agent-context/ non-entity files, Secrets/, .git/ (local repo
config), chezmoi + example.invalid repos, gitignored paths (build output), and scratch
clones under a temp root.
"""

HINT_DENY = """BLOCKED by require-worktree-edit hook: editing project source in the main checkout.
  %s
This session already has a worktree for this repo; the same file there is
  %s
Read THAT copy, then Edit it there (an edit is moved there for you once the worktree
copy has been read this session, and a new file is moved there right away).
"""


def git_out(args, cwd_dir):
    try:
        proc = subprocess.run(["git", "-C", cwd_dir] + args, capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def main():
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    fp = extract_path(data)
    if not fp:
        return 0
    session_cwd = data.get("cwd")
    session_cwd = session_cwd if isinstance(session_cwd, str) else ""

    
    if glob_match("*/.agent-context/*", fp):
        ok = (session_cwd == "" or glob_match("*/.agent-context", session_cwd)
              or glob_match("*/.agent-context/*", session_cwd))
        if not ok:
            sys.stderr.write(CROSS_PROJECT_DENY % (session_cwd, fp))
            return 2

    
    if glob_match("*/.agent-context/*/skills/*/SKILL.md", fp):
        pass  
    elif glob_match("*/.agent-context/*/skills/*/*", fp):
        return 0

    
    if any_glob(ENTITY_PATTERNS, fp) and not TEMPLATE_RE.search(fp):
        sys.stderr.write(ENTITY_DENY % fp)
        return 2

    
    store_server = False
    if glob_match("*/.agent-context/server/*", fp):
        if glob_match("*.md", fp) or glob_match("*/worktrees/*", fp):
            return 0
        store_server = True

    if glob_match("*/.agent-context/server/*", fp):
        pass  
    elif any_glob(("*.md", "*/" + hp.CLAUDE_DIRNAME + "/*", "*/.agents/*", "*/.agent-context/*",
                   "*/worktrees/*", "*/Secrets/*"), fp):
        return 0
    elif glob_match("*/.git/*", fp):
        return 0
    elif False:
        return 0
    elif False:
        return 0

    
    d = fp
    while d and d != "/" and not os.path.exists(d):
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    if not os.path.exists(d):
        return 0
    if os.path.isfile(d):
        d = os.path.dirname(d)

    if git_out(["rev-parse", "--is-inside-work-tree"], d) is None:
        return 0
    gitdir = git_out(["rev-parse", "--git-dir"], d) or ""
    if "/worktrees/" in gitdir:
        return 0

    top = git_out(["rev-parse", "--show-toplevel"], d) or ""
    if top:
        try:
            rp = os.path.realpath(top)
        except OSError:
            rp = top
        if any_glob(TEMP_ROOTS, rp):
            return 0
        tmpdir = os.environ.get("TMPDIR")
        if tmpdir and rp.startswith(tmpdir.rstrip("/") + "/"):
            return 0

    try:
        ignored = subprocess.run(["git", "-C", d, "check-ignore", "-q", fp],
                                 capture_output=True).returncode == 0
    except OSError:
        ignored = False
    if ignored:
        return 0

    redirect = worktree_redirect(data, top, fp)
    if isinstance(redirect, dict):
        print(json.dumps(redirect))
        return 0
    if isinstance(redirect, str) and redirect.startswith("hint:"):
        sys.stderr.write(HINT_DENY % (fp, redirect[len("hint:"):]))
        return 2

    if store_server:
        sys.stderr.write(SERVER_DENY % fp)
        return 2

    sys.stderr.write(GENERIC_DENY % fp)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("require-worktree-edit: failed open on an internal error: %r\n" % (exc,))
        sys.exit(0)
