#!/usr/bin/env python3
'file-agent-request — file an inter-agent request without being an agent.\n\nWhy this lives on the machine rather than in the app that calls it: a request is a\nstore doc, and a store doc is only real once the store has written it. A markdown file\ndropped at an entity path without the store\'s `uuid:` frontmatter is not indexed at\nall — the loader records it as "unindexed" and every get_*/list_* stays blind to it.\nRather than teach a phone the store\'s identity scheme, the phone asks a machine, and\nthe machine uses the store\'s own upsert.\n\nTargets resolve by name: a machine\'s hostname or machine id files into that machine\'s\ninbox at global scope; anything else is taken as a project display name and files into\nthat project\'s inbox. The distinction matters because a machine agent boots with no\nproject scope and reads only its own bucket.\n\nExit status is 0 when the doc is written, 1 when the target cannot be resolved, 2 on a\nusage error, 3 when the store does not answer. The path it wrote is printed on stdout.'
import argparse
import datetime as dt
import json
import os
import platform
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_mcp  
import store_task  


class _Mcp:
    'The three server calls this script makes, over MCP to the daemon.'

    @staticmethod
    def list_machines():
        return json.dumps(store_mcp.call("list_machines"))

    @staticmethod
    def list_entities(**kw):
        return json.dumps(store_mcp.call("list_entities", kw))

    @staticmethod
    def upsert_doc(**kw):
        return store_mcp.call("upsert_doc", {k: v for k, v in kw.items() if v is not None})


if store_task.targets_live_store():
    server = _Mcp()
else:
    
    
    
    
    
    VENV_PYTHON = os.path.expanduser("~/.agent-context/server/.venv/bin/python")
    REEXEC_FLAG = "FILE_AGENT_REQUEST_REEXEC"
    try:
        from agent_context import server
    except ModuleNotFoundError:
        if os.environ.get(REEXEC_FLAG) or not os.path.exists(VENV_PYTHON):
            raise
        os.environ[REEXEC_FLAG] = "1"
        os.execv(VENV_PYTHON, [VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    
    
    return slug[:60] or "request"


def resolve_target(name: str) -> tuple[str | None, str]:
    '(project, directory) for the target, or exits when the name means nothing.\n\n    Machines win over projects on a name clash: a machine inbox that silently became a\n    project inbox would be read by nobody.'
    machines = json.loads(server.list_machines())
    for machine in machines:
        if name.lower() in (machine["hostname"].lower(), machine["machine_id"].lower()):
            
            return None, f"inbox/machines/{machine.get('machine_id') or machine['hostname']}"

    projects = json.loads(server.list_entities(kind="project", limit=0))["items"]
    for project in projects:
        if name.lower() == project["display_name"].lower():
            return project["display_name"], "inbox"

    known_machines = ", ".join(sorted(m["hostname"] for m in machines))
    known_projects = ", ".join(sorted(p["display_name"] for p in projects))
    sys.exit(
        f"file-agent-request: no machine or project called {name!r}.\n"
        f"  machines: {known_machines}\n  projects: {known_projects}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="file-agent-request")
    parser.add_argument("--to", required=True, help="target machine hostname/id or project display name")
    parser.add_argument("--title", required=True)
    parser.add_argument("--priority", default="normal", choices=["low", "normal", "high"])
    parser.add_argument("--from", dest="sender", default=None,
                        help="who is asking; defaults to this machine's hostname")
    parser.add_argument("--body", default=None, help="request text; read from stdin when omitted")
    parser.add_argument("--dry-run", action="store_true",
                        help="print where it would go and what it would say, and write nothing")
    args = parser.parse_args()

    body_text = args.body if args.body is not None else sys.stdin.read()
    body_text = body_text.strip()
    if not body_text:
        print("file-agent-request: the request body is empty", file=sys.stderr)
        return 2

    project, directory = resolve_target(args.to)
    today = dt.date.today().isoformat()
    path = f"{directory}/{today}-{slugify(args.title)}.md"
    sender = args.sender or platform.node()

    
    
    doc = f"""---
from: {sender}
to: {args.to}
status: open
priority: {args.priority}
created: {today}
updated: {today}
---

# {args.title}

## Request

{body_text}
"""
    where = f"project {project}" if project else "global"
    if args.dry_run:
        print(f"would write {path} ({where})\n")
        print(doc)
        return 0

    server.upsert_doc(path=path, body=doc, project=project, title=args.title)
    print(f"{path} ({where})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (store_mcp.StoreUnreachable, store_mcp.ToolError) as exc:
        print("file-agent-request: the store did not take the request (%s)" % exc,
              file=sys.stderr)
        sys.exit(3)
