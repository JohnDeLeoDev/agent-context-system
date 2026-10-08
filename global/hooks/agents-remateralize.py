#!/usr/bin/env python3

'PostToolUse: keep the .claude/ projection in step with its source, within a session.\n\n`.claude/` is a generated projection, rebuilt only by the SessionStart materialize\nhooks. Without this hook, an edit to a file under `.agents/` or an upsert of a\nscript or skill to the store looks like it took, but the copy the harness executes\nstays the old one until the next session, and nothing warns.\n\nTwo directions, two different repairs, and they are not interchangeable:\n\n  (a) Disk edit under .agents/  -> agents-materialize.py  (.agents -> .claude)\n      Must not be project-materialize: that overlays the store copy onto .agents\n      and would clobber the edit that just happened.\n\n  (b) Store write (upsert_*)    -> project-materialize.py (store -> .agents -> .claude)\n      The store is now ahead of disk, so the overlay is what is wanted.\n\nSilent on success. The tool call already succeeded and the rebuild is routine\nbookkeeping; announcing it on every write would train the reader to skim past\nthe one case that matters. It speaks only when the projection is stale, because\nthen the next thing to run under .claude/ is the wrong code.'
import json
import os
import re
import subprocess
import sys
import time

NON_PROJECTING_PREFIXES = ("get_", "list_", "search_", "check_", "translate_",
                            "set_", "register_")
NON_PROJECTING_EXACT = {
    "resolve_project", "resolve_audit_observation", "add_audit_observation",
    "update_audit_observation", "upsert_memory", "upsert_doc",
    "upsert_instruction", "upsert_project",
}
HOME_WRITE_TOOLS = {"upsert_hook", "upsert_script", "upsert_command", "upsert_skill",
                     "upsert_agent_definition"}
HOME_WRITE_KINDS = {"hook", "script", "command", "skill", "agent_definition"}
MCP_PREFIX = "mcp__agent-context__"


def strip_prefix(tool):
    return tool[len(MCP_PREFIX):] if tool.startswith(MCP_PREFIX) else tool


def collect_kinds(tool_input):
    kinds = []
    top = tool_input.get("kind")
    if top is not None:
        kinds.append(top)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict) and edit.get("kind") is not None:
                kinds.append(edit.get("kind"))
    
    seen = []
    for k in kinds:
        if k not in seen:
            seen.append(k)
    return sorted(seen, key=str)


def run(cmd, cwd=None):
    '(ok, combined_output) for a script run.'
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def tail_lines(text, n=5):
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  

    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    file_path = tool_input.get("file_path") or tool_input.get("pathInProject") or ""

    home = os.environ.get("HOME") or os.path.expanduser("~")
    scripts_dir = os.path.join(home, ".agent-context", "global", "scripts")
    session_id = data.get("session_id") or "nosession"

    
    
    
    
    
    
    server_prefix = os.path.join(home, ".agent-context", "server") + os.sep
    if file_path.startswith(server_prefix):
        stamp_dir = os.path.join(home, ".local", "state", "agent-context",
                                  "server-dirty-said")
        stamp = os.path.join(stamp_dir, session_id)
        if not os.path.exists(stamp):
            try:
                os.makedirs(stamp_dir, exist_ok=True)
                open(stamp, "w").close()
            except OSError:
                pass
            json.dump(
                {"systemMessage": (
                    "You just edited the agent-context server. Until server/ is "
                    "committed, the daemon defers every push and every machine "
                    "defers its self-redeploy -- the fleet stops converging and "
                    "nothing else will tell you. Clear it before you finish: "
                    "`python3 ~/.agent-context/global/scripts/release-server.py` "
                    "(gates on py_compile + pytest + ruff, then commits), or "
                    "revert the edit."
                )},
                sys.stdout,
            )
        return 0

    
    
    
    
    
    
    
    
    
    
    kinds = collect_kinds(tool_input)
    home_write = False
    bare_tool = strip_prefix(tool)
    if bare_tool in HOME_WRITE_TOOLS:
        home_write = True
    elif bare_tool in ("edit_body", "bulk_edit", "delete_entity"):
        if any(k in HOME_WRITE_KINDS for k in kinds):
            home_write = True

    
    
    if tool.startswith(MCP_PREFIX):
        rlog_dir = os.path.join(home, ".local", "state", "agent-context")
        rlog = os.path.join(rlog_dir, "remateralize-trace.jsonl")
        try:
            os.makedirs(rlog_dir, exist_ok=True)
            if os.path.exists(rlog) and os.path.getsize(rlog) > 5242880:
                os.replace(rlog, rlog + ".1")
            with open(rlog, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "sid": session_id,
                    "tool": bare_tool,
                    "kinds": ",".join(str(k) for k in kinds),
                    "home_write": home_write,
                }) + "\n")
        except OSError:
            pass

    if home_write:
        ok, out = run([sys.executable, os.path.join(scripts_dir, "home-materialize.py"), "--force"])
        if not ok:
            json.dump(
                {"systemMessage": (
                    "⚠ home-materialize failed after a write to a "
                    "home-projected entity, so ~/.claude still holds the old copy "
                    "and will execute that. Your write did not take effect:\n%s"
                    % tail_lines(out)
                )},
                sys.stdout,
            )
            return 0

    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or ""
    if not root:
        return 0
    if not os.path.isdir(os.path.join(root, ".agents")):
        return 0  

    script = what = None
    if tool in ("Write", "Edit", "MultiEdit"):
        
        
        
        if not re.search(r"/\.agents/scripts/|/\.agents/hooks/|/\.agents/claude/",
                          file_path):
            return 0
        script = os.path.join(scripts_dir, "agents-materialize.py")
        what = "agents-materialize (.agents -> .claude)"
    elif tool.startswith(MCP_PREFIX):
        
        
        
        
        if bare_tool in NON_PROJECTING_EXACT or any(
                bare_tool.startswith(p) for p in NON_PROJECTING_PREFIXES):
            return 0
        script = os.path.join(scripts_dir, "project-materialize.py")
        what = "project-materialize (store -> .agents -> .claude)"
    else:
        return 0

    if not os.path.isfile(script):
        json.dump(
            {"systemMessage": (
                "⚠ %s could not run -- %s is missing, so the .claude/ "
                "projection is now stale: the copy the harness executes is not "
                "the one you just changed. Re-materialize by hand or start a new "
                "session before running anything under .claude/." % (what, script)
            )},
            sys.stdout,
        )
        return 0

    ok, out = run([sys.executable, script, root])
    if not ok:
        json.dump(
            {"systemMessage": (
                "⚠ %s failed -- the .claude/ projection is stale, so any "
                "script, skill or command you just changed will still execute in "
                "its old form. Fix this before relying on it:\n%s"
                % (what, tail_lines(out))
            )},
            sys.stdout,
        )
        return 0

    json.dump({"suppressOutput": True}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("agents-remateralize crashed and failed open: %r\n" % (exc,))
        sys.exit(0)
