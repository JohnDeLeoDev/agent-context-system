#!/usr/bin/env python3
"failure-trace — record every tool call that failed, so a bad turn is diagnosable later.\n\nWhy this exists. The fleet's other telemetry (token-usage-collect, the read ledger,\nread-telemetry) measures cost. Without this log, the only artifact of a turn that went\nwrong is the transcript, and reconstructing what happened means re-reading it by hand.\nA failure seen late in a turn usually has an earlier cause, so the useful record is the\nstep where the chain first broke, with its inputs.\n\nWhat it records. Only failures. A success line per tool call would be a second copy of\nthe transcript at enormous volume, and the fleet already has cost telemetry for the\nvolume question. The record carries the session and agent id so a subagent's failure is\nattributable to the subagent and never to its parent, plus enough of the arguments to\nrecognize the call and the head of the error.\n\nRedaction is required. Tool arguments carry file contents, command lines and\noccasionally secrets. Only a short, truncated summary of scalar arguments is kept, long\nvalues are cut, and anything whose key looks credential-shaped is dropped entirely. A\ndiagnostic log that quietly becomes a secret store is a worse problem than the one it\nsolves.\n\nSilent, non-blocking, best-effort: a failure to record must never turn a recoverable\ntool error into a broken turn."
import json
import os
import re
import sys
import time






STATE_DIR = os.environ.get("AGENT_CONTEXT_STATE_DIR",
                           os.path.expanduser("~/.local/state/agent-context"))
LOG = os.path.join(STATE_DIR, "failure-trace.jsonl")
MAX_BYTES = 8 * 1024 * 1024          
ARG_CHARS = 160                      
ERR_CHARS = 400                      

SECRETISH = re.compile(
    r"(token|secret|password|passwd|credential|api[_-]?key|auth|cookie|session[_-]?key)",
    re.I)


def summarize(args):
    "A recognisable, redacted sketch of the call's arguments."
    out = {}
    if not isinstance(args, dict):
        return out
    for k, v in list(args.items())[:12]:
        if SECRETISH.search(str(k)):
            out[k] = "<redacted>"
            continue
        if isinstance(v, (dict, list)):
            out[k] = "<%s len=%d>" % (type(v).__name__, len(v))
            continue
        s = str(v)
        out[k] = s if len(s) <= ARG_CHARS else s[:ARG_CHARS] + "…"
    return out


def rotate():
    try:
        if os.path.getsize(LOG) > MAX_BYTES:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return

    
    
    
    
    if payload.get("hook_event_name") != "PostToolUseFailure":
        return
    err = str(payload.get("error") or "tool failed (no error text)")

    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session": payload.get("session_id") or "",
        
        
        "agent": payload.get("agent_id") or "",
        "agent_type": payload.get("agent_type") or "",
        "tool": payload.get("tool_name") or "",
        "cwd": payload.get("cwd") or "",
        "args": summarize(payload.get("tool_input")),
        "error": err[:ERR_CHARS],
    }
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        rotate()
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass                         


if __name__ == "__main__":
    try:
        main()
    except Exception:                
        pass
    
    print(json.dumps({"suppressOutput": True}))
    sys.exit(0)
