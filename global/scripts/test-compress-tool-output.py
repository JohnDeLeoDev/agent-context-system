#!/usr/bin/env python3
"Battery for compress-tool-output (the hook) and tool_output_compress (its rules).\n\n  [1] the rules: what compress() shortens, what it keeps, and when it declines\n  [2] the hook: each result shape it rewrites, what it skips, the saved original\n  [3] through hook-dispatch.py, which must carry updatedToolOutput\n  [4] codex-hook-adapter's translation of updatedToolOutput for Codex\n  [5] the generated opencode plugin's execute.after, under node with a stand-in context\n      (skipped, and said so, when node is absent)\n  [6] the generated pi extension's tool_result, through test-pi-hooks-extension's helpers\n\nThe inputs are shaped after real tool results (a tar run that repeats one warning, a\ncompile log, a search listing under one worktree, an app log). Every hook run gets a\nHOME of its own, so nothing is written to this machine's state directory.\n\nUsage: python3 test-compress-tool-output.py      exit 0 when every check passes"
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "hooks", "compress-tool-output.py")
DISPATCH = os.path.join(HERE, "hook-dispatch.py")
sys.path.insert(0, HERE)
import tool_output_compress as toc  

TMP = tempfile.mkdtemp(prefix="compress-tool-output-")
PASSED = FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print("  PASS " + label)
    else:
        FAILED += 1
        print("  FAIL %s  -- %s" % (label, str(detail)[:600]))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TAR = "tar: Ignoring unknown extended header keyword 'LIBARCHIVE.xattr.com.apple.provenance'"
ROOT = "/Users/someone/Developer/Project/.agents/worktrees/share-queue/cli/src/"


def build_log():
    lines = ["start of run"] + [TAR] * 300
    lines += ["Compiling module_%d (step %d of 40) in 0.%d s" % (i, i, i) for i in range(1, 41)]
    lines += ["error: build step 7 failed with code 3", "end of run"]
    return "\n".join(lines)


def search_listing():
    return "\n".join("%sfile_%d.rs:%d:fn handler_%d() -> Result<(), String> {"
                     % (ROOT, i % 9, 10 + i, i) for i in range(60))


def app_log():
    lines = []
    for i in range(80):
        lines.append("2026-09-28 17:%02d:11 [warn] QueryClient error: {\"name\":\"TypeError\","
                     "\"message\":\"Failed to fetch\"}" % (i % 60))
    lines.append("2026-09-28 18:39:20 [warn] a line that appears once")
    return "\n".join(lines)


def check_rules():
    print("[1] the rules")
    check("a short text is left alone", toc.compress("x\n" * 100) is None)
    check("a non-string is left alone", toc.compress(None) is None)

    got = toc.compress(build_log())
    text = got.text if got else ""
    check("a build log shrinks to under a tenth", got is not None
          and len(text) * 10 < got.original_bytes, got)
    check("a line repeated in a row is shown once with its count",
          text.count(TAR) == 1 and "299 more lines identical to the line above" in text, text)
    check("a run of same-form lines keeps its first three and last two",
          all("Compiling module_%d " % i in text for i in (1, 2, 3, 39, 40))
          and "Compiling module_20 " not in text
          and "35 lines of the same form" in text, text)
    check("the first line, the error line and the last line stay",
          text.startswith("start of run\n") and "error: build step 7 failed with code 3" in text
          and text.endswith("end of run"), text)
    check("dropped_lines counts every dropped line", got and got.dropped_lines == 299 + 35, got)

    errors = "\n".join("src/module_%d.c: error: undefined reference to symbol_%d" % (i, i)
                       for i in range(120))
    check("lines that name an error are never thinned as a run", toc.compress(errors) is None)

    got = toc.compress(search_listing())
    text = got.text if got else ""
    check("a shared directory is stated once and cut from each line",
          got is not None and got.prefix == ROOT and text.count(ROOT) == 1
          and text.splitlines()[0].startswith(toc.MARK)
          and "file_3.rs:13:fn handler_3()" in text, text[:400])
    check("every search hit survives",
          all("handler_%d()" % i in text for i in range(60)), text[:400])

    got = toc.compress(app_log())
    text = got.text if got else ""
    check("log lines that differ only in their timestamp are dropped after two",
          got is not None and text.count("Failed to fetch") == 2
          and "apart from their timestamps" in text
          and "a line that appears once" in text, text)

    prose = "\n".join("Paragraph %d says something of its own about topic %s."
                      % (i, "abcdefghij"[i % 10] * (i % 7 + 3)) for i in range(200))
    check("text with no repetition is left alone", toc.compress(prose) is None)
    check("a result the harness already cut is left alone",
          toc.compress("<persisted-output>\nOutput too large\n" + build_log()) is None)
    again = toc.compress(build_log())
    check("a compressed text is not compressed again",
          again is not None and toc.compress(again.text + "\n" + TAR * 40) is None)


