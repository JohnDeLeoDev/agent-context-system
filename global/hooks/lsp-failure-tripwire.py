#!/usr/bin/env python3

'PostToolUse + PostToolUseFailure(<lang>-lsp | LSP): arm/disarm the "this session has no\ncode intelligence" tripwire that require-working-lsp enforces. Only a failure arms on a\ndead-server signature; only a success clears.\n\nWhy this exists. A dead language server fails open: every call errors, the agent\nfalls back to grep, and grep still returns hits, so the session looks productive\nwhile the answers are wrong (reflection, DI, serialization and framework loading\nall hide callers from grep, and a negative grep looks the same as a negative LSP\nanswer).\n\nThe written rule (memory `never-proceed-without-lsp`) and the SessionStart DEGRADED\nbanner do not cover this alone:\n  - a memory reaches only sessions that loaded it, and\n  - the SessionStart banner is a snapshot of one probe at t=0. It says nothing about a\n    server that dies later, and nothing re-checks.\nThis hook watches every LSP call for the whole session.\n\nThis hook is no blocker itself: it only records what it saw. The block lives\nin require-working-lsp so that a failing LSP call is never itself denied -- the agent\nmust stay free to call the server again to check whether it has recovered.'
import glob as _glob
import json
import os
import re
import subprocess as _sub
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp




VERDICT_TTL = 900.0



KICK_DEBOUNCE = 600.0



DEAD = re.compile(
    r'timed out after'                       
    r'|CONNECTION_CLOSED'                    
    r'|broken pipe'                          
    r'|failed to write header'               
    r'|no stub serializer'                   
    r'|request failed: canceled'            
    r'|\(code: -32800\)'                     
    r'|\(code: -32803\)'                     
    r'|MCP server .* not (?:found|available|connected)'
    r'|No such tool'
    
    
    
    r'|language server is down'
    
    
    
    r'|no compiler flags',
    re.I)












MISS = re.compile(r'not found|no references found|no (?:results|matches)', re.I)


BY_NAME_RE = re.compile(r'(?:definition|references|implementation)$', re.I)


def canary_says(d, tool, sid, state_dir):
    "True healthy, False half-dead, None don't know (a refresh was kicked off).\n\n    Non-blocking. The canary costs tens of seconds, so this\n    reads the verdict already on disk and, when there is none fresh enough, spawns a\n    refresh detached and declines to judge. The failure is persistent -- every\n    symbol-name query misses -- so the next miss reads the verdict and arms. Trading\n    one query's delay for never stalling a PostToolUse hook is the right way round."
    cwd = os.path.realpath(str(d.get("cwd") or os.getcwd()))
    
    
    
    
    home = os.path.realpath(os.path.expanduser("~"))
    server = tool.split("__")[1] if "__" in tool else tool
    server = re.sub(r'-lsp$|^mcp_+', '', server) or server

    
    
    try:
        with open(os.path.join(home, ".agent-context", "global",
                               "lsp-canaries.json")) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    root = None
    for rel in (cfg.get("canaries") or {}):
        cand = os.path.realpath(os.path.join(home, rel))
        if cwd == cand or cwd.startswith(cand + os.sep):
            root = cand
            break
    if root is None:
        return None

    state = os.path.join(hp.state_dir(home), "health", "lsp")
    fresh, now = None, time.time()
    for path in _glob.glob(os.path.join(state, "*.json")):
        try:
            with open(path) as fh:
                v = json.load(fh)
        except (OSError, ValueError):
            continue
        vcwd = os.path.realpath(str(v.get("cwd") or ""))
        if not (vcwd == root or vcwd.startswith(root + os.sep)):
            continue
        
        
        symbols = {e.get("symbol") for e in (cfg["canaries"].get(
            os.path.relpath(root, home)) or [])}
        if v.get("symbol") not in symbols:
            continue
        if now - float(v.get("ts") or 0) <= VERDICT_TTL:
            fresh = v if fresh is None or v["ts"] > fresh["ts"] else fresh
    if fresh is not None:
        return bool(fresh.get("ok"))

    
    
    kick = os.path.join(state_dir, "%s.kick-%s" % (sid, server))
    try:
        if os.path.isfile(kick) and now - os.path.getmtime(kick) < KICK_DEBOUNCE:
            return None
        open(kick, "w").close()
        _sub.Popen([sys.executable,
                    os.path.join(hp.scripts_dir(home), "lsp-canary.py"), root],
                   stdout=_sub.DEVNULL, stderr=_sub.DEVNULL, stdin=_sub.DEVNULL,
                   start_new_session=True)
    except (OSError, ValueError):
        pass
    return None


