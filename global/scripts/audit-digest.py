#!/usr/bin/env python3
'audit-digest.py: the "needs a human" digest of open audit observations.\n\nThe digest is not a stored doc. It is\nrendered at read time by the server (`fstools.audit_digest`): `get_session_context`\nlists it in the machine\'s inbox whenever anything is open or triaged, and\n`get_doc("inbox/machines/<to>/audit-digest.md")` returns the live rendering. A digest\nbuilt from the observation files on every read can never disagree with\n`list_audit_observations`.\n\nEvery machine is relay-only, checkout or not. This script always fetches the live\ndigest through `store-doc.py`\'s `fetch()`, the same get_doc MCP call a slash command\'s\nshell block uses, so there is one path and it runs every time.\n\nWhat this script does:\n  * default / --dry-run  print the digest as a session would read it\n  * --json               not available over the network; use list_audit_observations\n                         (the MCP tool) instead, which returns the same rows structured\n\nThis script does no snapshot-doc cleanup. If a stale snapshot doc\nresurfaces, delete it with the delete_entity MCP tool from\nan agent session, not from here.\n\nUsage:\n  audit-digest.py [--to laptop] [--dry-run] [--json]\n\nObservations guarded: #179, #444.'
import argparse
import os
import subprocess
import sys



STORE_DOC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store-doc.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="laptop", help="fleet machine_id (policy)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.json:
        print("audit-digest: --json is not available over the network; call the "
              "list_audit_observations MCP tool instead for the same rows, structured.",
              file=sys.stderr)
        return 2
    path = f"inbox/machines/{a.to}/audit-digest.md"
    proc = subprocess.run([sys.executable, STORE_DOC, path], capture_output=True)
    if proc.returncode != 0:
        sys.stderr.buffer.write(proc.stderr)
        return 1
    sys.stdout.buffer.write(proc.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