def run_hook(payload, home, raw=None):
    env = dict(os.environ, HOME=home)
    proc = subprocess.run([sys.executable, HOOK], input=raw if raw is not None
                          else json.dumps(payload), capture_output=True, text=True, env=env)
    out = proc.stdout.strip()
    try:
        doc = json.loads(out) if out else None
    except ValueError:
        doc = "not-json"
    return proc.returncode, doc, proc.stderr


def call(tool, response, **extra):
    payload = {"session_id": "0123abcd-session", "hook_event_name": "PostToolUse",
               "cwd": TMP, "tool_name": tool, "tool_input": {"command": "make"},
               "tool_use_id": "toolu_test01", "tool_response": response}
    payload.update(extra)
    return payload


def new_output(doc):
    if not isinstance(doc, dict):
        return None
    return (doc.get("hookSpecificOutput") or {}).get("updatedToolOutput")


def check_hook():
    print("[2] the hook")
    home = os.path.join(TMP, "home-bash")
    os.makedirs(home)
    log = build_log()
    rc, doc, err = run_hook(call("Bash", {"stdout": log, "stderr": "", "interrupted": False}), home)
    new = new_output(doc)
    check("a Bash result comes back in its own shape, stdout replaced",
          rc == 0 and isinstance(new, dict) and new.get("stderr") == ""
          and new.get("interrupted") is False and len(new.get("stdout", "")) * 10 < len(log),
          "%s %s %s" % (rc, doc, err))
    head = (new or {}).get("stdout", "").splitlines()[0] if new else ""
    saved = os.path.join(home, ".local", "state", "agent-context", "tool-output",
                         "0123abcd-session", "toolu_test01.txt")
    check("the first line names the saved original", head.startswith(toc.MARK) and saved in head, head)
    check("the saved file holds the original, byte for byte",
          os.path.isfile(saved) and open(saved, encoding="utf-8").read() == log)
    record = os.path.join(os.path.dirname(os.path.dirname(saved)), "savings.jsonl")
    try:
        row = json.loads(open(record, encoding="utf-8").read().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        row = {}
    check("the replacement is recorded with its sizes",
          row.get("tool") == "Bash" and row.get("before") == len(log.encode())
          and 0 < row.get("after", 0) < row.get("before", 0), row)

    home = os.path.join(TMP, "home-shapes")
    os.makedirs(home)
    rc, doc, _ = run_hook(call("Bash", log), home)
    check("a string result comes back as a string",
          rc == 0 and isinstance(new_output(doc), str) and new_output(doc).startswith(toc.MARK), doc)
    image = {"type": "image", "data": "aGk=", "mimeType": "image/png"}
    rc, doc, _ = run_hook(call("mcp__build__run", [{"type": "text", "text": "short"}, image,
                                                    {"type": "text", "text": log}]), home)
    new = new_output(doc)
    check("in a block list only the large text block changes",
          rc == 0 and isinstance(new, list) and len(new) == 3 and new[0]["text"] == "short"
          and new[1] == image and new[2]["text"].startswith(toc.MARK), doc)
    rc, doc, _ = run_hook(call("Bash", {"content": [{"type": "text", "text": log}],
                                        "isError": False, "stdout": log}), home)
    new = new_output(doc)
    check("pi's shape: stdout is replaced and its other keys are kept",
          isinstance(new, dict) and new["stdout"].startswith(toc.MARK)
          and new["isError"] is False, doc)

    for tool in ("Read", "read", "Edit", "apply_patch", "mcp__agent-context__get_session_context",
                 "get_session_context", "agent-context_get_session_context"):
        rc, doc, _ = run_hook(call(tool, log), home)
        check("%s is never touched" % tool, rc == 0 and doc is None, doc)
    rc, doc, _ = run_hook(call("Bash", {"stdout": "ok\n" * 20}), home)
    check("a small result prints nothing", rc == 0 and doc is None, doc)
    rc, doc, _ = run_hook(call("Bash", {"result": {"lines": [log]}}), home)
    check("an unknown shape prints nothing", rc == 0 and doc is None, doc)
    rc, doc, _ = run_hook(None, home, raw="{" + "x" * 5000)
    check("a payload that is not JSON prints nothing and exits 0", rc == 0 and doc is None, doc)
    rc, doc, _ = run_hook(call("Bash", log, hook_event_name="PreToolUse"), home)
    check("another event prints nothing", rc == 0 and doc is None, doc)

    rc, doc, _ = run_hook({"hook_event_name": "PostToolUse", "session_id": "cop-1", "cwd": TMP,
                           "tool_name": "Bash", "tool_input": {"command": "make"},
                           "tool_result": {"result_type": "success",
                                           "text_result_for_llm": log}}, home)
    mod = (doc or {}).get("modifiedResult") if isinstance(doc, dict) else None
    check("copilot's payload is answered with modifiedResult",
          rc == 0 and isinstance(mod, dict) and mod.get("resultType") == "success"
          and mod.get("textResultForLlm", "").startswith(toc.MARK)
          and "hookSpecificOutput" not in doc, doc)
    rc, doc, _ = run_hook({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                           "tool_result": {"result_type": "failure",
                                           "text_result_for_llm": log}}, home)
    check("a failed copilot result is left alone", rc == 0 and doc is None, doc)

    home = os.path.join(TMP, "home-sweep")
    root = os.path.join(home, ".local", "state", "agent-context", "tool-output")
    old, fresh = os.path.join(root, "old-session"), os.path.join(root, "fresh-session")
    for folder in (old, fresh):
        os.makedirs(folder)
    long_ago = time.time() - 30 * 86400
    os.utime(old, (long_ago, long_ago))
    run_hook(call("Bash", log, session_id="brand-new-session"), home)
    check("a session directory unused for a month is removed when a new one is made",
          not os.path.exists(old) and os.path.isdir(fresh)
          and os.path.isdir(os.path.join(root, "brand-new-session")), os.listdir(root))


def check_dispatch():
    print("[3] through the dispatcher")
    home = os.path.join(TMP, "home-dispatch")
    os.makedirs(home)
    registry = os.path.join(TMP, "hook-dispatch.json")
    with open(registry, "w", encoding="utf-8") as fh:
        json.dump({"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "%s %s" % (sys.executable, os.path.abspath(HOOK)),
             "timeout": 10}]}]}}, fh)
    env = dict(os.environ, HOME=home, HOOK_DISPATCH_REGISTRY=registry)
    log = build_log()
    proc = subprocess.run([sys.executable, DISPATCH, "PostToolUse"],
                          input=json.dumps(call("Bash", {"stdout": log, "stderr": ""})),
                          capture_output=True, text=True, env=env)
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        doc = None
    new = new_output(doc)
    check("the dispatcher carries the hook's updatedToolOutput",
          proc.returncode == 0 and isinstance(new, dict)
          and new.get("stdout", "").startswith(toc.MARK), "%s %s" % (proc.stdout[:300], proc.stderr[:300]))
    proc = subprocess.run([sys.executable, DISPATCH, "PostToolUse"],
                          input=json.dumps(call("Bash", {"stdout": "ok", "stderr": ""})),
                          capture_output=True, text=True, env=env)
    check("and prints nothing for a small result",
          proc.returncode == 0 and not proc.stdout.strip(), proc.stdout[:300])

    def guard(name, answer):
        path = os.path.join(TMP, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("import json\nprint(json.dumps(%r))\n" % (answer,))
        return {"type": "command", "command": "%s %s" % (sys.executable, path), "timeout": 10}

    def merged(event, guards):
        with open(registry, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {event: [{"matcher": "*", "hooks": guards}]}}, fh)
        run = subprocess.run([sys.executable, DISPATCH, event],
                             input=json.dumps(call("Bash", "x", hook_event_name=event)),
                             capture_output=True, text=True, env=env)
        try:
            return run.returncode, new_output(json.loads(run.stdout)), run.stdout
        except ValueError:
            return run.returncode, None, run.stdout

    def says(event="PostToolUse", **fields):
        return {"hookSpecificOutput": dict({"hookEventName": event}, **fields)}

    rc, new, out = merged("PostToolUse", [
        guard("ctx.py", says(additionalContext="ctx-post")),
        guard("first.py", says(updatedToolOutput={"stdout": "first"})),
        guard("second.py", says(updatedToolOutput={"stdout": "second"}))])
    check("updatedToolOutput comes from the first guard that sets it, beside the context",
          rc == 0 and new == {"stdout": "first"} and "ctx-post" in out, out[:300])
    rc, new, out = merged("PostToolUse", [
        guard("short.py", says(updatedToolOutput="dropped")),
        guard("block.py", {"decision": "block", "reason": "post-two"})])
    check("a PostToolUse refusal drops another guard's updatedToolOutput",
          rc == 2 and new is None, out[:300])
    rc, new, out = merged("PreToolUse", [guard("pre.py", says("PreToolUse", updatedToolOutput="no"))])
    check("updatedToolOutput is carried on PostToolUse only", rc == 0 and new is None, out[:300])


