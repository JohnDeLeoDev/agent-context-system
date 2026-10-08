#!/usr/bin/env python3
'SessionStart: report any harness-owned file memory that has reappeared on disk.\n\nBackstop for block-file-memory-write. That hook denies Write/Edit/MultiEdit/\nNotebookEdit into a harness memory directory, which covers how Claude Code writes\nthem today, but a future harness release could ship a dedicated memory tool under\na name no matcher predicts, and a sibling agent (Copilot, pi, opencode) has its own\nmechanisms entirely. This check is mechanism-independent: it looks at the disk.\n\nThe failure it catches is unreported drift. Memories written under\n~/.claude/projects/<cwd>/memory/ sit outside the store and nothing else reports\nthem. One session of drift is cheap to fix; weeks of it are not.\n\nOutput: { systemMessage } listing offenders, else silent.'
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp


def main():
    
    
    
    home = os.environ.get("HOME") or ""
    if not home:
        print(json.dumps({"suppressOutput": True}))
        return 0

    found = []
    for base_name in (hp.CLAUDE_DIRNAME, ".codex", ".copilot", ".opencode"):
        base = os.path.join(home, base_name)
        if not os.path.isdir(base):
            continue
        
        for d in _find_memory_dirs(base, max_depth=3):
            n = _count_md_files(d)
            if n > 0:
                found.append("  • %s  (%d .md file(s))" % (d, n))

    if found:
        msg = ("⚠ Harness file-based memory has reappeared on disk:\n"
               + "\n".join(found)
               + "\n\nThis is not the memory system. All persistent memory lives in "
               "the agent-context store (upsert_memory). Files here are "
               "machine-local, invisible to the other machines and agents, and "
               "loaded into every session outside the store's budget accounting.\n\n"
               "Fix: read each file, fold anything durable into the store with "
               "upsert_memory (search_all(query, kind=\"memory\") first; tier narrow ones lazy), then "
               "delete the directory with Bash. Do not leave it half-migrated -- two "
               "stores is the actual defect.")
        print(json.dumps({"systemMessage": msg}))
    else:
        print(json.dumps({"suppressOutput": True}))
    return 0


def _find_memory_dirs(base, max_depth):
    'Directories named exactly `memory`, at or below `base`, no more than\n    max_depth path components below it -- mirrors `find base -maxdepth N -type d\n    -name memory`.'
    base = base.rstrip(os.sep)
    base_depth = base.count(os.sep)
    out = []
    for root, dirs, _files in os.walk(base):
        depth = root.count(os.sep) - base_depth
        if depth >= max_depth:
            dirs[:] = []
            continue
        for d in list(dirs):
            if d == "memory":
                out.append(os.path.join(root, d))
    return out


def _count_md_files(d):
    n = 0
    for _root, _dirs, files in os.walk(d):
        n += sum(1 for f in files if f.endswith(".md"))
    return n


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
