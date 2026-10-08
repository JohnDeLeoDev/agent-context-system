#!/usr/bin/env python3
"PreToolUse gate: no code work while this session's language server is down.\n\nuser's rule: never work on code without a working LSP, in any project. Disclosing\nthe degradation and carrying on with grep does not satisfy it.\n\nThe rule also lives as memory `never-proceed-without-lsp`, but a memory reaches only\nsessions that loaded it and a SessionStart banner is a snapshot of t=0. So it is\nenforced by this hook, which is global scope and carries no project condition: it\nbinds in every repository on every machine.\n\nWhat it blocks: source edits and subagent/workflow spawns. Both are the acts that turn a\nwrong grep into landed work or fan it out to workers who will repeat it.\n\nWhat it does not block, because the point is to get the server fixed:\n  - Bash             -- diagnosis and repair (ps, kill an orphan, restart a bridge)\n  - the LSP tools    -- the agent must be able to retest; a success auto-clears the flag\n  - prose and agent-infrastructure paths (*.md, .agents/, .claude/, .agent-context/)\n    -- writing the ledger, an inbox note or a memory is not code work and needs no\n      symbol resolution.\nA gate people route around is worse than no gate, so the escape hatches are the honest\nones and nothing else."
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp




EXEMPT = re.compile(
    r'\.(?:md|markdown|txt|rst|json5?|ya?ml|toml)$'
    r'|/\.agents?/'
    r'|/' + re.escape(hp.CLAUDE_DIRNAME) + r'/'
    r'|/\.agent-context/'
    r'|(?:^|/)(?:AGENTS|CLAUDE|README)\.md$',
    re.I)


def main() -> int:
    
    
    
    
    
    state_dir = os.environ.get("LSP_DOWN_STATE_DIR") or os.path.join(
        hp.state_dir(), "lsp-down")

    try:
        d = json.loads(sys.stdin.read())
    except Exception:
        return 0  
    if not isinstance(d, dict):
        return 0

    sid = str(d.get("session_id") or "nosession")
    sid = re.sub(r'[^A-Za-z0-9_.-]', '_', sid)[:128] or "nosession"
    flag = os.path.join(state_dir, sid)
    if not os.path.isfile(flag):
        return 0  

    tool = str(d.get("tool_name") or "")
    args = d.get("tool_input") or {}
    if not isinstance(args, dict):
        args = {}

    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(args.get("file_path") or args.get("notebook_path") or "")
        if not path or EXEMPT.search(path):
            return 0
        what = "edit " + path
    elif tool in ("Task", "Agent", "Workflow"):
        
        
        
        what = "spawn a subagent/workflow"
    else:
        return 0

    detail = tool_at = count = ""
    try:
        with open(flag) as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k == "detail":
                    detail = v
                elif k == "tool":
                    tool_at = v
                elif k == "count":
                    count = v
    except OSError:
        pass

    sys.stderr.write(
        "require-working-lsp: refused to %s: the language server is down in this "
        "session (%s failed call(s), first from %s: %s).\n"
        "Stop code work and fix the server first; grep misses real callers. Bash, "
        "the LSP tools and writes to docs, .agents/ and .claude/ stay allowed.\n"
        "  1. ps -Ao pid,ppid,command | grep -iE 'lspd|lsp' | grep -v grep "
        "(is lspd alive with a live language-server child?)\n"
        "  2. lspd.py --restart --key <server> --workspace <main checkout>, then "
        "retry the LSP call. A success clears this gate. Do not kill the bridge, "
        "daemon or child by hand.\n"
        "  3. If the server is healthy but this session's MCP client gave up, ask "
        "the user to restart the session.\n"
        "See get_doc(\"lsp-daemon-operations.md\"); memory never-proceed-without-lsp.\n"
        % (what, count or "1", tool_at, detail)
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("require-working-lsp crashed and failed open: %r\n" % (exc,))
        sys.exit(0)
