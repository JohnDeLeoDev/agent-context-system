#!/usr/bin/env python3
"Battery for store_mcp.py: the one MCP streamable-HTTP pathway every store script uses.\n\npolicy (one pathway): store_mcp.call() does initialize -> notifications/initialized ->\ntools/call -> DELETE over MCP streamable HTTP. This battery runs a fake daemon speaking the\nminimal wire format (JSON or SSE bodies, Mcp-Session-Id header, 202 on the notification) and\nchecks store_mcp's error mapping, transport hygiene (no redirect, no proxy, token never\nleaked) and its two pure-function helpers, mcp_url and relay_env.\n\nRun: python3 test-store-mcp.py"
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store_mcp  

TOKEN = "tok-9f3a1c7e-not-a-real-secret"
TMP_BASE = os.path.expanduser("~/.cache/tmp")

passed = 0
failures = []


def check(label, ok, detail: object = ""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + str(detail) if detail else ""))




def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


@dataclass
class RPCReply:
    status: int = 200
    message: dict | None = None   
    raw_body: bytes | None = None  
    sse: bool = False
    headers: dict = field(default_factory=dict)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, tool_reply, *, session_id="sess-1", init_sse=False, notify_status=202,
                 delete_reply=None, init_headers=None):
        super().__init__(("127.0.0.1", 0), Handler)
        self.tool_reply = tool_reply  
        self.session_id = session_id
        self.init_sse = init_sse
        self.notify_status = notify_status
        self.delete_reply = delete_reply or (lambda headers: RPCReply(status=200))
        self.init_headers = init_headers or {}
        self.seen = []

    @property
    def port(self):
        return self.server_address[1]

    def respond(self, method, req_id, payload, headers):
        if method == "initialize":
            msg = {"jsonrpc": "2.0", "id": req_id,
                   "result": {"protocolVersion": store_mcp.PROTOCOL_VERSION,
                              "serverInfo": {"name": "fake", "version": "1"}, "capabilities": {}}}
            h = {"Mcp-Session-Id": self.session_id}
            h.update(self.init_headers)
            return RPCReply(message=msg, sse=self.init_sse, headers=h)
        if method == "notifications/initialized":
            return RPCReply(status=self.notify_status)
        if method == "tools/call":
            return self.tool_reply(req_id, payload.get("params") or {})
        return RPCReply(status=404)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        server = cast(Server, self.server)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        payload = json.loads(raw) if raw.strip() else {}
        method = payload.get("method")
        req_id = payload.get("id")
        server.seen.append({"http": "POST", "method": method, "id": req_id, "headers": headers})
        self._write(server.respond(method, req_id, payload, headers))

    def do_DELETE(self):
        server = cast(Server, self.server)
        headers = {k.lower(): v for k, v in self.headers.items()}
        server.seen.append({"http": "DELETE", "method": None, "id": None, "headers": headers})
        self._write(server.delete_reply(headers))

    def _write(self, reply: RPCReply):
        if reply.raw_body is not None:
            body, ctype = reply.raw_body, None
        elif reply.message is not None:
            body = json.dumps(reply.message).encode("utf-8")
            ctype = "text/event-stream" if reply.sse else "application/json"
            if reply.sse:
                body = ("data: " + body.decode("utf-8") + "\n\n").encode("utf-8")
        else:
            body, ctype = b"", None
        self.send_response(reply.status)
        if ctype:
            self.send_header("Content-Type", ctype)
        for k, v in reply.headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format, *args):
        return