def evaluate(d, state_dir):
    '(up_kind, kind, detail, sid, tool) for this call. up_kind is "name"/"position"\n    when this is a recovery signal; kind/detail are set when it should arm.'
    tool = str(d.get("tool_name") or "")
    if not (re.search(r'-lsp__', tool) or tool == "LSP"):
        return None, None, None, None, tool

    sid = str(d.get("session_id") or "nosession")
    sid = re.sub(r'[^A-Za-z0-9_.-]', '_', sid)[:128] or "nosession"

    
    
    
    failed = (d.get("hook_event_name") == "PostToolUseFailure"
              or ("error" in d and "tool_response" not in d))
    resp = d.get("tool_response")
    if failed:
        blob = str(d.get("error") or "")
    elif isinstance(resp, (dict, list)):
        blob = json.dumps(resp)
    else:
        blob = str(resp or "")

    
    
    
    errflag = False
    if isinstance(resp, dict):
        errflag = bool(resp.get("is_error") or resp.get("isError") or resp.get("error"))

    by_name = BY_NAME_RE.search(tool)
    ok_kind = "name" if by_name else "position"

    up_kind = kind = detail = None
    if failed:
        if DEAD.search(blob):
            kind = "dead"
            detail = " ".join(blob.split())[:200]
        
        
    elif errflag:
        
        
        
        pass
    elif by_name and MISS.search(blob):
        healthy = canary_says(d, tool, sid, state_dir)
        if healthy is False:
            kind = "name"
            detail = ("the canary symbol, known to exist here, also answers 'not "
                       "found': the bridge resolves positions but not names")
        elif healthy is True:
            up_kind = ok_kind
        
    else:
        up_kind = ok_kind

    return up_kind, kind, detail, sid, tool


def main() -> int:
    home = os.environ.get("HOME") or os.path.expanduser("~")
    state_dir = os.path.join(hp.state_dir(home), "lsp-down")
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        return 0

    raw = sys.stdin.read()
    try:
        d = json.loads(raw)
    except Exception:
        return 0  
    if not isinstance(d, dict):
        return 0

    up_kind, kind, detail, sid, tool = evaluate(d, state_dir)
    if sid is None:
        return 0  

    flag = os.path.join(state_dir, sid)

    if up_kind is not None:
        
        
        
        
        
        
        
        if os.path.isfile(flag):
            armed_kind = "dead"
            try:
                with open(flag) as fh:
                    for line in fh:
                        k, _, v = line.strip().partition("=")
                        if k == "kind":
                            armed_kind = v
            except OSError:
                pass
            if armed_kind == "name" and up_kind != "name":
                sys.stderr.write(
                    "lsp-failure-tripwire: %s answered, but the fault is on the "
                    "symbol-name path, which a position request does not test. The "
                    "gate stays armed until a definition or references call "
                    "resolves.\n" % tool
                )
                return 0
            try:
                os.remove(flag)
            except OSError:
                pass
            sys.stderr.write(
                "lsp-failure-tripwire: %s answered; code intelligence is back, "
                "gate cleared.\n" % tool
            )
        return 0

    if detail is None:
        return 0

    
    
    count = 1
    if os.path.isfile(flag):
        try:
            with open(flag) as fh:
                lines = fh.readlines()
        except OSError:
            lines = []
        out_lines = []
        found = False
        for line in lines:
            if line.startswith("count="):
                try:
                    count = int(line.strip().partition("=")[2]) + 1
                except ValueError:
                    count = 2
                out_lines.append("count=%d\n" % count)
                found = True
            else:
                out_lines.append(line)
        if not found:
            count = 2
            out_lines.insert(0, "count=%d\n" % count)
        try:
            with open(flag, "w") as fh:
                fh.writelines(out_lines)
        except OSError:
            pass
    else:
        try:
            with open(flag, "w") as fh:
                fh.write("count=1\n")
                fh.write("kind=%s\n" % (kind or "dead"))
                fh.write("tool=%s\n" % tool)
                fh.write("at=%s\n" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                fh.write("detail=%s\n" % detail)
        except OSError:
            pass

    sys.stderr.write(
        "lsp-failure-tripwire: the language server is down (failure #%d this "
        "session; %s: %s).\n"
        "Stop code work and fix the server first; grep is no substitute. Code edits "
        "and subagent spawns are refused until an LSP call answers.\n"
        "  1. ps -Ao pid,ppid,command | grep -iE 'lspd|lsp' | grep -v grep "
        "(is lspd alive with a live language-server child?)\n"
        "  2. lspd.py --restart --key <server> --workspace <main checkout>, then "
        "retry the LSP call. A success clears the gate.\n"
        "  3. If the server is healthy but this session's MCP client gave up, ask "
        "the user to restart the session.\n"
        "See get_doc(\"lsp-daemon-operations.md\"); memory never-proceed-without-lsp.\n"
        % (count, tool, detail)
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("lsp-failure-tripwire crashed and failed open: %r\n" % (exc,))
        sys.exit(0)
