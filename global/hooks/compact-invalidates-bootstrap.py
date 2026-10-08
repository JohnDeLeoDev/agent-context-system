#!/usr/bin/env python3
"SessionStart(compact): make the store bootstrap COUNT AGAIN after a compaction.\n\nTHE HOLE THIS CLOSES. require-store-bootstrap.py refuses to work a turn on a\nsession that never called get_session_context. It decides that two ways, and BOTH\nof them survive compaction: a per-session stamp file under\n~/.local/state/agent-context/health/bootstrap/<session_id>, and a scan of the whole transcript\nfor a tool_use naming the tool. A session that bootstrapped once at 09:00 therefore\nsatisfies the gate at 17:00, three compactions later -- by which point the\ninstructions it loaded are no longer in the model's context at all.\n\nWHAT THIS DOES. Deletes the stamp so the fast path cannot short-circuit, and\nrecords the transcript's line count AT THE MOMENT OF COMPACTION so the gate can\ntell a fresh call from the stale pre-compaction one. Both are per-session, so\nconcurrent sessions cannot invalidate each other."
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp



CAP_ENV = "AGENT_CONTEXT_PRECOMPACT_MAX_CHARS"
OFF_ENV = "AGENT_CONTEXT_PRECOMPACT_CONTEXT"
DEFAULT_CAP = 6000


def snapshot_cap():
    try:
        n = int(os.environ.get(CAP_ENV, ""))
    except ValueError:
        return DEFAULT_CAP
    return n if n > 0 else DEFAULT_CAP


def main():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    session = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or ""

    state = os.path.join(hp.state_dir(home), "health")
    stamps = os.path.join(state, "bootstrap")

    if session:
        try:
            os.remove(os.path.join(stamps, session))
        except OSError:
            pass
        
        
        
        
        try:
            os.remove(os.path.join(stamps, session + ".denied"))
        except OSError:
            pass
        
        
        
        
        shutil.rmtree(os.path.join(home, ".local", "state", "agent-context",
                                    "read-ledger", session), ignore_errors=True)
        os.makedirs(stamps, exist_ok=True)
        
        
        compacted_path = os.path.join(stamps, session + ".compacted")
        line_count = 0
        if transcript and os.path.isfile(transcript):
            try:
                with open(transcript, encoding="utf-8", errors="replace") as fh:
                    line_count = sum(1 for _ in fh)
            except OSError:
                line_count = 0
        try:
            with open(compacted_path, "w", encoding="utf-8") as fh:
                fh.write(str(line_count))
        except OSError:
            pass

    
    
    
    
    
    
    
    
    
    
    
    extra = ""
    cap_dir = os.environ.get("AGENT_CONTEXT_STATE_DIR") or os.path.join(
        home, ".local", "state", "agent-context")
    cap = os.path.join(cap_dir, "precompact", session + ".json") if session else ""
    snapshot_off = os.environ.get(OFF_ENV, "").strip().lower() == "off"
    if session and cap and os.path.isfile(cap) and not snapshot_off:
        try:
            with open(cap, encoding="utf-8") as fh:
                d = json.load(fh)
            out = []
            for label, key in (("Working tree", "work"), ("Store", "store")):
                w = d.get(key) or {}
                if not w.get("repo"):
                    continue
                head = "%s @ %s (%s)" % (w.get("repo"), w.get("branch") or "?",
                                          w.get("head") or "?")
                if w.get("worktree"):
                    head += "  [WORKTREE: %s]" % w["worktree"]
                out.append("%s: %s" % (label, head))
                files = w.get("modified") or []
                if files:
                    out.append("  %d uncommitted file(s) at compaction:"
                               % w.get("modified_count", len(files)))
                    out += ["    " + f for f in files[:20]]
                    if w.get("modified_count", 0) > 20:
                        out.append("    ... +%d more" % (w["modified_count"] - 20))
            
            
            edited = [str(f) for f in (d.get("edited_this_session") or [])]
            if edited:
                out.append("Edited this session (%d, oldest first):" % len(edited))
                out += ["    " + f for f in edited]
            written = [str(e) for e in (d.get("store_entities_written") or [])]
            if written:
                out.append("Store entities written (%d, oldest first):" % len(written))
                out += ["    " + e for e in written]
            asks = [str(a) for a in (d.get("recent_user_asks") or [])]
            if asks:
                out.append("Recent user asks (oldest first):")
                out += ["    - " + a for a in asks]
            extra = "\n".join(out)
        except Exception:
            extra = ""

    extra = extra.strip()
    limit = snapshot_cap()
    if len(extra) > limit:
        extra = (extra[:limit].rstrip()
                 + "\n[snapshot truncated at %d chars; raise %s or set %s=off]"
                 % (limit, CAP_ENV, OFF_ENV))
    msg = ("This session has just been COMPACTED: the always-loaded instructions and "
           "memory are no longer in context, only the summary is. Call "
           "mcp__agent-context__get_session_context with the current working directory "
           "BEFORE any other tool use. Until you do, the store-bootstrap gate will "
           "refuse every tool call except that one.")
    if extra:
        msg += ("\n\nState captured immediately before the compaction, which the "
                "summary is not guaranteed to have kept. Historical reference only: "
                "do not re-run any task listed here, and expect the repo to have "
                "changed since:\n" + extra)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                              "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
