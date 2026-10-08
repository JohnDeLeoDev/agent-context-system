#!/usr/bin/env python3
"config-secret-scan: find literal credentials in the config files.\n\nScanned: settings.json, the MCP config (~/.claude.json, mcpServers only), and the\nstore's global and project files (hooks, scripts, docs, memories, sidecars). A finding\nnames the file, line and rule. It never prints a value (memory\nsecrets-redact-by-section-not-field-name).\n\nRules, chosen for near-zero false positives because a noisy check gets skimmed:\n  provider-token      a provider-prefixed token (memory-husk-guard's own regex, imported)\n  private-key         a PEM private key block\n  url-credential      user:pass@ or a token/api_key query parameter inside a URL\n  literal-credential  a credential-named key or KEY/TOKEN/SECRET/PASSWORD assignment\n                      holding a literal: not an op:// reference, $VAR, placeholder, hash,\n                      date, or an all-letters identifier. Known blind spot: a hex-only\n                      secret of 32 to 64 characters reads as a hash and is not flagged.\n\nALLOW exempts a file by path suffix and needs a reason, so an exemption is explained.\n\nUsage:  config-secret-scan.py [--json] [--paths P ...]     exit 1 when anything is found"
import importlib.util
import json
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.join(HOME, ".agent-context"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_paths as hp
import store_task  

OUT = os.path.join(hp.state_dir(HOME), "health", "config-scan.json")

ALLOW = {
    "hook-test-cases.py":
        "fake provider tokens are the deny-case fixtures for memory-husk-guard",
    "test-store-reader-graph-safety.py":
        "TEST_ONLY_KEY holds a store-relative file path used as a fixture, not a credential",
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "worktrees", "archive"}
TEXT_EXT = (".md", ".py", ".sh", ".json", ".toml", ".yaml", ".yml", ".txt", ".env", ".conf")
MAX_BYTES = 2_000_000


def _provider_re():
    'The provider-token regex lives once, in memory-husk-guard.'
    path = os.path.join(STORE, "global", "hooks", "memory-husk-guard.py")
    spec = importlib.util.spec_from_file_location("memory_husk_guard", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PROVIDER_TOKEN_RE


PROVIDER_RE = _provider_re()
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
URL_CRED_RE = re.compile(
    r"https?://[^\s/:@\"']+:[^\s/@\"']{3,}@"
    r"|[?&](?:api[_-]?key|access[_-]?token|token|secret)=[A-Za-z0-9._~+/-]{16,}",
    re.IGNORECASE)
NAME_RE = re.compile(
    r"(api[_-]?key|access[_-]?key|secret|token|password|passwd|credential|auth)",
    re.IGNORECASE)
HASH_RE = re.compile(
    r"^(?:[0-9a-f]{32,64}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})$", re.IGNORECASE)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
ASSIGN_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)\s*=\s*"
    r"[\"']?([A-Za-z0-9/+_.=-]{20,})")
PLACEHOLDER_RE = re.compile(
    r"^(op://|\$|\{\{|<|%|x{4,}|\*{3,}|your[_-]|example|changeme|placeholder|redacted"
    r"|test|dummy|fake|sample)", re.IGNORECASE)


def literal(value):
    'A value that is a real literal, not a reference or a placeholder.'
    v = str(value).strip().strip("\"'")
    if len(v) < 16 or PLACEHOLDER_RE.match(v) or HASH_RE.match(v) or DATE_RE.match(v):
        return False
    
    
    return bool(re.search(r"[A-Za-z]", v) and re.search(r"\d", v))


def scan_text(text):
    "Yield (line_no, rule) for one file's text."
    for n, line in enumerate(text.splitlines(), 1):
        if PROVIDER_RE.search(line):
            yield n, "provider-token"
        if PRIVATE_KEY_RE.search(line):
            yield n, "private-key"
        if URL_CRED_RE.search(line):
            yield n, "url-credential"
        m = ASSIGN_RE.search(line)
        if m and literal(m.group(2)):
            yield n, "literal-credential"


def scan_json_values(doc, path=""):
    'Walk a settings or MCP document: a credential-named key holding a literal.'
    if isinstance(doc, dict):
        for k, v in doc.items():
            here = "%s.%s" % (path, k)
            if str(k).lstrip("_$").lower().startswith("comment"):
                continue
            if isinstance(v, str) and NAME_RE.search(str(k)) and literal(v):
                yield here, "literal-credential"
            else:
                yield from scan_json_values(v, here)
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from scan_json_values(v, "%s[%d]" % (path, i))


