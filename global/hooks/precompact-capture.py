#!/usr/bin/env python3
"precompact-capture — save what a compaction summary reliably loses.\n\nWHY CAPTURE HERE AND DELIVER LATER. A PreCompact hook could try to steer the summary,\nbut whether its output reaches the summarizer is exactly the kind of thing this store\nhas now twice been wrong about by assuming. So this hook only writes a FILE -- an act\nwith an observable result that a test can assert -- and the delivery happens on the\nSessionStart that follows, through `additionalContext`, which was verified end to end by\nprobe and appears in the transcript as a `hook_additional_context` record.\n\nWHAT IS WORTH KEEPING is deliberately narrow: facts that are expensive to rediscover and\ncheap to record. Which files are modified and where, which branch and worktree, whether\nthe store itself is dirty. Not a task narrative -- the summary is already trying to do\nthat, and a second worse copy would just spend context. The transcript adds two things git\ncannot supply, read without a model call: the files the session edited (a committed file\nleaves `git status`) and the user's last few real asks (an early correction is what a\nsummary drops first).\n\nNever blocks; a compaction must proceed whatever happens here."
import collections
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

STATE = os.environ.get("AGENT_CONTEXT_STATE_DIR",
                       os.path.expanduser("~/.local/state/agent-context"))
OUT = os.path.join(STATE, "precompact")
MAX_FILES = 40





MAX_EDITED = 30
MAX_ASKS = 10
ASK_CHARS = 200
SCAN_SECONDS = 8                      
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
NOISE = ("<command-", "<local-command-", "<ci-monitor-event>", "Stop hook feedback:",
         "[Request interrupted", "<task-notification>",
         "This session is being continued from a previous conversation")


NOT_TYPED_ORIGINS = ("task-notification",)


def user_ask(rec):
    'The text of a real user ask, or None for anything the harness or a tool wrote.'
    if rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary"):
        return None
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") in NOT_TYPED_ORIGINS:
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None              
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        text = "\n".join(parts)
    else:
        return None
    text = " ".join(REMINDER.sub("", text).split())
    if not text or text.startswith(NOISE):
        return None
    return text if len(text) <= ASK_CHARS else text[:ASK_CHARS - 1] + "…"


STORE_PREFIX = "mcp__agent-context__"


def store_write(tool, args):
    '`kind:name` for a store entity written through an MCP tool, else None.\n\n    The store blocks Edit and Write on entity files, so a session changes a hook, script\n    or doc through edit_body (kind and key) or an upsert_<kind> tool (a name).'
    if not isinstance(tool, str) or not tool.startswith(STORE_PREFIX) \
            or not isinstance(args, dict):
        return None
    short = tool[len(STORE_PREFIX):]
    if short == "edit_body":
        kind, name = args.get("kind"), args.get("key")
    elif short.startswith("upsert_"):
        kind = short[len("upsert_"):]
        name = args.get("name") or args.get("slug") or args.get("key")
    else:
        return None
    if isinstance(kind, str) and kind and isinstance(name, str) and name:
        return "%s:%s" % (kind, name)
    return None


def remember(seen, key, tool_id, pending):
    'Record a write at the newest slot, and note how to undo it if the call fails.'
    pending[tool_id] = (seen, key, key in seen)
    seen.pop(key, None)                   
    seen[key] = None


def drop_failed(rec, pending):
    'Forget a write whose tool_result says it errored: the change never landed.\n\n    An entry that an earlier successful call already put there stays.'
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_result" \
                or block.get("is_error") is not True:
            continue
        undo = pending.get(block.get("tool_use_id"))
        if undo and not undo[2]:
            undo[0].pop(undo[1], None)


def scan_transcript(path):
    '(edited files, recent asks, store entities written), each oldest first.\n\n    Empty on any problem.'
    edited = {}
    written = {}
    pending = {}                          
    asks = collections.deque(maxlen=MAX_ASKS)
    if not path or not os.path.isfile(path):
        return [], [], []
    deadline = time.monotonic() + SCAN_SECONDS
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if time.monotonic() > deadline:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") == "user":
                    drop_failed(rec, pending)
                    ask = user_ask(rec)
                    if ask:
                        asks.append(ask)
                elif rec.get("type") == "assistant":
                    msg = rec.get("message")
                    content = msg.get("content") if isinstance(msg, dict) else None
                    for block in content if isinstance(content, list) else []:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        args = block.get("input")
                        entity = store_write(block.get("name"), args)
                        if entity:
                            remember(written, entity, block.get("id"), pending)
                            continue
                        if block.get("name") not in EDIT_TOOLS:
                            continue
                        target = args.get("file_path") or args.get("notebook_path") \
                            if isinstance(args, dict) else None
                        if isinstance(target, str) and target:
                            remember(edited, target, block.get("id"), pending)
    except OSError:
        pass
    return list(edited)[-MAX_EDITED:], list(asks), list(written)[-MAX_EDITED:]


def git(args, cwd):
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, timeout=10)
        return (p.stdout or "").strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def snapshot(cwd):
    if not cwd or not os.path.isdir(cwd):
        return {}
    top = git(["rev-parse", "--show-toplevel"], cwd)
    if not top:
        return {"cwd": cwd}
    status = git(["status", "--short"], top)
    files = [ln.strip() for ln in status.splitlines() if ln.strip()][:MAX_FILES]
    return {
        "cwd": cwd,
        "repo": top,
        "branch": git(["rev-parse", "--abbrev-ref", "HEAD"], top),
        "head": git(["rev-parse", "--short", "HEAD"], top),
        
        
        
        "worktree": next((top.split(mark, 1)[1] for mark in ("/" + scope + "/worktrees/" for scope in hp.HARNESS_DIRNAMES) if mark in top), None),
        "modified": files,
        "modified_count": len([ln for ln in status.splitlines() if ln.strip()]),
    }


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    session = payload.get("session_id") or "nosession"
    record = {
        "session": session,
        "trigger": payload.get("trigger"),
        "work": snapshot(payload.get("cwd") or os.getcwd()),
        
        
        "store": snapshot(os.environ.get("AGENT_CONTEXT_STORE",
                                         os.path.expanduser("~/.agent-context"))),
    }
    (record["edited_this_session"], record["recent_user_asks"],
     record["store_entities_written"]) = scan_transcript(payload.get("transcript_path"))
    try:
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, "%s.json" % session), "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=1)
    except OSError:
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:                 
        pass
    sys.exit(0)
