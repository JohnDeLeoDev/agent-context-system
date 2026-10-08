#!/usr/bin/env python3
"MCP-only manifest access: read, upsert NAME, remove NAME.\nupsert reads one server specification as JSON from stdin; --dry-run validates only.\nCredentials belong in each harness's runtime authentication, never this manifest."
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_task

def validate(name, spec):
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in name):
        raise ValueError("invalid server name")
    if not isinstance(spec, dict):
        raise ValueError("server specification must be an object")
    fields = {"transport", "command", "args", "url", "harnesses", "projects", "env"}
    if any(k not in fields and k != "$comment" and not k.startswith("$comment-") for k in spec):
        raise ValueError("unknown specification field")
    if spec.get("transport") == "http" and any(k in spec for k in ("command", "args", "env")):
        raise ValueError("http cannot carry stdio fields")
    if spec.get("transport") == "stdio" and "url" in spec:
        raise ValueError("stdio cannot carry url")
    harnesses = spec.get("harnesses")
    allowed = {"claude", "claude-desktop", "opencode", "copilot", "antigravity", "xcode", "codex"}
    if not isinstance(harnesses, list) or not harnesses or any(not isinstance(h, str) or h not in allowed for h in harnesses):
        raise ValueError("harnesses must list supported harness names")
    if name == "xcode" and harnesses != ["claude"]:
        raise ValueError("xcode bridge must stay on claude only")
    if spec.get("transport") == "stdio":
        if not isinstance(spec.get("command"), str) or not spec["command"].strip():
            raise ValueError("stdio requires command")
        if not isinstance(spec.get("args"), list) or any(not isinstance(v, str) for v in spec["args"]):
            raise ValueError("stdio requires string args")
    elif spec.get("transport") == "http":
        url = spec.get("url")
        if not isinstance(url, str):
            raise ValueError("http requires url")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("http requires a credential-free HTTP URL")
        if "antigravity" in harnesses:
            raise ValueError("antigravity does not support http")
    else:
        raise ValueError("transport must be stdio or http")
    if "projects" in spec:
        projects = spec["projects"]
        if not isinstance(projects, list) or not projects or any(not isinstance(p, str) or not p or Path(p).is_absolute() or ".." in Path(p).parts for p in projects):
            raise ValueError("projects must be nonempty HOME-relative paths")
    if "env" in spec and (not isinstance(spec["env"], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in spec["env"].items())):
        raise ValueError("env must contain strings")
    if "oauthCallbackPort" in spec:
        raise ValueError("oauthCallbackPort is forbidden")
    
    scanner_spec = importlib.util.spec_from_file_location("manifest_secret_scan", Path(__file__).with_name("config-secret-scan.py"))
    scanner = importlib.util.module_from_spec(scanner_spec)
    scanner_spec.loader.exec_module(scanner)
    if list(scanner.scan_text(json.dumps(spec))) or list(scanner.scan_json_values(spec)):
        raise ValueError("literal credentials are forbidden")
    if any(k in spec for k in ("headers", "token", "password", "apiKey")):
        raise ValueError("authentication belongs in the harness, not the manifest")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("read", "upsert", "remove"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if (args.operation == "read") != (args.name is None):
        parser.error("read takes no name; upsert and remove require a name")
    root = Path(os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context")))
    path = root / "global" / "mcp-servers.json"
    try:
        document = json.loads(path.read_text())
        if not isinstance(document, dict) or not isinstance(document.get("servers"), dict):
            raise ValueError("manifest must contain servers")
        if args.operation == "read":
            print(json.dumps(document, indent=2))
            return 0
        if args.operation == "upsert":
            spec = json.loads(sys.stdin.read())
            validate(args.name, spec)
            document["servers"][args.name] = spec
        else:
            if args.name == "agent-context":
                raise ValueError("agent-context cannot be removed")
            if args.name not in document["servers"]:
                raise ValueError("server not found")
            del document["servers"][args.name]
        if not args.dry_run:
            sys.path.insert(0, str(root / "server" / "src"))
            from agent_context.paths import write_atomic
            write_atomic(path, json.dumps(document, indent=2) + "\n")
        print(json.dumps({"operation": args.operation, "name": args.name, "dry_run": args.dry_run}))
        return 0
    except (OSError, ValueError) as exc:
        
        print("mcp-manifest: " + str(exc), file=sys.stderr)
        return 2

if __name__ == "__main__":
    store_task.main_or_forward("mcp-manifest", main, stdin=True)
