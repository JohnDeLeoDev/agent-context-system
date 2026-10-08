#!/usr/bin/env python3
"""Start the portable context store."""
import os
import sys
from pathlib import Path

os.environ.setdefault("AGENT_CONTEXT_STORE", str(Path(__file__).resolve().parent))
os.environ.setdefault("AGENT_CONTEXT_NO_SYNC", "1")
os.environ.setdefault("AGENT_CONTEXT_SELF_DEPLOY", "0")
os.environ.setdefault("AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS", "0")
if "--http" in sys.argv:
    if not os.environ.get("AGENT_CONTEXT_TOKEN"):
        raise SystemExit("Set AGENT_CONTEXT_TOKEN before starting HTTP transport.")
    os.environ["AGENT_CONTEXT_TRANSPORT"] = "http"
elif "--shared" not in sys.argv:
    os.environ["AGENT_CONTEXT_NO_DAEMON"] = "1"
    os.environ.pop("AGENT_CONTEXT_TRANSPORT", None)

sys.path.insert(0, str(Path(__file__).resolve().parent / "server/src"))
from agent_context.relay_env import load_relay_env
load_relay_env(os.environ)
from agent_context.server import main
main()