def serve(tool_reply, **kw):
    server = Server(tool_reply, **kw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop(server):
    server.shutdown()
    server.server_close()


NO_ENV_FILE_HOME = os.path.join(TMP_BASE, "sm-empty-home")


def env_for(server, token: str | None = TOKEN):
    
    e = {"AGENT_CONTEXT_HOST": "127.0.0.1", "AGENT_CONTEXT_PORT": str(server.port), "HOME": NO_ENV_FILE_HOME}
    if token:
        e["AGENT_CONTEXT_TOKEN"] = token
    return e


class RawServer:
    'Answers each connection with `handler(conn)`, for replies http.server cannot send.'

    def __init__(self, handler):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.handler = handler
        self.stopped = False
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stopped:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.serve_one, args=(conn,), daemon=True).start()

    def serve_one(self, conn):
        try:
            conn.settimeout(5)
            conn.recv(65536)
            self.handler(conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self.stopped = True
        self.sock.close()




def case_relay_env():
    print("U1 relay_env: environment wins over the file")
    root = tempfile.mkdtemp(prefix="sm.", dir=TMP_BASE)
    try:
        cfg = os.path.join(root, ".config", "agent-context")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "env"), "w") as fh:
            fh.write('AGENT_CONTEXT_HOST=fromfile\nAGENT_CONTEXT_PORT=1111\n'
                     'AGENT_CONTEXT_TOKEN="file-token"\n')
        found = store_mcp.relay_env({"HOME": root, "AGENT_CONTEXT_HOST": "fromenv"})
        check("env var wins", found["AGENT_CONTEXT_HOST"] == "fromenv", found)
        check("file fills the gap", found["AGENT_CONTEXT_PORT"] == "1111", found)
        check("quoted value unquoted", found["AGENT_CONTEXT_TOKEN"] == "file-token", found)

        print("U1 relay_env: export prefix")
        with open(os.path.join(cfg, "env"), "w") as fh:
            fh.write("export AGENT_CONTEXT_HOST=exported\n")
        found = store_mcp.relay_env({"HOME": root})
        check("export prefix stripped", found["AGENT_CONTEXT_HOST"] == "exported", found)

        print("U1 relay_env: AGENT_CONTEXT_ENV_FILE overrides the path")
        alt = os.path.join(root, "alt-env")
        with open(alt, "w") as fh:
            fh.write("AGENT_CONTEXT_HOST=altfile\n")
        found = store_mcp.relay_env({"HOME": root, "AGENT_CONTEXT_ENV_FILE": alt})
        check("alt path used", found["AGENT_CONTEXT_HOST"] == "altfile", found)

        print("U1 relay_env: single quotes")
        with open(os.path.join(cfg, "env"), "w") as fh:
            fh.write("AGENT_CONTEXT_HOST='single'\n")
        found = store_mcp.relay_env({"HOME": root})
        check("single-quoted value unquoted", found["AGENT_CONTEXT_HOST"] == "single", found)

        print("U1 relay_env: no file, no env: all None")
        found = store_mcp.relay_env({"HOME": os.path.join(root, "nowhere")})
        check("all keys None", all(v is None for v in found.values()), found)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_mcp_url():
    print("U2 mcp_url rules")
    check("loopback default -> http 8765", store_mcp.mcp_url({}) == "http://127.0.0.1:8765/mcp")
    check("remote host, no port -> https, no port",
          store_mcp.mcp_url({"AGENT_CONTEXT_HOST": "example.com"}) == "https://example.com/mcp")
    check("remote host, explicit port -> http with that port",
          store_mcp.mcp_url({"AGENT_CONTEXT_HOST": "example.com", "AGENT_CONTEXT_PORT": "9999"})
          == "http://example.com:9999/mcp")
    check("IPv6 loopback bracketed, http, default port",
          store_mcp.mcp_url({"AGENT_CONTEXT_HOST": "::1"}) == "http://[::1]:8765/mcp")
    check("IPv6 remote bracketed, https, no port",
          store_mcp.mcp_url({"AGENT_CONTEXT_HOST": "2001:db8::1"}) == "https://[2001:db8::1]/mcp")
    try:
        store_mcp.mcp_url({"AGENT_CONTEXT_HOST": "example.com", "AGENT_CONTEXT_PORT": "abc"})
        check("non-numeric port refused", False)
    except store_mcp.StoreUnreachable:
        check("non-numeric port refused", True)




def case_json_success():
    print("C1 JSON response, tool result is a JSON object")
    server = serve(lambda rid, params: RPCReply(message={"jsonrpc": "2.0", "id": rid,
                                                          "result": text_result('{"body": "hi"}')}))
    try:
        result = store_mcp.call("get_doc", {"path": "x.md"}, env=env_for(server))
        check("value decoded", result == {"body": "hi"}, result)
        calls = [s["method"] for s in server.seen if s["http"] == "POST"]
        check("full handshake order", calls == ["initialize", "notifications/initialized", "tools/call"], calls)
        deletes = [s for s in server.seen if s["http"] == "DELETE"]
        check("session closed", len(deletes) == 1, server.seen)
        check("DELETE carries the session id",
              deletes and deletes[0]["headers"].get("mcp-session-id") == "sess-1", deletes)
    finally:
        stop(server)


def case_sse_success():
    print("C1 SSE response")
    server = serve(lambda rid, params: RPCReply(sse=True, message={"jsonrpc": "2.0", "id": rid,
                                                                    "result": text_result("plain text")}))
    server2 = Server(lambda rid, params: RPCReply(message={"jsonrpc": "2.0", "id": rid,
                                                            "result": text_result('"ok"')}),
                     init_sse=True)
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    try:
        result = store_mcp.call("get_doc", {"path": "x.md"}, env=env_for(server))
        result2 = store_mcp.call("get_doc", {"path": "x.md"}, env=env_for(server2))
        check("SSE tools/call result parsed", result == "plain text", result)
        check("SSE initialize leg also works", result2 == "ok", result2)
    finally:
        stop(server)
        stop(server2)


