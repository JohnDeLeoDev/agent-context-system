#!/usr/bin/env python3
"store-prose-tools -- which MCP tools carry written text into the store.\n\nWhy this exists. A store write is committed and pushed to four remotes within\nseconds, so every guard that reads a body -- the credential block, the plain\nlanguage check -- has to cover every tool that can carry one. A hand-typed list\nof tool names is short as soon as the server gains a write tool, and widening it\nby hand repeats the act that produced the gap. The list has to be derived from\nthe thing it is a list of. This reads the server's own tool signatures and\nreports what is there.\n\nThe anti-rot property. Every string parameter on every tool is classified as\ntext or not-text. A parameter this file has never seen makes it exit 2 and name\nthe parameter, rather than quietly leaving it out of the set. That is the whole\npoint: a new field on the server is a decision someone has to make, not a hole\nthat opens by itself.\n\nUsage:\n  store-prose-tools --matcher          the `|`-joined hook matcher string\n  store-prose-tools --fields           tool -> field names, one per line\n  store-prose-tools --json             the whole map\n\nIt has no --check. The checking lives in invariant-check.py, so there is one\nplace that decides whether a guard is short, not two that can disagree.\n\nObservations guarded: #317, #318."
import ast
import json
import os
import sys

STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context"))
SERVER = os.path.join(STORE, "server", "src", "agent_context", "server.py")
PREFIX = "mcp__agent-context__"


TEXT_FIELDS = {
    "body", "script_body", "description", "title", "text", "content",
    "observation", "evidence", "note", "resolution_note", "new_string",
    "metadata",
}







NOT_TEXT_FIELDS = {
    "allowed_tools", "body_path", "canonical_remote", "cwd", "display_name",
    "effort", "entity_type", "event_type", "from_machine", "group_by",
    "integration_branch", "key", "kind", "language", "load_behavior",
    "local_path", "machine", "matcher", "memory_type", "model", "name",
    "observed_date", "old_string", "origin", "path", "path_prefix",
    "permission_mode", "project", "query", "scope", "severity", "slug",
    "stack", "status", "tools", "workspace",
    
    
    
    "action", "links", "rel", "section",
    
    
    
    "entity_id", "name_prefix",
}



STRUCTURAL = {"bulk_edit": ["edits[].replacements[][1]"]}


def _tools(path):
    '(name, [string params]) for every @mcp.tool in the server.'
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError) as exc:
        print("store-prose-tools: cannot read the server at %s: %s" % (path, exc),
              file=sys.stderr)
        raise SystemExit(3)
    
    
    aliases = {n.targets[0].id: ast.unparse(n.value) for n in tree.body
               if isinstance(n, ast.Assign) and len(n.targets) == 1
               and isinstance(n.targets[0], ast.Name)}
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any("mcp.tool" in ast.unparse(d) for d in node.decorator_list):
            continue
        params = []
        for arg in list(node.args.args) + list(node.args.kwonlyargs):
            ann = ast.unparse(arg.annotation) if arg.annotation else ""
            ann = aliases.get(ann, ann)
            if "str" in ann:
                params.append(arg.arg)
        out.append((node.name, params))
    return out


def build():
    '{tool name without prefix: [text fields]}, or exit 2 on a new field.'
    carriers, unknown = {}, {}
    for name, params in _tools(SERVER):
        fields = []
        for p in params:
            if p in TEXT_FIELDS:
                fields.append(p)
            elif p not in NOT_TEXT_FIELDS:
                unknown.setdefault(p, []).append(name)
        if fields:
            carriers[name] = fields
    for name, fields in STRUCTURAL.items():
        carriers.setdefault(name, fields)
    if unknown:
        print("store-prose-tools: the server has string parameters this file has "
              "never classified, so the answer would be short and nobody would "
              "know. Add each to TEXT_FIELDS or NOT_TEXT_FIELDS:", file=sys.stderr)
        for p, tools in sorted(unknown.items()):
            print("  %-24s on %s" % (p, ", ".join(sorted(tools))), file=sys.stderr)
        raise SystemExit(2)
    if not carriers:
        print("store-prose-tools: found no text-carrying tools at all, which "
              "means the server was not parsed, not that the store stopped "
              "taking writes.", file=sys.stderr)
        raise SystemExit(3)
    return carriers


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--fields"
    carriers = build()
    if mode == "--matcher":
        print("|".join(PREFIX + t for t in sorted(carriers)))
    elif mode == "--json":
        print(json.dumps(carriers, indent=2, sort_keys=True))
    elif mode == "--fields":
        for tool in sorted(carriers):
            print("%-28s %s" % (tool, " ".join(carriers[tool])))
    else:
        print((__doc__ or "").strip().split("Usage:")[1].strip(), file=sys.stderr)
        raise SystemExit(64)


if __name__ == "__main__":
    main()
