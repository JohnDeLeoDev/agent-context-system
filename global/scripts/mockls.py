#!/usr/bin/env python3
"A fake language server, for testing lspd's multiplexing without a real one.\n\nIt records every message it receives to $MOCKLS_JOURNAL so the test can assert on what\nthe SHARED server actually saw -- which is the whole point: the interesting bugs are all\nabout what lspd forwards, suppresses, or duplicates."
import json
import os
import sys
import threading
import time

JOURNAL = os.environ.get("MOCKLS_JOURNAL", "/tmp/mockls.journal")
INDEX_MS = float(os.environ.get("MOCKLS_INDEX_MS", "0"))



REFS = int(os.environ.get("MOCKLS_REFS", "0"))
DEGRADE = os.environ.get("MOCKLS_DEGRADE_FILE", "")




DIE = os.environ.get("MOCKLS_DIE_FILE", "")



NO_SYMBOLS = [s for s in os.environ.get("MOCKLS_NO_SYMBOLS", "").split(",") if s]



FLAT = os.environ.get("MOCKLS_FLAT_SYMBOLS", "") == "1"


NULL_REFS = [s for s in os.environ.get("MOCKLS_NULL_REFS", "").split(",") if s]












SCENARIO = os.environ.get("MOCKLS_SCENARIO", "")


def note(kind, payload):
    with open(JOURNAL, "a") as fh:
        fh.write(json.dumps({"kind": kind, "payload": payload}) + "\n")
        fh.flush()


def on_name(uri, pos):
    'True when pos lands on the word `probe` in the document on disk.'
    try:
        with open(uri[len("file://"):]) as fh:
            row = fh.read().splitlines()[pos.get("line", 0)]
    except (OSError, IndexError):
        return False
    col = row.find("probe")
    return col >= 0 and col <= pos.get("character", -1) < col + len("probe")


def send(obj):
    raw = json.dumps(obj).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
    sys.stdout.buffer.flush()


def read_message(fh):
    length = None
    while True:
        line = fh.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if b":" in line:
            k, v = line.split(b":", 1)
            if k.strip().lower() == b"content-length":
                length = int(v.strip())
    if not length:
        return None
    buf = b""
    while len(buf) < length:
        c = fh.read(length - len(buf))
        if not c:
            return None
        buf += c
    return json.loads(buf)


def scenario_case(by, params):
    doc = os.path.basename((params.get("textDocument") or {}).get("uri") or "")
    pos = params.get("position") or {}
    if by == "query":
        return str(params.get("query"))
    if by == "doc":
        return doc
    return "%s:%s:%s" % (doc, pos.get("line"), pos.get("character"))


def scenario_answer(text, ws, msg):
    '(found, value): the canned result for a request, or the messages for a notification.'
    method = msg.get("method")
    if not method:
        return False, None
    params = msg.get("params") or {}
    uri = (params.get("textDocument") or {}).get("uri") or ""
    data = json.loads(text.replace("{WS}", ws).replace("{URI}", uri))
    if msg.get("id") is None:
        table = data.get("on") or {}
        if method not in table:
            return False, None
        return True, scenario_pick(table[method], params)
    table = data.get("results") or {}
    if method not in table:
        return False, None
    return True, scenario_pick(table[method], params)


def scenario_pick(spec, params):
    if isinstance(spec, dict) and "$by" in spec:
        cases = spec.get("cases") or {}
        return cases.get(scenario_case(spec["$by"], params), spec.get("default"))
    return spec


def indexing_cycle():
    "Announce a cold index, then finish. Exercises lspd's warm gate."
    send({"jsonrpc": "2.0", "method": "$/progress",
          "params": {"token": "idx1", "value": {"kind": "begin", "title": "Indexing"}}})
    time.sleep(INDEX_MS / 1000.0)
    send({"jsonrpc": "2.0", "method": "$/progress",
          "params": {"token": "idx1", "value": {"kind": "end"}}})


def main():
    if DIE and os.path.exists(DIE):
        
        
        sys.stderr.write("This build of mockls has expired.\nThe IDE will now close.\n")
        return 7
    fh = sys.stdin.buffer
    scenario, ws = None, ""
    if SCENARIO:
        with open(SCENARIO) as sf:
            scenario = sf.read()
    while True:
        msg = read_message(fh)
        if msg is None:
            return 0
        method = msg.get("method")
        mid = msg.get("id")
        note(method or "response", msg)

        if scenario is not None:
            if method == "initialize":
                root = (msg.get("params") or {}).get("rootUri") or ""
                ws = root[len("file://"):] if root.startswith("file://") else root
            else:
                found, value = scenario_answer(scenario, ws, msg)
                if found and mid is not None:
                    if isinstance(value, dict) and "$error" in value:
                        send({"jsonrpc": "2.0", "id": mid, "error": value["$error"]})
                    else:
                        send({"jsonrpc": "2.0", "id": mid, "result": value})
                    continue
                if found:
                    for out in value or []:
                        send(out)
                    continue

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"capabilities": {"referencesProvider": True},
                             "serverInfo": {"name": "mockls"}}})
            if INDEX_MS > 0:
                threading.Thread(target=indexing_cycle, daemon=True).start()
            
            
            send({"jsonrpc": "2.0", "id": 9001, "method": "workspace/configuration",
                  "params": {"items": [{"section": "mock"}]}})
        elif method == "workspace/symbol":
            q = (msg.get("params") or {}).get("query")
            send({"jsonrpc": "2.0", "id": mid,
                  "result": [{"name": "SYM:" + str(q), "kind": 5,
                              "location": {"uri": "file:///x", "range": {}}}]})
        elif method == "textDocument/documentSymbol":
            doc = ((msg.get("params") or {}).get("textDocument") or {}).get("uri", "")
            if any(frag in os.path.basename(doc) for frag in NO_SYMBOLS):
                send({"jsonrpc": "2.0", "id": mid, "result": []})
                continue
            if FLAT:
                send({"jsonrpc": "2.0", "id": mid,
                      "result": [{"name": "probe", "kind": 12, "location": {
                          "uri": doc,
                          "range": {"start": {"line": 0, "character": 0},
                                    "end": {"line": 0, "character": 26}}}}]})
                continue
            send({"jsonrpc": "2.0", "id": mid,
                  "result": [{"name": "probe", "kind": 12,
                              "range": {"start": {"line": 0, "character": 0},
                                        "end": {"line": 0, "character": 5}},
                              "selectionRange": {
                                  "start": {"line": 0, "character": 16},
                                  "end": {"line": 0, "character": 21}}}]})
        elif method == "textDocument/references":
            params = msg.get("params") or {}
            doc = (params.get("textDocument") or {}).get("uri", "")
            pos = params.get("position") or {}
            if any(frag in os.path.basename(doc) for frag in NULL_REFS) or (
                    FLAT and not on_name(doc, pos)):
                send({"jsonrpc": "2.0", "id": mid, "result": None})
                continue
            degraded = bool(DEGRADE) and os.path.exists(DEGRADE)
            n = 0 if degraded else REFS
            send({"jsonrpc": "2.0", "id": mid,
                  "result": [{"uri": "file:///x", "range": {}} for _ in range(n)]})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": None})


if __name__ == "__main__":
    sys.exit(main())
