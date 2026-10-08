#!/usr/bin/env python3
'store-doc.py [--project <name>] <doc-path> -- print one store doc (global, or the named\nproject\'s), fetched via the get_doc MCP tool.\n\nFor a slash command\'s shell block, which cannot call the get_doc MCP tool directly and, on a\nmachine with no shared-docs/ tree, has no file to cat:\n\n    PROMPT_BODY=$(python3 "$HOME/.agent-context/global/scripts/store-doc.py" ralph-cleanup-universal.md)\n\npolicy (one pathway): the daemon is reached only through store_mcp.call, which does the MCP\nhandshake the way the relay reaches it (AGENT_CONTEXT_HOST/PORT/TOKEN from the environment,\ngaps filled from ~/.config/agent-context/env). The body is the doc with its store frontmatter\nalready stripped by get_doc, printed as UTF-8.\n\nExit 0 with the body on stdout; 1 with one `store-doc: <reason>` line on stderr and nothing on\nstdout (never a response body, never the token); 2 on a usage error. Writes no file.'
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_mcp


def fetch(path, project=None):
    args = {"path": path}
    if project:
        args["project"] = project
    try:
        result = store_mcp.call("get_doc", args)
    except (store_mcp.StoreUnreachable, store_mcp.ToolError) as exc:
        raise store_mcp.StoreUnreachable(str(exc)) from None
    if not isinstance(result, dict) or result.get("body") is None:
        raise store_mcp.StoreUnreachable("no such doc")
    body = result["body"]
    if not body.strip():
        raise store_mcp.StoreUnreachable("the doc %s is empty" % path)
    return body


def main(argv):
    project = None
    if len(argv) == 3 and argv[0] == "--project" and argv[1]:
        project, argv = argv[1], argv[2:]
    if len(argv) != 1 or not argv[0]:
        print("usage: store-doc.py [--project <name>] <doc-path>", file=sys.stderr)
        return 2
    try:
        text = fetch(argv[0], project)
    except (store_mcp.StoreUnreachable, store_mcp.ToolError) as exc:
        print("store-doc: %s" % exc, file=sys.stderr)
        return 1
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        return 1  
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
