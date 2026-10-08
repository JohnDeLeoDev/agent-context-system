#!/usr/bin/env python3
'The one way a store script reaches agent-context content: an MCP tools/call to the daemon.\n\npolicy (one pathway) and policy (a script you run reaches content only through MCP). Scripts\nimport this module and never open an HTTP route, a store file or another copy of the env\nparse. Standard library only: it runs on relay machines and the Synology nodes, from hooks,\nwith no venv.\n\nThe daemon is reached the way the relay reaches it: AGENT_CONTEXT_HOST, AGENT_CONTEXT_PORT\nand AGENT_CONTEXT_TOKEN from the environment, gaps filled from ~/.config/agent-context/env\n(AGENT_CONTEXT_ENV_FILE overrides the path). No host means the local daemon on\n127.0.0.1:8765. A remote host gets https and no port unless a port is set. The bearer token\ngoes to the configured host only: no redirects, no proxy.\n\n    import store_mcp\n    doc = store_mcp.call("get_doc", {"path": "ralph-cleanup-universal.md"})\n\n`call` returns the tool\'s result parsed as JSON when it is JSON, else the text. A tool that\nanswers {"error": ...} raises ToolError; transport trouble raises StoreUnreachable. Both\ncarry a message safe to print: never a response body, never the token.\n\n`deadline` caps one call\'s wall clock, every request in it included; a hook passes what is\nleft of its own timeout. `meta` rides along as the tools/call `_meta`. A tool that resolves\na cwd (resolve_project) needs `evidence_meta(repo_evidence(cwd))` there when the daemon is\non another machine: that daemon cannot read this machine\'s disk.'
import json
import os
import re
import subprocess
import sys
import time





RELAY_ENV_KEYS = ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_TOKEN", "AGENT_CONTEXT_PORT")
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 15
DEADLINE = float(os.environ.get("STORE_MCP_DEADLINE") or 60)  
MAX_BYTES = 64 * 1024 * 1024
EVIDENCE_META_KEY = "agent-context/project-evidence"  
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class StoreUnreachable(Exception):
    'The daemon could not be reached or answered outside the protocol.'


class ToolError(Exception):
    'The tool ran and refused: its own error text.'

    detail: "dict | None" = None  


def _opener():
    'An opener with no proxy and no redirects.'
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  

    
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def relay_env(env=None):
    'AGENT_CONTEXT_HOST/TOKEN/PORT: the environment wins, the env file fills gaps.'
    env = os.environ if env is None else env
    found = {k: env.get(k) or None for k in RELAY_ENV_KEYS}
    home = env.get("HOME") or os.path.expanduser("~")
    path = env.get("AGENT_CONTEXT_ENV_FILE") or os.path.join(home, ".config", "agent-context", "env")
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError:
        text = ""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not sep or key not in RELAY_ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not found[key]:
            found[key] = value
    return found


def is_remote(env):
    'True when the host names a daemon on another machine (the relay-only model).'
    import ipaddress
    host = env.get("AGENT_CONTEXT_HOST") or "127.0.0.1"
    if host.lower().rstrip(".") == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


IDENTITY_FILE = "relay-identity.json"   
IDENTITY_HEADERS = ("x-agent-context-", "x-relay-source-etag")


def _state_dir(env):
    "The server's paths.state_dir(), for a script that cannot import the server."
    home = env.get("HOME") or os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "agent-context")
    return os.path.join(env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"),
                        "agent-context")


