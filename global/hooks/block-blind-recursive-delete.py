#!/usr/bin/env python3

'PreToolUse(Bash): a recursive delete is never issued blind.\n\nWhy the rule exists. An agent can run `rm -rf` on a directory it never listed,\ndestroy work that lived there, and then report the directory as empty. A brief\nthat says "write no files" does not stop a delete, and a subagent inherits no\ninstructions, so a matcher is needed. The inventory this hook prints also puts\nwhat was there on record, so a false "it was empty" report can be checked.\n\nWhy it is no blanket refusal. `rm -rf build/`, `rm -rf node_modules`, and the\nmaterializer\'s own documented repair `rm -rf .claude && materialize` are\nordinary, correct work. So the policy is graded:\n\n  absent / a plain file / an empty directory  -> allow, no output.\n  a regenerable build artifact                -> allow, no output. It rebuilds.\n  a scratch root (.agents/tmp, the session\n    scratchpad, /tmp/claude-*)                -> allow, but print the inventory\n                                                  first. Cleaning scratch at\n                                                  turn end is a standing rule,\n                                                  and the inventory keeps a\n                                                  record of what was deleted.\n  a directory this session ran `ls` on in an\n    earlier call                              -> allow, and print the inventory.\n                                                  The agent has seen what is there.\n  anything else, non-empty                    -> block, and show what is inside.'
import json
import os
import re
import shlex
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

FAST_PATH = re.compile(r"(^|[^A-Za-z0-9_])rm([^A-Za-z0-9_]|$)|-delete([^A-Za-z0-9_]|$)")




RECURSIVE_RM = re.compile(r"(?<![\w-])rm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?![\w-])")
FIND_DELETE = re.compile(r"(?<![\w-])find\b[^|;&]*(?:-delete|-exec\s+rm\s+(?:-[a-zA-Z]*[rR]|--recursive))")



REGENERABLE = {
    "build", "builds", ".gradle", ".venv", "venv", "node_modules", "dist", "out",
    "obj", "bin", "target", "DerivedData", ".next", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".parcel-cache", ".turbo", hp.CLAUDE_DIRNAME,
}


def _tmp_roots():
    
    
    
    roots = set()
    for t in (tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/folders",
              "/private/var/folders"):
        try:
            roots.add(_slashed(os.path.realpath(t)).rstrip("/") + "/")
        except OSError:
            pass
    return roots


def _slashed(p):
    "`p` with forward slashes: Windows' realpath answers with backslashes (pc)."
    return p.replace(os.sep, "/")


def is_scratch(p, tmproots):
    'Scratch roots: deletion is routine and required at turn end, so never\n    blocked, but scratch can hold work in progress, so it is always itemized.'
    q = _slashed(p).rstrip("/") + "/"
    return (
        "/.agents/tmp/" in q
        or re.search(r"/claude-\d+/", q) is not None
        or any(q.startswith(r) for r in tmproots)
        or "/scratchpad" in q
    )


def is_regenerable(p):
    return any(part in REGENERABLE for part in p.split(os.sep) if part)


def _tokens(cmd):
    try:
        return shlex.split(cmd, comments=True)
    except ValueError:
        return cmd.split()                        


def listed_dirs(data, cmd):
    'Real paths of the directories an earlier Bash call of this session ran `ls` on.\n    The call being judged never counts, by its id or by its text.'
    import transcript_records
    this_id = data.get("tool_use_id")
    out = set()
    for rec in transcript_records.records(data.get("transcript_path")):
        content = (rec.get("message") or {}).get("content")
        if rec.get("type") != "assistant" or not isinstance(content, list):
            continue
        base = rec.get("cwd") or data.get("cwd") or os.getcwd()
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            earlier = (block.get("input") or {}).get("command")
            if (block.get("name") != "Bash" or not isinstance(earlier, str)
                    or earlier == cmd or (this_id and block.get("id") == this_id)):
                continue
            for part in re.split(r"[;&|\n]+", earlier):
                words = _tokens(part)
                if not words or words[0] != "ls":
                    continue
                operands = [w for w in words[1:] if not w.startswith("-")] or ["."]
                for w in operands:
                    out.add(os.path.realpath(os.path.join(base, os.path.expanduser(w))))
    return out


def inventory(entries, limit=25):
    shown = entries[:limit]
    more = len(entries) - len(shown)
    line = ", ".join(shown)
    if more > 0:
        line += ", ... and %d more" % more
    return line


def main():
    raw = sys.stdin.read()

    
    
    
    if not FAST_PATH.search(raw):
        return 0

    try:
        data = json.loads(raw)
        args = data.get("tool_input") or data.get("tool_args") or data.get("params") or {}
        cmd = (args.get("command") if isinstance(args, dict) else "") or data.get("command") or ""
    except Exception:
        return 0                                  
    if not cmd:
        return 0

    bare = re.sub(r"'[^']*'", "''", cmd)
    bare = re.sub(r'"[^"]*"', '""', bare)

    if not (RECURSIVE_RM.search(bare) or FIND_DELETE.search(bare)):
        return 0

    tmproots = _tmp_roots()

    
    
    
    
    tokens = _tokens(cmd)

    cwd = data.get("cwd") or os.getcwd()
    blocked, noted = [], []
    listed = None                                 
    for t in tokens:
        if not t or t.startswith("-"):
            continue
        cand = os.path.expanduser(t)
        if not os.path.isabs(cand):
            cand = os.path.join(cwd, cand)
        try:
            if not os.path.isdir(cand) or os.path.islink(cand):
                continue                          
            entries = sorted(os.listdir(cand))
        except OSError:
            continue                              
        if not entries:
            continue                              
        real = os.path.realpath(cand)
        if is_scratch(real, tmproots):
            noted.append((real, entries))
        elif is_regenerable(real):
            continue
        else:
            if listed is None:
                try:
                    listed = listed_dirs(data, cmd)
                except Exception:
                    listed = set()                
            (noted if real in listed else blocked).append((real, entries))

    if blocked:
        lines = []
        for path, entries in blocked:
            lines.append("  %s\n    %d entr%s: %s"
                         % (path, len(entries), "y" if len(entries) == 1 else "ies",
                            inventory(entries)))
        reason = (
            "BLOCKED by block-blind-recursive-delete: recursive delete of a non-empty directory "
            "that is not a build artifact or scratch root, and that this session has not "
            "listed. Nothing was deleted.\n"
            + "\n".join(lines) + "\n"
            "Next, one of:\n"
            "  - Run `ls -la <that directory>` as its own call, read it, then repeat the "
            "delete: a directory this session has listed is allowed.\n"
            "  - Delete the regenerable child (build/, dist/, node_modules/, .venv/).\n"
            "Do not report a directory as empty that you have not listed."
        )
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason}}))
        return 0

    if noted:
        lines = []
        for path, entries in noted:
            lines.append("  %s -- %d entr%s: %s"
                         % (path, len(entries), "y" if len(entries) == 1 else "ies",
                            inventory(entries)))
        msg = ("block-blind-recursive-delete: allowing a recursive delete of scratch or of a "
               "directory this session listed, and "
               "recording what was there so no later summary can call it empty:\n"
               + "\n".join(lines))
        print(json.dumps({"systemMessage": msg}))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        
        sys.exit(0)
