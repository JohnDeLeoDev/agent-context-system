#!/usr/bin/env python3
'test-lspd-ordering.'
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("lspd_order_test", HERE / "lspd.py")
assert spec and spec.loader
lspd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lspd)
scratch = Path.home() / ".cache" / "tmp"
scratch.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="lspd-order-", dir=scratch) as tmp:
    path = Path(tmp) / "seed.ts"
    path.write_text("export const seed = 1;\n")
    uri = path.as_uri()
    daemon = lspd.Daemon("order-test", tmp, ["true"])
    daemon.doc_refs = {uri: {42}}
    daemon.doc_open_sent = {uri}
    close_entered = threading.Event()
    release_close = threading.Event()
    sent = []

    def send(msg):
        if msg["method"] == "textDocument/didClose":
            close_entered.set()
            assert release_close.wait(2)
        sent.append(msg["method"])

    daemon.to_server = send
    close = threading.Thread(target=daemon.handle_doc, args=(
        42, "textDocument/didClose",
        {"jsonrpc": "2.0", "method": "textDocument/didClose",
         "params": {"textDocument": {"uri": uri}}}))
    reopen = threading.Thread(target=daemon.open_held, args=(str(path), "typescript"))
    close.start()
    assert close_entered.wait(2), "close did not reach the sender"
    reopen.start()
    time.sleep(0.05)
    release_close.set()
    close.join(2)
    reopen.join(2)
    assert not close.is_alive() and not reopen.is_alive(), "document sender hung"
    assert sent == ["textDocument/didClose", "textDocument/didOpen"], sent
print("1 passed, 0 failed")