def identity_headers(env):
    'identity headers.'
    if not is_remote(env):
        return {}
    try:
        with open(os.path.join(_state_dir(env), IDENTITY_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and k.startswith(IDENTITY_HEADERS)
            and 0 < len(v) <= 512 and v.isprintable()}


def mcp_url(env):
    "Same rule as the server's daemon.mcp_url()."
    import ipaddress
    host = env.get("AGENT_CONTEXT_HOST") or "127.0.0.1"
    port = env.get("AGENT_CONTEXT_PORT")
    if port and not port.isdigit():
        raise StoreUnreachable("AGENT_CONTEXT_PORT is not a number")
    try:
        if ipaddress.ip_address(host).version == 6:
            host = "[%s]" % host
    except ValueError:
        pass
    if is_remote(env) and not port:
        return "https://%s/mcp" % host
    return "http://%s:%s/mcp" % (host, port or 8765)


def _git(args):
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_evidence(directory):
    "What this machine's disk says about the repo at `directory`, in the shape the\n    server's identity.project_evidence gives: {cwd (the repo root), marker_id, remotes}.\n    Credentials in an https remote are dropped before the url leaves the machine."
    import urllib.parse
    root = _git(["-C", directory, "rev-parse", "--show-toplevel"]) or directory
    marker = None
    try:
        with open(os.path.join(root, ".agents", "project-id"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r'^id *= *"(.*)"', line.rstrip("\n"))
                if m:
                    marker = m.group(1) if UUID_RE.match(m.group(1)) else None
                    break
    except OSError:
        pass
    urls = []
    for name in _git(["-C", root, "remote"]).split():
        url = _git(["-C", root, "remote", "get-url", name])
        parts = urllib.parse.urlsplit(url)
        if parts.scheme in ("http", "https") and "@" in parts.netloc:
            url = urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
        if url and url not in urls:
            urls.append(url)
    return {"cwd": root, "marker_id": marker, "remotes": urls}


def evidence_meta(evidence):
    'The tools/call `_meta` that carries repo_evidence to the server.'
    return {EVIDENCE_META_KEY: evidence}


def _read_capped(response, started, deadline):
    chunks, size = [], 0
    while True:
        if time.monotonic() - started > deadline:
            raise StoreUnreachable("the daemon took too long")
        chunk = response.read1(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BYTES:
            raise StoreUnreachable("the response is too large")


def _messages(raw, content_type):
    'JSON-RPC messages from a JSON body or an SSE stream.'
    text = raw.decode("utf-8")
    if "text/event-stream" not in (content_type or ""):
        return [json.loads(text)] if text.strip() else []
    out, data = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            out.append(json.loads("\n".join(data)))
            data = []
    return out


class _Session:
    def __init__(self, env, deadline, read_timeout=None):
        self.url = mcp_url(env)
        self.deadline = deadline
        
        
        self.read_timeout = read_timeout or TIMEOUT
        self.token = env.get("AGENT_CONTEXT_TOKEN")
        self.identity = {}                      
        self.session_id = None
        self.next_id = 0
        self.opener = _opener()

    def _post(self, payload, started):
        import http.client
        import urllib.error
        import urllib.request
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION, **self.identity}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
        left = self.deadline - (time.monotonic() - started)
        if left <= 0:
            raise StoreUnreachable("the daemon took too long")
        try:
            with self.opener.open(request, timeout=min(self.read_timeout, left)) as response:
                self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
                return _messages(_read_capped(response, started, self.deadline),
                                 response.headers.get("Content-Type"))
        except urllib.error.HTTPError as exc:
            raise StoreUnreachable("the daemon answered HTTP %d" % exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise StoreUnreachable("the daemon is unreachable (%s)"
                                   % type(getattr(exc, "reason", exc)).__name__)
        except (http.client.HTTPException, ValueError, UnicodeError) as exc:
            raise StoreUnreachable("the request failed (%s)" % type(exc).__name__)

    def request(self, method, params, started):
        self.next_id += 1
        want = self.next_id
        for msg in self._post({"jsonrpc": "2.0", "id": want, "method": method, "params": params},
                              started):
            if msg.get("id") != want:
                continue
            if "error" in msg:
                raise StoreUnreachable("the daemon refused %s (%s)"
                                       % (method, (msg["error"] or {}).get("code")))
            return msg.get("result") or {}
        raise StoreUnreachable("no response to %s" % method)

    def notify(self, method, started):
        self._post({"jsonrpc": "2.0", "method": method}, started)

    def close(self):
        'End the server-side session; best effort, never raises.'
        import http.client
        import urllib.error
        import urllib.request
        if not self.session_id:
            return
        headers = {"Mcp-Session-Id": self.session_id, "MCP-Protocol-Version": PROTOCOL_VERSION,
                   **self.identity}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            self.opener.open(urllib.request.Request(self.url, headers=headers, method="DELETE"),
                             timeout=TIMEOUT).close()
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            pass


def _result_value(result):
    text = "".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    if result.get("isError"):
        raise ToolError(text.strip() or "the tool failed")
    if isinstance(value, dict) and "error" in value:  
        exc = ToolError(str(value["error"]))
        exc.detail = value
        raise exc
    return value


def call(tool, arguments=None, env=None, meta=None, deadline=None, read_timeout=None):
    'Run one MCP tool on the daemon and return its result (JSON-parsed when it is JSON).\n    `read_timeout` lifts the 15 s cap on one socket read, for a tool that says nothing\n    until it finishes; the whole call is still bounded by `deadline`.'
    started = time.monotonic()
    relay = relay_env(env)
    session = _Session(relay, DEADLINE if deadline is None else deadline, read_timeout)
    
    
    session.identity = identity_headers({**(os.environ if env is None else env), **{
        k: v for k, v in relay.items() if v}})
    params = {"name": tool, "arguments": arguments or {}}
    if meta:
        params["_meta"] = meta
    try:
        session.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                       "clientInfo": {"name": "store_mcp", "version": "1"}}, started)
        session.notify("notifications/initialized", started)
        result = session.request("tools/call", params, started)
    finally:
        session.close()
    return _result_value(result)