def check_codex():
    print("[4] the Codex translation")
    adapter = load("codex_hook_adapter", os.path.join(HERE, "codex-hook-adapter.py"))

    def said(**specific):
        return json.dumps({"hookSpecificOutput": dict({"hookEventName": "PostToolUse"}, **specific)})

    doc = json.loads(adapter.codex_output(said(updatedToolOutput="short text")))
    check("a string replacement becomes decision block with the text as its reason",
          doc == {"decision": "block", "reason": "short text"}, doc)
    doc = json.loads(adapter.codex_output(said(updatedToolOutput={"stdout": "from stdout"},
                                               additionalContext="ctx")))
    check("an object's stdout is the reason, and other context is kept",
          doc.get("decision") == "block" and doc.get("reason") == "from stdout"
          and doc["hookSpecificOutput"] == {"hookEventName": "PostToolUse",
                                            "additionalContext": "ctx"}, doc)
    out = adapter.codex_output(said(updatedToolOutput="x" * (adapter.REPLACEMENT_MAX_BYTES + 1)))
    check("a replacement Codex would cut is dropped, and so is the field it refuses",
          "updatedToolOutput" not in out and "block" not in out, out[:200])
    plain = said(additionalContext="only context")
    check("output without a replacement passes unchanged", adapter.codex_output(plain) == plain)
    check("output that is not JSON passes unchanged",
          adapter.codex_output("plain words") == "plain words" and adapter.codex_output("") == "")