def _mcp_only(doc):
    '~/.claude.json is mostly caches and telemetry ids. Only its MCP server entries,\n    global and per project, can carry a wired credential.'
    out = {"mcpServers": doc.get("mcpServers") or {}}
    for name, proj in (doc.get("projects") or {}).items():
        if isinstance(proj, dict) and proj.get("mcpServers"):
            out["projects:" + name] = proj["mcpServers"]
    return out


def store_files():
    for root in (os.path.join(STORE, d) for d in ("global", "projects")):
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.endswith(TEXT_EXT):
                    p = os.path.join(dirpath, f)
                    try:
                        if os.path.getsize(p) <= MAX_BYTES:
                            yield p
                    except OSError:
                        pass


def config_files():
    for p in (hp.settings_file(HOME), hp.claude_json(HOME),
              os.path.join(hp.claude_home(HOME), "settings.local.json")):
        if os.path.isfile(p):
            yield p


def _scan_targets(targets):
    'Return (findings, files_scanned). A finding is {file, line, rule}.'
    found, n = [], 0
    for p in targets:
        if any(p.endswith(suffix) for suffix in ALLOW):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        n += 1
        for line, rule in scan_text(text):
            found.append({"file": p, "line": line, "rule": rule})
        if p.endswith(".json"):
            try:
                doc = json.loads(text)
            except ValueError:
                continue
            if os.path.basename(p) == os.path.basename(hp.claude_json(HOME)):
                doc = _mcp_only(doc)
            for where, rule in scan_json_values(doc):
                found.append({"file": p, "line": where, "rule": rule})
    return found, n


def scan(paths=None):
    'Both halves: the harness config files and the store tree.'
    targets = list(paths) if paths else list(config_files()) + list(store_files())
    return _scan_targets(targets)


def scan_local(paths=None):
    'The harness half only: settings.json, the MCP config, settings.local.json.\n    These are never store content, so this half always runs on the machine that\n    started config-secret-scan, whether or not the store half forwards.'
    return _scan_targets(list(paths) if paths else list(config_files()))


def scan_store(paths=None):
    'The store-tree half only: global/ and projects/ under AGENT_CONTEXT_STORE.'
    return _scan_targets(list(paths) if paths else list(store_files()))


def write_verdict(found):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if not found:
        try:
            os.remove(OUT)
        except OSError:
            pass
        return
    failures = ["%s:%s %s (value not shown)" % (f["file"], f["line"], f["rule"])
                for f in found[:20]]
    if len(found) > 20:
        failures.append("... and %d more; run config-secret-scan.py" % (len(found) - 20))
    rec = {"ts": int(time.time()), "component": "config secret scan",
           "ok": False, "failures": failures}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh, indent=2)
    os.replace(tmp, OUT)


def _report(args, found, n):
    if "--json" in args:
        print(json.dumps({"scanned": n, "findings": found}, indent=1))
    else:
        for f in found:
            print("%s:%s %s" % (f["file"], f["line"], f["rule"]))
        print("%d file(s) scanned, %d finding(s)" % (n, len(found)))


def main():
    'Runs whole (paths pinned or already inside the store host): both halves here,\n    same process. This is the server-task entry point, and the fallback for a caller\n    that already targets a fixture or names its own --paths.'
    args = sys.argv[1:]
    paths = args[args.index("--paths") + 1:] if "--paths" in args else None
    store_only = "--store-only" in args
    found, n = (scan_store(paths) if store_only else scan(paths))
    _report(args, found, n)
    if paths is None and not store_only:
        write_verdict(found)
    return 1 if found else 0


if __name__ == "__main__":
    _args = sys.argv[1:]
    _paths = _args[_args.index("--paths") + 1:] if "--paths" in _args else None
    if _paths is not None or store_task.in_server():
        
        
        
        sys.exit(main())

    
    _local_found, _local_n = scan_local()
    if store_task.targets_live_store():
        try:
            _result = store_task.forward("config-secret-scan", ["--store-only", "--json"])
        except (store_task.store_mcp.StoreUnreachable, store_task.store_mcp.ToolError) as exc:
            sys.stderr.write("config-secret-scan: the store did not run the store-tree scan "
                             "(%s)\n" % exc)
            sys.exit(store_task.UNREACHABLE_EXIT)
        try:
            _store_out = json.loads(_result.get("stdout") or "{}")
        except ValueError:
            _store_out = {}
        _store_found = _store_out.get("findings") or []
        _store_n = _store_out.get("scanned") or 0
    else:
        _store_found, _store_n = scan_store()

    _found = _local_found + _store_found
    _n = _local_n + _store_n
    _report(_args, _found, _n)
    write_verdict(_found)
    sys.exit(1 if _found else 0)
