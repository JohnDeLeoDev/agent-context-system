#!/usr/bin/env python3

'require-store-bootstrap — refuse to work a turn on a session that never loaded the store.\n\nThe hole this closes. CLAUDE.md tells the agent to call get_session_context on its\nfirst turn. Nothing checks that it did. When the agent-context MCP server fails to\nlaunch -- a moved venv, an unsynced store, a dependency bump without `uv sync`, a\nmachine where setup never ran -- the tool is not in the roster, the\ninstruction has nothing to call, and the session proceeds with:\n\n    no global instructions, no memory, no audit queue, and no guardrails.\n\nIt still answers questions fluently. From the outside it looks like a correctly\nbootstrapped session. Everything else in the fleet\'s safety story -- the worktree\nmandate, the deploy gate, the git-write limits -- is carried by instructions that\nnever arrived.\n\nWhy the enforcement is on PreToolUse. A block on UserPromptSubmit deadlocks: a\nblocked UserPromptSubmit never starts a model turn. The reason string is\nprinted to the user and is not added to the model\'s context, so the\ninstruction "call get_session_context now" is delivered to a model that was never\ninvoked. The next prompt hits the identical state and blocks again. After a\n/compact, where compact-invalidates-bootstrap.py expires the stamp, every prompt\nwould block and no turn would ever start.\n\nSo the two events do different jobs:\n\n  * UserPromptSubmit -> nudge only (additionalContext). It reaches the model, it\n    lets the turn start, and it is the only channel here the model can act on.\n    It never blocks. Adding a block here re-creates the deadlock.\n  * PreToolUse(*) -> deny, and this is the gate. It runs inside a started\n    turn, so a denial is feedback the model reads and can respond to in the same\n    turn, by making the one call that clears it. No tool, hence no edit, deploy or\n    git write, can happen before the store loads, and the gate stays satisfiable.\n\nTwo exemptions, both required:\n  * get_session_context itself -- otherwise the only exit is also barred.\n  * ToolSearch -- on a harness with deferred tool schemas, get_session_context is\n    not callable until its schema has been fetched. Denying ToolSearch would be the\n    same deadlock one step removed.\n\nThe denial is self-limiting. After MAX_DENIALS refusals in one session the gate\ngives up and allows. A subagent inherits the parent\'s session_id but not\nnecessarily the agent-context tool grant, so a post-compaction subagent could\notherwise be stuck refusing every tool with no way to satisfy the gate. A guard\nthat can brick work is a guard that gets deleted; one that costs a few wasted calls\nand then yields still catches the case it was built for.\n\nFail-open on a transcript it cannot read, unlike block-deploy.py.\nblock-deploy fails closed because it gates one irreversible action, so a false block\ncosts a retry. This gate stands in front of every tool call; a parse error failing\nclosed would lock the user out of their own session. It warns.\n\nInput  (stdin JSON): { cwd, session_id, transcript_path, hook_event_name, tool_name, prompt }\nOutput (stdout JSON): {} | { hookSpecificOutput: … }'
import json
import os
import sys
TYPE_CHECKING = False  
if TYPE_CHECKING:
    from typing import NoReturn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

HOME = hp.home()
STATE = os.path.join(hp.state_dir(), "health")
STAMPS = os.path.join(STATE, "bootstrap")
TOOL = "get_session_context"




EXEMPT = (TOOL, "ToolSearch")




MAX_DENIALS = 5

DENY_REASON = (
    "Refused: this session has not loaded the agent-context store, so it has no "
    "global instructions, memory or guardrails.\n"
    "Call mcp__agent-context__get_session_context with the current working "
    "directory (ToolSearch for it first where tool schemas are deferred), follow "
    "what it returns, then retry this call.\n"
    "Every other tool is refused until then.\n"
    "If ToolSearch finds no get_session_context, the agent-context server started "
    "without tools (ls did not answer at session start) and this session "
    "cannot recover: tell the user, or your orchestrator, to relaunch it."
)

NUDGE = (
    "This session has not called mcp__agent-context__get_session_context, so it "
    "has no global instructions, no memory and no guardrails. Call it with the "
    "current working directory before anything else and follow what it returns; "
    "until you do, every other tool call will be refused."
)

UNGUARDED = (
    "Unguarded session: the agent-context MCP server is not available on this "
    "machine, so this session has no global instructions, memory or guardrails.\n"
    "Say so in your reply and take no irreversible action (git writes, deploys, "
    "edits outside a worktree).\n"
    "Help the user repair the server before dependent work: `uv sync --directory "
    "~/.agent-context/server` for a missing or stale venv, or "
    "`~/.agent-context/setup.sh` if the store was never set up here."
)


def emit(obj) -> "NoReturn":
    print(json.dumps(obj))
    sys.exit(0)


def context(event, text):
    emit({"hookSpecificOutput": {"hookEventName": event,
                                 "additionalContext": text}})


def deny(text):
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                 "permissionDecision": "deny",
                                 "permissionDecisionReason": text}})


