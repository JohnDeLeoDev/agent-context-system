#!/usr/bin/env python3
"A hook or script on a relay machine called the daemon with the token and no machine\nidentity, so the daemon logged an unbound network session for each call. The machine's\nrelay bridge now leaves its identity headers in the state directory, and store_mcp sends\nthem. This checks what store_mcp reads from that file and that the headers go out on the\nwire."
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_mcp  

failures = []
passed = 0


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL %s %s" % (label, detail))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.server.seen.append({k.lower(): v for k, v in self.headers.items()})
        if "id" not in body:
            self.send_response(202)
            self.end_headers()
            return
        result = ({"protocolVersion": store_mcp.PROTOCOL_VERSION, "capabilities": {},
                   "serverInfo": {"name": "t", "version": "0"}}
                  if body.get("method") == "initialize"
                  else {"content": [{"type": "text", "text": '"ok"'}], "isError": False})
        raw = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Mcp-Session-Id", "s1")
        self.end_headers()
        self.wfile.write(raw)

    def do_DELETE(self):
        self.server.seen.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def main():
    home = tempfile.mkdtemp(prefix="store-mcp-identity-")
    try:
        env = {"AGENT_CONTEXT_HOST": "ls.example.net", "HOME": home,
               "XDG_STATE_HOME": os.path.join(home, "state")}
        check("no file, no headers", store_mcp.identity_headers(env) == {})
        state = store_mcp._state_dir(env)
        os.makedirs(state, exist_ok=True)
        path = os.path.join(state, store_mcp.IDENTITY_FILE)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"x-agent-context-machine": "uuid-1", "x-agent-context-machine-id": "laptop",
                       "x-relay-source-etag": "abc", "authorization": "Bearer stolen",
                       "x-agent-context-home": "bad\nvalue", "x-agent-context-platform": 7}, fh)
        want = {"x-agent-context-machine": "uuid-1", "x-agent-context-machine-id": "laptop",
                "x-relay-source-etag": "abc"}
        got = store_mcp.identity_headers(env)
        check("the bridge's identity headers are read, and nothing else in the file",
              got == want, got)
        check("none on the daemon's own host",
              store_mcp.identity_headers({**env, "AGENT_CONTEXT_HOST": "127.0.0.1"}) == {})

        
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.seen = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        real = store_mcp.is_remote
        try:
            store_mcp.is_remote = lambda e: True
            wire = {**env, "AGENT_CONTEXT_HOST": "127.0.0.1",
                    "AGENT_CONTEXT_PORT": str(server.server_address[1])}
            store_mcp.call("get_health", {}, env=wire)
        finally:
            store_mcp.is_remote = real
            server.shutdown()
            server.server_close()
        check("every request of the call carries them",
              len(server.seen) >= 3 and all(
                  all(s.get(k) == v for k, v in want.items()) for s in server.seen), server.seen)
        check("and nothing the file should not add",
              all(s.get("authorization") is None for s in server.seen))

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        check("an unreadable file is no identity", store_mcp.identity_headers(env) == {})
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print("\n%d/%d passed" % (passed, passed + len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
