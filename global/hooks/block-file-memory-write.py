#!/usr/bin/env python3
"PreToolUse(Write|Edit|MultiEdit|NotebookEdit): block writes to a harness's own\nfile-based memory directory. All persistent memory belongs in the agent-context\nstore, via upsert_memory.\n\nWhy this exists. Claude Code's own memory tool writes to\n~/.claude/projects/<encoded-cwd>/memory/, and the harness loads the MEMORY.md there\ninto every session. Memory written there is outside the store, outside its budget\naccounting, and machine-local: other machines and other harnesses never see it, and\nnothing reports it.\n\nA soft rule is the wrong shape here: the harness's system prompt instructs the\nagent to write these files, so the agent is told two contradictory things and the\nstore loses by default. A hook settles it.\n\nDeleting or moving such a directory is not blocked, only creating or\nediting content in it. Cleanup uses Bash (rm/mv), which this hook never sees.\nBackstop for anything that bypasses these tools: the file-memory-drift-check\nSessionStart hook reports any such directory that reappears.\n\nInput (stdin JSON): { tool_name, tool_input: { file_path | notebook_path, ... } }\nOutput: permissionDecision=deny + reason, or allow silently."
import json
import re
import sys







MEMORY_DIR = re.compile(
    r"/\.(claude|codex|copilot|opencode)/(projects/[^/]+/)?memory(/|$)"
)

REASON = (
    "BLOCKED: `{fp}` is in a harness file-based memory directory; memory lives in the store.\n"
    "  • search_all(query, kind=\"memory\") first, then mcp__agent-context__upsert_memory(slug, "
    "memory_type=feedback|project|reference|user, description, body[, project])\n"
    "  • For a narrow memory, add load_behavior=\"lazy\".\n"
    "  • To clean up such a directory, use Bash (rm/mv)."
)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not fp:
        return 0

    if MEMORY_DIR.search(fp):
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": REASON.format(fp=fp),
                }
            },
            sys.stdout,
        )
        return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("block-file-memory-write crashed and failed open: %r\n" % (exc,))
        sys.exit(0)