OPENCODE_DRIVER = r"""
const [plugin_path, cwd, cases_path] = process.argv.slice(2)
const { readFileSync } = await import("node:fs")
const plugin = (await import(plugin_path)).default
const hooks = {}
const ctx = {
  location: { directory: cwd },
  tool: { hook: async (n, f) => { hooks[n] = f } },
  session: { hook: async () => {}, synthetic: async () => {} },
}
await plugin.setup(ctx)
const out = []
for (const event of JSON.parse(readFileSync(cases_path, "utf8"))) {
  await hooks["execute.after"](event)
  out.push(event.result ?? null)
}
console.log(JSON.stringify(out))
process.exit(0)
"""


def check_opencode():
    print("[5] the opencode plugin's execute.after")
    node = shutil.which("node")
    if not node:
        print("  node not found; nothing run")
        return
    hm = load("harness_materialize", os.path.join(HERE, "harness-materialize.py"))
    empty = json.dumps([])
    text = hm.OPENCODE_GUARD_PLUGIN.format(
        shell=empty, edit=empty, claude_shell=empty, claude_edit=empty,
        approval=json.dumps(""), drift=json.dumps(""),
        compress=json.dumps(os.path.abspath(HOOK)), brief=json.dumps("rules"),
        launcher=json.dumps(None), python=json.dumps(sys.executable), dispatcher=json.dumps(""))
    home = os.path.join(TMP, "home-opencode")
    cwd = os.path.join(TMP, "oc-project")
    for folder in (home, cwd):
        os.makedirs(folder)
    log = build_log()
    file_part = {"type": "file", "uri": "data:image/png;base64,aGk=", "mime": "image/png"}

    def event(tool, content, **result):
        return {"tool": tool, "sessionID": "ses_oc1", "id": "call_1", "input": {"command": "make"},
                "status": "completed", "result": dict({"content": content}, **result)}

    cases = [
        event("bash", [{"type": "text", "text": log}, file_part], output=log),
        event("read", [{"type": "text", "text": log}]),
        event("bash", [{"type": "text", "text": "ok"}]),
        dict(event("bash", [{"type": "text", "text": log}]), status="error"),
    ]
    paths = {name: os.path.join(TMP, name) for name in ("plugin.mjs", "driver.mjs", "cases.json")}
    for name, body in (("plugin.mjs", text), ("driver.mjs", OPENCODE_DRIVER),
                       ("cases.json", json.dumps(cases))):
        with open(paths[name], "w", encoding="utf-8") as fh:
            fh.write(body)
    run = subprocess.run([node, paths["driver.mjs"], paths["plugin.mjs"], cwd, paths["cases.json"]],
                         capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, HOME=home, XDG_STATE_HOME=os.path.join(home, "state")))
    try:
        got = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        check("the plugin loads and the driver finishes", False, (run.stderr or run.stdout)[-500:])
        return
    shell = got[0]
    check("a shell result's text is replaced, and its file part is kept after it",
          len(shell["content"]) == 2 and shell["content"][0]["text"].startswith(toc.MARK)
          and shell["content"][1] == file_part, shell)
    check("`output`, which a nested call reads, gets the same text",
          shell["output"] == shell["content"][0]["text"], shell.get("output", "")[:200])
    check("a file read is left alone", got[1]["content"][0]["text"] == log)
    check("a small result is left alone", got[2]["content"] == [{"type": "text", "text": "ok"}], got[2])
    check("a call that did not complete is left alone", got[3]["content"][0]["text"] == log)


