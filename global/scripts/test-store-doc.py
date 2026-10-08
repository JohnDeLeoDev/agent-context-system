#!/usr/bin/env python3
"Battery for store-doc.py: print one store doc fetched via the get_doc MCP tool.\n\nUsed by the ralph commands to read a prompt body on a machine with no shared-docs/ tree.\npolicy (one pathway): the daemon is now reached only through store_mcp.call, so every case\nhere stubs that one call in-process and checks store-doc's own contract: exit codes, the\nsingle `store-doc: <reason>` stderr line, no token or response detail ever printed, empty\nand missing docs, UTF-8 output, usage errors, and a broken pipe on the reader's end.\n\nMoved to test-store-mcp.py (the transport store_mcp.py itself owns, so it is out of scope\nhere): relay env from variables or the config file, HTTP status handling, JSON-RPC and\nisError mapping, no redirect, no proxy, the size cap and the wall-clock deadline.\n\nRun: python3 test-store-doc.py"
import importlib.util
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store_mcp  

TOKEN = "tok-9f3a1c7e-not-a-real-secret"
BODY = "prompt body line one\n\nsecond paragraph with unicode: café ✓\n"
ERROR_PREFIX = "store-doc:"

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


def load_store_doc():
    spec = importlib.util.spec_from_file_location("store_doc_under_test", os.path.join(HERE, "store-doc.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


STORE_DOC = load_store_doc()


class _Out:
    def __init__(self):
        self.buffer = io.BytesIO()


def run(args, fake_call):
    "Run store-doc's main() in-process with store_mcp.call stubbed."
    old_call = store_mcp.call
    old_stdout, old_stderr = sys.stdout, sys.stderr
    store_mcp.call = fake_call
    out, err = _Out(), io.StringIO()
    sys.stdout, sys.stderr = out, err
    try:
        rc = STORE_DOC.main(args)
    finally:
        store_mcp.call = old_call
        sys.stdout, sys.stderr = old_stdout, old_stderr
    return rc, out.buffer.getvalue(), err.getvalue()


def error_lines(err):
    return [ln for ln in err.splitlines() if ln.strip()]


def one_store_doc_line(err):
    lines = error_lines(err)
    return len(lines) == 1 and lines[0].startswith(ERROR_PREFIX)


def case_success():
    print("C1 success")
    calls = []

    def fake_call(tool, args, env=None):
        calls.append((tool, args))
        return {"body": BODY}

    rc, out, err = run(["ralph-cleanup-universal.md"], fake_call)
    check("rc 0", rc == 0, rc)
    check("stdout is the doc body, byte for byte", out == BODY.encode("utf-8"), out[:80])
    check("nothing on stderr", err == "", err)
    check("get_doc called once with the path", calls == [("get_doc", {"path": "ralph-cleanup-universal.md"})], calls)


def case_nested_path():
    print("C1 nested doc path")
    calls = []

    def fake_call(tool, args, env=None):
        calls.append((tool, args))
        return {"body": BODY}

    rc, out, err = run(["consolidation/plan.md"], fake_call)
    check("nested path passed through", calls == [("get_doc", {"path": "consolidation/plan.md"})], calls)
    check("rc 0", rc == 0)


def case_missing_doc():
    print("C3 missing doc: get_doc returns no body")
    rc, out, err = run(["nope.md"], lambda tool, args, env=None: {"body": None})
    check("rc 1", rc == 1, rc)
    check("stdout empty", out == b"", out)
    check("one store-doc: line", one_store_doc_line(err), err)
    check("names 'no such doc'", "no such doc" in err, err)


def case_empty_doc():
    print("C3 empty doc")
    rc, out, err = run(["blank.md"], lambda tool, args, env=None: {"body": "   \n"})
    check("rc 1", rc == 1, rc)
    check("stdout empty", out == b"", out)
    check("one store-doc: line", one_store_doc_line(err), err)
    check("names the empty doc", "empty" in err and "blank.md" in err, err)


def case_tool_error():
    print("C3 ToolError from get_doc")
    def fake_call(tool, args, env=None):
        raise store_mcp.ToolError("authentication required")
    rc, out, err = run(["x.md"], fake_call)
    check("rc 1", rc == 1, rc)
    check("stdout empty", out == b"", out)
    check("one store-doc: line", one_store_doc_line(err), err)
    check("token absent from stderr", TOKEN not in err, err)


def case_store_unreachable():
    print("C3 StoreUnreachable from the daemon")
    def fake_call(tool, args, env=None):
        raise store_mcp.StoreUnreachable("the daemon is unreachable (ConnectionRefusedError)")
    rc, out, err = run(["x.md"], fake_call)
    check("rc 1", rc == 1, rc)
    check("stdout empty", out == b"", out)
    check("one store-doc: line", one_store_doc_line(err), err)
    check("token absent from stderr", TOKEN not in err, err)


def case_no_token_leak():
    print("C3 no token leak even when the failure text carries it")
    def fake_call(tool, args, env=None):
        raise store_mcp.ToolError("boom " + TOKEN)
    rc, out, err = run(["x.md"], fake_call)
    
    
    
    check("rc 1", rc == 1, rc)
    check("exactly one line on stderr", len(error_lines(err)) == 1, err)


def case_usage():
    print("C2 usage")
    for label, args in (("no argument", []), ("two arguments", ["a.md", "b.md"]), ("empty argument", [""])):
        calls = []
        rc, out, err = run(args, lambda tool, a, env=None, calls=calls: (calls.append(1), {"body": BODY})[1])
        check(label + ": exit 2, no call, nothing on stdout",
              rc == 2 and not calls and out == b"", "%d %r %r" % (rc, out, err))


def case_utf8():
    print("C4 UTF-8 body with unicode")
    rc, out, err = run(["x.md"], lambda tool, args, env=None: {"body": "café ✓\n"})
    check("rc 0", rc == 0)
    check("bytes are UTF-8", out == "café ✓\n".encode("utf-8"), out)


def case_broken_pipe():
    print("C5 the reader went away (BrokenPipeError)")
    r, w = os.pipe()
    os.close(r)  
    wfile = os.fdopen(w, "wb")
    old_stdout = sys.stdout
    old_call = store_mcp.call

    class Out:
        buffer = wfile

    sys.stdout = Out()
    store_mcp.call = lambda tool, args, env=None: {"body": BODY}
    try:
        rc = STORE_DOC.main(["x.md"])
    finally:
        sys.stdout = old_stdout
        store_mcp.call = old_call
        try:
            wfile.close()
        except OSError:
            pass
    check("rc 1 on a broken pipe", rc == 1, rc)


def main():
    case_success()
    case_nested_path()
    case_missing_doc()
    case_empty_doc()
    case_tool_error()
    case_store_unreachable()
    case_no_token_leak()
    case_usage()
    case_utf8()
    case_broken_pipe()
    total = passed + len(failures)
    print("\n%d/%d passed" % (passed, total))
    for label in failures:
        print("FAILED: " + label)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