def case_auth_header():
    print("C1 Authorization header sent only with a token")
    server = serve(lambda rid, params: RPCReply(message={"jsonrpc": "2.0", "id": rid,
                                                          "result": text_result('"ok"')}))
    try:
        store_mcp.call("get_doc", {}, env=env_for(server, token=TOKEN))
        with_token = [s["headers"].get("authorization") for s in server.seen if s["http"] == "POST"]
        check("token present on every POST", all(h == "Bearer " + TOKEN for h in with_token), with_token)
        server.seen.clear()
        store_mcp.call("get_doc", {}, env=env_for(server, token=None))
        without_token = [s["headers"].get("authorization") for s in server.seen if s["http"] == "POST"]
        check("no Authorization header without a token", all(h is None for h in without_token), without_token)
    finally:
        stop(server)


def case_http_error():
    print("C3 HTTP error status -> StoreUnreachable, body and token never in the message")
    def tool_reply(rid, params):
        return RPCReply(status=500, raw_body=("boom " + TOKEN).encode("utf-8"))
    server = serve(tool_reply)
    try:
        try:
            store_mcp.call("get_doc", {}, env=env_for(server))
            check("raised StoreUnreachable", False)
        except store_mcp.StoreUnreachable as exc:
            check("raised StoreUnreachable", True)
            check("message names the HTTP status", "500" in str(exc), str(exc))
            check("token absent from the message", TOKEN not in str(exc), str(exc))
            check("body text absent from the message", "boom" not in str(exc), str(exc))
    finally:
        stop(server)