def bootstrapped(path, after=0):
    "Did this session call get_session_context?\n\n    Scans for a tool_use block naming the tool. Returns None when the transcript\n    cannot be read at all, which the caller treats as 'unknown', not 'no'.\n\n    `after` is a line offset: lines at or before it are skipped. It is how a\n    compacted session stops counting its own pre-compaction bootstrap. Loading the\n    store puts instructions in the model's context, and compaction takes them back\n    out -- so a call made before the summary is no longer evidence that this turn\n    has them. Zero (the default) scans everything, which is right for a session\n    that has never been compacted."
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            for lineno, line in enumerate(fh, start=1):
                if lineno <= after:
                    continue
                
                
                
                if TOOL not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                msg = entry.get("message") if isinstance(entry, dict) else None
                blocks = (msg or {}).get("content") if isinstance(msg, dict) else None
                if not isinstance(blocks, list):
                    continue
                for b in blocks:
                    if (isinstance(b, dict) and b.get("type") == "tool_use"
                            and TOOL in (b.get("name") or "")):
                        return True
    except OSError:
        return None
    return False


def server_unavailable():
    "Is agent-context known-missing per the last MCP roster probe?\n\n    Absence of a probe verdict is not treated as unavailable: on a healthy machine\n    that has never probed, the tool is almost certainly there, and guessing\n    'unavailable' would downgrade a hard gate into a soft banner for no reason."
    try:
        with open(os.path.join(STATE, "mcp.json")) as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return False
    names = [e.get("name") for e in (report.get("missing") or [])]
    names += [e.get("name") for e in (report.get("degraded") or [])]
    return "agent-context" in names


def stamp_now(stamp):
    if not stamp:
        return
    try:
        os.makedirs(STAMPS, exist_ok=True)
        open(stamp, "w").close()
    except OSError:
        pass


def denials(stamp, bump=False):
    "Refusals issued to this session, optionally counting one more.\n\n    Kept next to the stamp so compact-invalidates-bootstrap's per-session cleanup\n    and this counter share a lifetime, and so concurrent sessions never share one."
    if not stamp:
        return 0
    path = stamp + ".denied"
    try:
        with open(path) as fh:
            n = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        n = 0
    if bump:
        try:
            os.makedirs(STAMPS, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(str(n + 1))
        except OSError:
            pass
        n += 1
    return n


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        emit({})

    event = payload.get("hook_event_name") or "UserPromptSubmit"
    pre_tool = event == "PreToolUse"
    tool = payload.get("tool_name") or ""

    
    
    
    
    if pre_tool and any(name in tool for name in EXEMPT):
        if TOOL in tool:
            stamp_now(os.path.join(STAMPS, payload.get("session_id") or "") if payload.get("session_id") else None)
        emit({})

    session = payload.get("session_id") or ""
    stamp = os.path.join(STAMPS, session) if session else None

    
    
    if stamp and os.path.exists(stamp):
        emit({})

    
    
    
    after = 0
    if stamp:
        try:
            with open(stamp + ".compacted") as fh:
                after = int(fh.read().strip() or 0)
        except (OSError, ValueError):
            after = 0

    state = bootstrapped(payload.get("transcript_path"), after)

    if state is True:
        stamp_now(stamp)
        emit({})

    if state is None:
        
        
        
        
        
        tpath = payload.get("transcript_path")
        missing = not tpath or not os.path.exists(tpath)
        if pre_tool:
            emit({})
        if missing:
            context(event,
                    "First prompt of this session (no transcript on disk yet), so this "
                    "gate cannot confirm the store was loaded. "
                    "Call mcp__agent-context__get_session_context before anything else.")
        else:
            context(event,
                    "This gate could not read the session transcript (%s), so it cannot "
                    "confirm the agent-context store was loaded. Check the file's "
                    "permissions and encoding. If you have not called "
                    "mcp__agent-context__get_session_context this session, call it "
                    "before anything else." % tpath)

    if server_unavailable():
        
        if pre_tool:
            emit({})
        context(event, UNGUARDED)

    if not pre_tool:
        context(event, NUDGE)

    if denials(stamp, bump=True) > MAX_DENIALS:
        
        
        emit({})

    deny(DENY_REASON)


def crash_report(exc):
    "A crash in this hook must not be silent, and here it fails open.\n\n    Same handler shape as preflight-core-health's; invariant-check's\n    `python-hook-crash-handler` requires it.\n\n    If this hook crashed with no handler, the gate would not deny: the harness\n    swallows a non-zero exit, the tool call proceeds, and the session runs its whole\n    life with no instructions, no memory and no guardrails, which is the state\n    this file exists to prevent.\n\n    Every internal `try` in this file is narrow (OSError/ValueError), because\n    those are the expected, recoverable failures. This catches the unexpected ones --\n    the TypeError against a harness payload that changed shape, the AttributeError on a\n    None nobody predicted -- and converts a silent fail-open into a loud one. It emits\n    through additionalContext, the one channel that reaches the model, and exits 0:\n    a non-zero exit buys nothing here (the harness discards it) and risks\n    the report being discarded with it. The report is the product."
    import traceback
    tb = traceback.format_exc().strip().splitlines()
    last = tb[-1].strip() if tb else repr(exc)
    where = ""
    for line in reversed(tb):
        if line.strip().startswith("File "):
            where = line.strip()
            break
    detail = ("%s  [%s]" % (last, where)) if where else last
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                "require-store-bootstrap crashed and did not gate this tool call: %s\n"
                "If you have not called get_session_context with the working directory "
                "this session, call it now: without it there are no global "
                "instructions, memory or guardrails.\n"
                "Likely cause: a stale projection (a hook upsert reaches ~/.claude only "
                "in the next session). Run:\n"
                "  python3 ~/.agent-context/global/scripts/home-materialize.py" % detail),
        },
    }))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise                                 
    except Exception as exc:                  
        crash_report(exc)
        sys.exit(0)
