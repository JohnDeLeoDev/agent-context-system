#!/usr/bin/env python3

'block-worktree-move-mid-flight: refuse EnterWorktree/ExitWorktree while subagents run.\n\nEntering a worktree mid-session permanently breaks Bash for every subagent\nalready in flight, and they discover it only when a command fails. The harness\'s\nworktree-isolation check refuses any shell command from an agent whose cwd is the main\ncheckout once the parent session is worktree-isolated, including a bare `echo`, and the\nagent cannot recover, because its cwd was pinned at launch. The lead sees no error: the agent\'s findings\ncome back marked "read-only, unverified", which reads as ordinary caution and hides\nthe tooling failure. The work the agent could not verify is lost.\n\nWhy a hook and no instruction. A line of instruction would be paid for by every session\non every machine and read by an agent who has no reason to suspect the hazard when it\nmatters: the launch and the move are minutes apart and look unrelated. The hook fires at\nthe move, costs nothing otherwise, and states the two remedies.\n\nBoth directions. ExitWorktree is guarded for the same reason in reverse: agents launched\ninside the worktree have their cwd pinned there, and moving the session out from under\nthem strands them the same way. The defect is the session changing checkout while agents\nare pinned, which is symmetric.\n\nFails open. No transcript, an unreadable one, or a parse failure means no output.\nA guard that cannot see the agents must not stop the move: a false deny here would block\nlegitimate worktree work with no way for the agent to tell it is wrong.'
import json
import os
import re
import sys

LAUNCH_TOOLS = {"Task", "Agent", "Workflow"}

TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>.*?<tool-use-id>(?P<tool_use_id>[^<]*)</tool-use-id>"
    r".*?<status>(?P<status>[^<]*)</status>.*?</task-notification>", re.DOTALL)


def disabled():
    names = (os.environ.get("AGENT_CONTEXT_DISABLE_HOOKS") or "").split(",")
    return "block-worktree-move-mid-flight" in {n.strip() for n in names}


def in_flight(transcript_path):
    'Subagent launches with no result yet, as {tool_use_id: label}.\n\n    Keyed on the tool_use/tool_result pairing for a synchronous launch, because that\n    pairing is what the harness guarantees. A background launch\n    (run_in_background) gets its tool_result the moment it starts -- "Async agent\n    launched successfully", carrying an agentId, not a finding -- so that tool_result\n    does not clear it. A background launch only clears once a later turn carries a\n    <task-notification> naming its tool_use_id with <status>completed</status>; that\n    notification arrives as a "user" turn whose message content is a plain string, a\n    different shape from the tool_use/tool_result lists this loop otherwise reads, which\n    is why both are checked. A compact boundary resets the tally: lines before it\n    describe a context that no longer exists, and a launch whose result was summarized\n    away would otherwise wedge this guard on for the whole session.'
    launched, background, done = {}, set(), set()
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("subtype") == "compact_boundary" or "compact_boundary" in rec:
                launched, background, done = {}, set(), set()
                continue
            
            
            
            if rec.get("isSidechain"):
                continue
            
            
            
            
            if rec.get("type") in ("queue-operation", "attachment"):
                for m in TASK_NOTIFICATION_RE.finditer(line):
                    if m.group("status") == "completed":
                        done.add(m.group("tool_use_id"))
                continue
            content = ((rec.get("message") or {}).get("content"))
            if isinstance(content, str):
                for m in TASK_NOTIFICATION_RE.finditer(content):
                    if m.group("status") == "completed":
                        done.add(m.group("tool_use_id"))
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use" and block.get("name") in LAUNCH_TOOLS:
                    inp = block.get("input") or {}
                    label = (inp.get("description") or inp.get("subagent_type")
                             or block.get("name") or "subagent")
                    launched[block.get("id")] = str(label)
                    if inp.get("run_in_background"):
                        background.add(block.get("id"))
                elif kind == "tool_result":
                    
                    
                    if block.get("tool_use_id") not in background:
                        done.add(block.get("tool_use_id"))
    return {i: label for i, label in launched.items() if i and i not in done}


def main():
    if disabled():
        return 0
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    tool = payload.get("tool_name") or ""
    if tool not in ("EnterWorktree", "ExitWorktree"):
        return 0

    path = payload.get("transcript_path") or ""
    if not path or not os.path.exists(path):
        return 0                       
    try:
        running = in_flight(path)
    except OSError:
        return 0

    if not running:
        return 0

    names = "\n".join("  • %s" % label for label in list(running.values())[:6])
    sys.stderr.write(
        "BLOCKED by ~/.agent-context/global/hooks/block-worktree-move-mid-flight.py:\n\n"
        "%d subagent(s) are still running, and %s would break Bash for every one of "
        "them for the rest of their run:\n\n%s\n\n"
        "Their cwd was pinned at launch. Once this session changes checkout, the "
        "worktree-isolation check refuses every shell command they issue, including a "
        "bare `echo`, and they cannot recover. Worse, they do not report a tooling "
        "failure: their findings come back marked \"read-only, unverified\", which reads "
        "as ordinary caution. Work already done is thrown away without reporting it.\n\n"
        "Two ways forward:\n"
        "  • Wait for them to report, then move. You are notified when they finish; do "
        "not poll or sleep.\n"
        "  • Move first and launch them afterwards, so they inherit the worktree.\n"
        % (len(running), tool, names))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                  
        sys.exit(0)
