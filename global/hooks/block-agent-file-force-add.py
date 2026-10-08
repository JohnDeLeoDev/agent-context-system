#!/usr/bin/env python3
'PreToolUse(Bash): refuse to force-add an agent file into a project repo.\n\nNARROW ON PURPOSE. It fires only on an explicit force flag together with an\nagent path. A plain `git add .` is untouched: the exclude block already handles\nit, and blocking the ordinary spelling would make routine work painful for no\ngain.'
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp




EXEMPT_MARKERS = (".agent-context", "/.local/share/chezmoi")

FORCE = {"-f", "--force"}
OPTS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--chmod",
                   "--pathspec-from-file"}

PARSE_FAILED_DENY = """BLOCKED by ~/.agent-context/global/hooks/block-agent-file-force-add.py:

This command names an agent path and a git add, but could not be parsed,
so the hook cannot tell whether it force-adds one. It fails closed.

Split it into plain, separate commands and re-run.
"""

MISSING_SCAN_DENY = """BLOCKED by ~/.agent-context/global/hooks/block-agent-file-force-add.py:

The shared shell parser is missing, so this command cannot be judged:
  %s

That is a broken projection, not a problem with your command.
Repair it with:  python3 ~/.agent-context/global/scripts/home-materialize.py
"""

FORCE_ADD_DENY = """BLOCKED by ~/.agent-context/global/hooks/block-agent-file-force-add.py:

This force-adds an agent file into a project repo:

%s
NOTHING agent-related is ever committed to a project repo -- no exceptions, not
even .agents/project-id. Memory: never-commit-agent-files-into-a-project-repo.

`.agents/` and `.claude/` are already in .git/info/exclude, which is why a plain
`git add .` skips them. `-f` is the one thing that overrides that, so it is the
one thing this hook refuses.

Where the content belongs:
  - Instructions, docs, memories, skills, hooks, scripts -> the agent-context
    store, via its MCP tools (upsert_doc / upsert_memory / upsert_hook / ...).
  - Per-project agent config -> .agents/ on disk, which is gitignored
    and reconstituted by project-materialize.py on any machine.
  - .claude/ is a GENERATED projection. Committing it commits a build artifact.

If a project needs a tracked agent file, that is a decision for user,
not a flag.
"""


def is_agent_path(text):
    'A path INSIDE a .agents/ or .claude/ tree, at any depth.\n\n    NOT `lstrip("./")`. str.lstrip takes a SET OF CHARACTERS, not a prefix, so it eats\n    the leading dot of `.claude/settings.json` and leaves `claude/settings.json`, which\n    matches nothing -- the guard then allowed every top-level spelling while still\n    catching `src/.claude/foo`. Caught by running the cases before trusting it.'
    p = text.strip().replace("\\", "/")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    return any(seg in hp.HARNESS_DIRNAMES for seg in parts)


def git_at(argv, i):
    t = argv[i]
    if t.quoted:                       
        return False
    return os.path.basename(t.text) == "git"


def find_blocked(raw, scan_path):
    'List of force-added agent paths, or None on any parse failure (fail closed).'
    try:
        spec = importlib.util.spec_from_file_location("scs", scan_path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load " + scan_path)
        scs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scs)
        data = json.loads(raw)
        command, cwd = scs.payload_command(data)
        _, segments = scs.parse(command, cwd)
    except Exception:
        return None

    blocked = []
    for _dir, argv, *_ in segments:
        if any(m in (_dir or "") for m in EXEMPT_MARKERS):
            continue
        i = 0
        while i < len(argv):
            if not git_at(argv, i):
                i += 1
                continue
            j = i + 1
            while j < len(argv):                       
                t = argv[j].text
                if t in OPTS_WITH_VALUE:
                    j += 2
                    continue
                if t.startswith("--") and "=" in t:
                    j += 1
                    continue
                break
            if j < len(argv) and argv[j].text == "add" and not argv[j].quoted:
                rest = argv[j + 1:]
                forced = any(a.text in FORCE for a in rest if not a.quoted)
                
                for a in rest:
                    if (not a.quoted and a.text.startswith("-")
                            and not a.text.startswith("--") and "f" in a.text[1:]):
                        forced = True
                if forced:
                    for a in rest:
                        if a.text.startswith("-"):
                            continue
                        if is_agent_path(a.text):
                            blocked.append(a.text)
                i = j + 1
                continue
            i += 1
    return blocked


def main():
    raw = sys.stdin.read()

    
    if hp.AGENTS_DIRNAME not in raw and hp.CLAUDE_DIRNAME not in raw:
        return 0
    if "add" not in raw:
        return 0

    scan_path = os.environ.get("SHELL_COMMAND_SCAN") or os.path.join(
        hp.scripts_dir(), "shell-command-scan.py")
    if not os.path.isfile(scan_path):
        
        
        
        sys.stderr.write(MISSING_SCAN_DENY % scan_path)
        return 2

    blocked = find_blocked(raw, scan_path)
    if blocked is None:
        sys.stderr.write(PARSE_FAILED_DENY)
        return 2
    if blocked:
        listed = "".join("  %s\n" % b for b in blocked[:5])
        sys.stderr.write(FORCE_ADD_DENY % listed)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("block-agent-file-force-add: failed open on an internal error: %r\n" % (exc,))
        sys.exit(0)
