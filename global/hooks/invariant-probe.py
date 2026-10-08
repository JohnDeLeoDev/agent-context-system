#!/usr/bin/env python3
"SessionStart: keep the invariant verdict fresh, without spending the session's budget.\n\nWHY A PROBE AND NOT AN INLINE RUN. invariant-check walks ~95 files across six rules.\nThat is cheap in absolute terms and still far too much to put on the critical path of\nevery single session start, for an answer that does not change minute to minute. Same\nshape as the MCP roster probe and the LSP canary: run detached, write a verdict, and\nlet the NEXT session read it. A half-applied invariant that has been open for weeks is\nnot less worth reporting an hour later.\n\nHOW IT SURFACES. `--health` writes ~/.local/state/agent-context/health/invariants.json through\nhealth-record.py. preflight-core-health's generic_findings() already picks up any\nverdict file with ok:false, so this needs no wiring there at all -- it inherits the\nDEGRADED block, the once-a-day push, and the staleness rules for free. Deliberately\nreusing that machinery rather than inventing a second reporting channel: a finding\nnobody reads is the failure this whole system keeps rediscovering.\n\nSilent here, always. The verdict is the output; this file just starts the work."
import os
import subprocess
import sys


def main():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    store = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(home, ".agent-context")
    scripts = os.path.join(store, "global", "scripts")

    
    
    
    
    if not os.path.isdir(os.path.join(store, "server")):
        health = os.path.join(home, ".local", "state", "agent-context", "health")
        for name in ("invariants.json", "observation-coverage.json"):
            try:
                os.unlink(os.path.join(health, name))
            except OSError:
                pass
        return 0

    
    
    
    
    
    
    
    for s in ("invariant-check", "observation-coverage"):
        script = os.path.join(scripts, s + ".py")
        if not os.path.isfile(script):
            continue
        
        
        try:
            subprocess.Popen(
                [sys.executable, script, "--health"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
