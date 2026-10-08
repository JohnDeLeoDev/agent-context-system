#!/usr/bin/env python3
'PreToolUse(SendMessage|ListAgents): another session is messaged through the store (policy, policy).\n\nWhy. A message sent through the store arrives wrapped as\n`<cross-session-message from="name [ref]" via="agent-context">`, and Claude Code\'s own text\naround an arrival tells the agent to answer with the native SendMessage to the `from`\naddress. That address is the store\'s: the native tool cannot resolve it, so the reply\nfails or goes nowhere.\n\nWhat is a store address. Only what this session\'s transcript shows came from the store:\n  - the `from` of an envelope marked via="agent-context" (the daemon writes the mark;\n    Claude Code\'s own envelope has none), and\n  - a row of a store `list_agents` result.\nFor each, the label `name [ref]`, the bare name and the bare ref count, since send_message\ntakes all three. SendMessage to a store address is refused with the equivalent call (policy:\ncorrect the call; a hook cannot swap one tool for another, so it prints the one to make).\n\nListAgents is never refused, since it lists subagents and teammates; once a session it is\ntold where the other sessions are listed.\n\nFails open: no transcript, an unreadable one or any fault allows the call. The exception is\na `uds:` or `bridge:` address, which names another session and nothing else.'
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

HOME = os.path.expanduser("~")

ENVELOPE = re.compile(r'<cross-session-message from="([^"]*)" via="agent-context"[^>]*>')
LABEL = re.compile(r"^(.*?)\s*\[([^\]\s]+)\]$")

AGENT_TOOLS = ("Agent", "Task")
AGENT_ID = re.compile(r"\bagentId:\s*([\w.-]+)")

PEER_PREFIXES = ("uds:", "bridge:")


def state_dir():
    'Where the server keeps its state (paths.state_dir), as record-session-claim resolves it.'
    d = os.environ.get("AGENT_CONTEXT_STATE_DIR")
    if d:
        return d
    if sys.platform == "darwin":
        return os.path.join(HOME, "Library", "Application Support", "agent-context")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state")
    return os.path.join(base, "agent-context")


def _texts(content):
    "Every string a record's content holds: plain text, text blocks, tool results."
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield from _texts(block.get("text"))
                if block.get("type") == "tool_result":
                    yield from _texts(block.get("content"))


def _add(addresses, label):
    label = label.strip()
    if not label:
        return
    addresses.add(label)
    m = LABEL.match(label)
    if m:
        addresses.update(part for part in m.groups() if part)


def is_store_list(name):
    "The store's list_agents under any harness's naming, never the native ListAgents."
    return name == "list_agents" or name.endswith("__list_agents")


def transcript_addresses(transcript_path):
    "(store addresses, this session's own subagents and teammates), as the transcript\n    shows them."
    import transcript_records
    addresses, own, list_calls, agent_calls = set(), set(), set(), set()
    for rec in transcript_records.records(transcript_path):
        content = (rec.get("message") or {}).get("content")
        for text in _texts(content):
            for label in ENVELOPE.findall(text):
                _add(addresses, label.replace("&quot;", '"').replace("&amp;", "&"))
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in AGENT_TOOLS:
                agent_calls.add(block.get("id"))
                given = (block.get("input") or {}).get("name")
                if isinstance(given, str) and given.strip():
                    own.add(given.strip())
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in agent_calls:
                for text in _texts(block.get("content")):
                    own.update(AGENT_ID.findall(text))
            elif block.get("type") == "tool_use" and is_store_list(str(block.get("name") or "")):
                list_calls.add(block.get("id"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in list_calls:
                for text in _texts(block.get("content")):
                    for row in text.splitlines():
                        label = row.split(" · ")[0]
                        if LABEL.match(label.strip()):
                            _add(addresses, label)
    return addresses, own


def first_time(session):
    'True once per session; True again on any bookkeeping fault.'
    if not session:
        return True
    try:
        d = os.path.join(state_dir(), "nudges")
        os.makedirs(d, exist_ok=True)
        stamp = os.path.join(d, "native-list-agents-%s" % re.sub(r"[^\w.-]", "_", session))
        if os.path.exists(stamp):
            return False
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write("1\n")
    except OSError:
        pass
    return True


def main():
    data = json.load(sys.stdin)
    tool = str(data.get("tool_name") or "")

    if tool == "ListAgents":
        if first_time(str(data.get("session_id") or "").strip()):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    "redirect-native-peer-message: use ListAgents for this session's own "
                    "subagents and teammates. Every other session, on this machine or "
                    "another, is listed by the agent-context `list_agents` tool, with "
                    "`busy` or `idle`, and messaged with its `send_message`. A native "
                    "SendMessage to another session is refused.")}}))
        return 0

    if tool != "SendMessage":
        return 0
    args = data.get("tool_input") or {}
    to = str(args.get("to") or "").strip() if isinstance(args, dict) else ""
    if not to or data.get("agent_id"):
        return 0
    peer = to.startswith(PEER_PREFIXES)
    store, own = set(), set()
    try:
        if data.get("transcript_path"):
            store, own = transcript_addresses(data["transcript_path"])
        elif not peer:
            return 0
    except Exception:
        if not peer:
            return 0

    if to in store:
        reason = (
            "redirect-native-peer-message: %s is an agent-context store address, which the "
            "native SendMessage cannot resolve.\n"
            "Send it with the store's tool, the other arguments unchanged:\n"
            "    mcp__agent-context__send_message(to=%s, message=...)\n"
            "A subagent or teammate of this session stays on SendMessage."
            % (json.dumps(to), json.dumps(to)))
    elif to in own and not peer:
        return 0
    else:
        reason = (
            "redirect-native-peer-message: %s is no subagent or teammate this session "
            "started, so it is another session. Another session is messaged through the "
            "agent-context store, never with the native SendMessage (policy):\n"
            "    mcp__agent-context__list_agents()\n"
            "    mcp__agent-context__send_message(to=<its name in that list>, message=...)\n"
            "A subagent or teammate stays on SendMessage, addressed by the agentId its "
            "Agent call returned or the name that call gave it. A session the store does "
            "not list has no store bridge and cannot be messaged: tell user.\n"
            "If this session has no such tools: where tool schemas are deferred, load them "
            "with ToolSearch(\"select:mcp__agent-context__list_agents,"
            "mcp__agent-context__send_message\"). If that finds none, this session's tool "
            "list is from before the tools existed and only `/mcp` reconnect or a new "
            "session brings them (policy): say so to user in your reply now, with the "
            "message you could not send, and keep the unsent text."
            % json.dumps(to))
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        sys.exit(0)