def case_jsonrpc_error():
    print("C3 JSON-RPC protocol error -> StoreUnreachable")
    def tool_reply(rid, params):
        return RPCReply(message={"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "nope"}})
    server = serve(tool_reply)
    try:
        try:
            store_mcp.call("get_doc", {}, env=env_for(server))
            check("raised StoreUnreachable", False)
        except store_mcp.StoreUnreachable:
            check("raised StoreUnreachable", True)
    finally:
        stop(server)


def case_is_error():
    print("C3 isError result -> ToolError")
    def tool_reply(rid, params):
        return RPCReply(message={"jsonrpc": "2.0", "id": rid, "result": text_result("the tool refused", is_error=True)})
    server = serve(tool_reply)
    try:
        try:
            store_mcp.call("get_doc", {}, env=env_for(server))
            check("raised ToolError", False)
        except store_mcp.ToolError as exc:
            check("raised ToolError", True)
            check("message carries the tool's text", "the tool refused" in str(exc), str(exc))
    finally:
        stop(server)


def case_error_dict():
    print('C3 {"error": ...} result -> ToolError with .detail')
    def tool_reply(rid, params):
        return RPCReply(message={"jsonrpc": "2.0", "id": rid, "result": text_result('{"error": "no such doc"}')})
    server = serve(tool_reply)
    try:
        try:
            store_mcp.call("get_doc", {}, env=env_for(server))
            check("raised ToolError", False)
        except store_mcp.ToolError as exc:
            check("raised ToolError", True)
            check("detail carries the parsed dict", getattr(exc, "detail", None) == {"error": "no such doc"},
                  getattr(exc, "detail", None))
    finally:
        stop(server)


def case_non_json_text():
    print("C3 non-JSON text result returned as str")
    server = serve(lambda rid, params: RPCReply(message={"jsonrpc": "2.0", "id": rid,
                                                          "result": text_result("hello world")}))
    try:
        result = store_mcp.call("get_doc", {}, env=env_for(server))
        check("returned as-is string", result == "hello world", result)
    finally:
        stop(server)


def case_null_result():
    print("C3 tool text 'null' -> Python None")
    server = serve(lambda rid, params: RPCReply(message={"jsonrpc": "2.0", "id": rid,
                                                          "result": text_result("null")}))
    try:
        result = store_mcp.call("get_doc", {}, env=env_for(server))
        check("result is None", result is None, result)
    finally:
        stop(server)


def case_size_cap():
    print("C5 size cap (MAX_BYTES monkeypatched small)")
    big = "x" * 10000
    server = serve(lambda rid, params: RPCReply(message={"jsonrpc": "2.0", "id": rid,
                                                          "result": text_result(json.dumps(big))}))
    old = store_mcp.MAX_BYTES
    store_mcp.MAX_BYTES = 100
    try:
        try:
            store_mcp.call("get_doc", {}, env=env_for(server))
            check("raised StoreUnreachable", False)
        except store_mcp.StoreUnreachable as exc:
            check("raised StoreUnreachable", True)
            check("reason names the size", "large" in str(exc), str(exc))
    finally:
        store_mcp.MAX_BYTES = old
        stop(server)


def case_deadline():
    print("C5 deadline (DEADLINE monkeypatched small, server drips slowly)")
    def drip(conn):
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n")
        for _ in range(30):
            conn.sendall(b" ")
            time.sleep(0.2)
    server = RawServer(drip)
    old = store_mcp.DEADLINE
    store_mcp.DEADLINE = 0.5
    try:
        started = time.time()
        try:
            store_mcp.call("get_doc", {}, env=env_for(server))
            check("raised StoreUnreachable", False)
        except store_mcp.StoreUnreachable as exc:
            elapsed = time.time() - started
            check("raised StoreUnreachable", True)
            check("returned near the deadline, not the full drip", elapsed < 5, "%.1fs" % elapsed)
            check("reason names the delay", "long" in str(exc), str(exc))
    finally:
        store_mcp.DEADLINE = old
        server.close()


def case_redirect_not_followed():
    print("C3 no redirect follow: a 302 to another port never receives the token")
    elsewhere = serve(lambda rid, params: RPCReply(message={"jsonrpc": "2.0", "id": rid,
                                                             "result": text_result('"ok"')}))
    def tool_reply(rid, params):
        return RPCReply(status=302, headers={"Location": "http://127.0.0.1:%d/mcp" % elsewhere.port})
    server = serve(tool_reply)
    try:
        try:
            store_mcp.call("get_doc", {}, env=env_for(server))
            check("raised StoreUnreachable", False)
        except store_mcp.StoreUnreachable:
            check("raised StoreUnreachable", True)
        check("the redirect target was never contacted", not elsewhere.seen, elsewhere.seen)
    finally:
        stop(server)
        stop(elsewhere)


def case_delete_on_failure():
    print("C3 session DELETE sent at the end even when the tool call fails")
    def tool_reply(rid, params):
        return RPCReply(message={"jsonrpc": "2.0", "id": rid, "result": text_result("boom", is_error=True)})
    server = serve(tool_reply)
    try:
        try:
            store_mcp.call("get_doc", {}, env=env_for(server))
        except store_mcp.ToolError:
            pass
        deletes = [s for s in server.seen if s["http"] == "DELETE"]
        check("DELETE was sent despite the failed call", len(deletes) == 1, server.seen)
    finally:
        stop(server)


def case_proxy_ignored():
    print("C5 proxy env is ignored: request goes direct")
    server = serve(lambda rid, params: RPCReply(message={"jsonrpc": "2.0", "id": rid,
                                                          "result": text_result('"ok"')}))
    proxy = serve(lambda rid, params: RPCReply(message={"jsonrpc": "2.0", "id": rid,
                                                         "result": text_result('"from the proxy"')}))
    old_http_proxy = os.environ.get("http_proxy")
    old_HTTP_PROXY = os.environ.get("HTTP_PROXY")
    os.environ["http_proxy"] = "http://127.0.0.1:%d" % proxy.port
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:%d" % proxy.port
    try:
        result = store_mcp.call("get_doc", {}, env=env_for(server))
        check("daemon answered directly", result == "ok", result)
        check("the proxy saw nothing", not proxy.seen, proxy.seen)
    finally:
        if old_http_proxy is None:
            os.environ.pop("http_proxy", None)
        else:
            os.environ["http_proxy"] = old_http_proxy
        if old_HTTP_PROXY is None:
            os.environ.pop("HTTP_PROXY", None)
        else:
            os.environ["HTTP_PROXY"] = old_HTTP_PROXY
        stop(server)
        stop(proxy)


def main():
    os.makedirs(TMP_BASE, exist_ok=True)
    case_relay_env()
    case_mcp_url()
    case_json_success()
    case_sse_success()
    case_auth_header()
    case_http_error()
    case_jsonrpc_error()
    case_is_error()
    case_error_dict()
    case_non_json_text()
    case_null_result()
    case_size_cap()
    case_deadline()
    case_redirect_not_followed()
    case_delete_on_failure()
    case_proxy_ignored()
    total = passed + len(failures)
    print("\n%d/%d passed" % (passed, total))
    for label in failures:
        print("FAILED: " + label)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