def check_pi():
    print("[6] the generated pi extension's tool_result")
    if not shutil.which("node"):
        print("  node not found; nothing run")
        return
    
    
    pi = load("test_pi_hooks_extension", os.path.join(HERE, "test-pi-hooks-extension.py"))
    folder = pi.tmpdir()
    image = {"type": "image", "data": "aGk=", "mimeType": "image/png"}

    def result_of(guard_body, content):
        guard = pi.write_guard(folder, "guard-%d.py" % len(os.listdir(folder)), guard_body)
        registry = pi.registry_file(folder, {"PostToolUse": [("Bash", guard)]})
        step = {"event": "tool_result",
                "payload": {"toolName": "bash", "toolCallId": "c1", "input": {"command": "ls"},
                            "isError": False, "content": content}}
        return pi.run_extension(pi.render_fixture(), [step], str(folder),
                                registry=registry)["results"][0]

    try:
        res = result_of("import json, sys\nresp = json.load(sys.stdin)['tool_response']\n"
                        "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse', "
                        "'updatedToolOutput': dict(resp, stdout='SHORT-MARK')}}))\n",
                        [{"type": "text", "text": "long-one"}, image,
                         {"type": "text", "text": "long-two"}])
        check("a replacement stands for every text part, and the image is kept after it",
              res is not None and res.get("content") == [{"type": "text", "text": "SHORT-MARK"},
                                                         image], res)
        res = result_of("", [{"type": "text", "text": "out"}])
        check("a PostToolUse that says nothing leaves the result alone", res is None, res)
    except Exception as exc:  
        check("the pi extension renders and runs under node", False, "%s: %s" % (type(exc).__name__, exc))


def main():
    try:
        check_rules()
        check_hook()
        check_dispatch()
        check_codex()
        check_opencode()
        check_pi()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
