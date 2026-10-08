#!/usr/bin/env python3
'Battery for consolidation Phase 3a: hook-dispatch edges found by the adversarial review.\n\n  [1] A background child that an earlier Python guard left holding fd 1 cannot write into\n      a later guard\'s captured output: the later guard\'s refusal still refuses.\n  [2] A payload that is not valid UTF-8 still reaches the guards, the bad bytes replaced,\n      instead of crashing the dispatcher before anything runs.\n  [3] sys.stdin has a .buffer, so a guard that reads bytes from stdin works in-process as\n      it does as a subprocess.\n  [4] hook-registry expands one event\'s guards once, even when settings.json names the\n      dispatcher for that event twice.\n  [5] A group whose matcher is not a valid regex fails open loudly: a systemMessage and a\n      hook-dispatch health record name the matcher, and the event\'s other groups run.\n  [6] hook-firing-report counts a [guard] prefix only where the dispatcher puts one:\n      right after the harness\'s "hook-dispatch.py <Event>]: " frame, or after a blank\n      line inside that refusal. Prose quoting "[block-deploy.sh]" is not a firing.\n  [7] continue:false and its stopReason reach the merged output.\n\nNot reproduced, so not here: the review\'s guess that the one-shot timeout alarm could land\ninside the dispatcher\'s own restore code.\n\nReuses the fixture helpers of test-hook-dispatch.py (locked) and removes its temp dir.\n\nUsage: test-hook-dispatch-edges.py'

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE = load("test_hook_dispatch_base", os.path.join(HERE, "test-hook-dispatch.py"))
Scene, tool_call, lifecycle = BASE.Scene, BASE.tool_call, BASE.lifecycle
say_py, pre_output, read, put, clip = BASE.say_py, BASE.pre_output, BASE.read, BASE.put, BASE.clip
TMP = BASE.TMP
STORE = os.environ.get("AGENT_CONTEXT_STORE") or BASE.STORE

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def check_orphan_child():
    print("[1] an orphaned child cannot write into a later guard's output")
    s = Scene("edge-orphan")
    deny = pre_output(permissionDecision="deny", permissionDecisionReason="late-deny")
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [
        s.py("spawner.py", "import subprocess\n"
                           "subprocess.Popen(['/bin/sh', '-c', 'sleep 0.4; echo NOISE-FROM-ORPHAN'])\n"),
        s.py("refuser.py", "import time\nprint(json.dumps(%r))\nsys.stdout.flush()\ntime.sleep(0.9)\n"
             % (deny,)),
    ]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"))
    time.sleep(0.2)
    check("a later guard's refusal survives an earlier guard's child writing to fd 1",
          r.rc == 2 and "[refuser.py] late-deny" in r.err and s.ran() == ["spawner.py", "refuser.py"],
          "%s; ran %s" % (r, s.ran()))


def check_bytes_payload():
    print("[2] a payload that is not valid UTF-8")
    s = Scene("edge-bytes")
    out = s.path("tool.txt")
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [
        s.py("reads.py", "data = json.load(sys.stdin)\nopen(%r, 'w').write(data.get('tool_name', ''))\n"
             % out)]}]})
    raw = b'{"tool_name": "Bash", "tool_input": {"command": "echo \xff\xfe"}}'
    env = dict(os.environ, HOME=s.home, HOOK_DISPATCH_REGISTRY=s.registry)
    proc = subprocess.run([sys.executable, BASE.DISPATCH, "PreToolUse"], input=raw,
                          capture_output=True, env=env, cwd=s.dir, timeout=60)
    check("still reaches the guards, with the bad bytes replaced",
          proc.returncode == 0 and s.ran() == ["reads.py"] and read(out) == "Bash",
          "rc=%s stderr=%r; ran %s" % (proc.returncode, clip(proc.stderr.decode("utf-8", "replace")),
                                       s.ran()))


def check_stdin_buffer():
    print("[3] sys.stdin.buffer inside an in-process guard")
    s = Scene("edge-buffer")
    out = s.path("tool.txt")
    s.write({"PreToolUse": [{"matcher": "Bash", "hooks": [
        s.py("bytes.py", "data = json.load(sys.stdin.buffer)\nopen(%r, 'w').write(data['tool_name'])\n"
             % out)]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"))
    check("a guard can read its payload from sys.stdin.buffer",
          r.rc == 0 and read(out) == "Bash" and not r.field("systemMessage"), str(r))


def check_double_dispatcher():
    print("[4] hook-registry with the dispatcher named twice for one event")
    reg = load("hook_registry_edges", os.path.join(HERE, "hook-registry.py"))
    guard = {"type": "command", "command": "/u/.claude/hooks/only-guard.sh"}
    registry = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [guard]}]}}
    dispatcher = {"type": "command", "command": "/py /u/.claude/scripts/hook-dispatch.py PreToolUse"}
    hooks = {"PreToolUse": [{"hooks": [dispatcher]}, {"hooks": [dict(dispatcher)]}]}
    expanded = reg.expand(hooks, registry)
    commands = [h.get("command") for g in expanded.get("PreToolUse", []) for h in g.get("hooks", [])]
    check("expands the event's guards once", commands == [guard["command"]], str(commands))


def check_invalid_regex():
    print("[5] a matcher that is not a valid regex")
    s = Scene("edge-regex")
    s.write({"PreToolUse": [{"matcher": "Bash(", "hooks": [s.py("broken.py")]},
                            {"matcher": "Bash", "hooks": [s.py("fine.py")]}]})
    r = s.run("PreToolUse", tool_call("PreToolUse", "Bash"))
    record = read(s.health("hook-dispatch"))
    check("fails open loudly: a systemMessage from hook-dispatch names the matcher",
          r.rc == 0 and "hook-dispatch" in (r.field("systemMessage") or "")
          and "Bash(" in (r.field("systemMessage") or ""), str(r))
    check("and is recorded as hook-dispatch, naming the matcher",
          "Bash(" in record and "PreToolUse" in record, clip(record))
    check("and the event's other groups still run", s.ran() == ["fine.py"], "ran %s" % s.ran())


def check_firing_pattern():
    print("[6] hook-firing-report counts a [guard] prefix only inside the dispatcher's frame")
    home = os.path.join(TMP, "edge-firing")
    put(os.path.join(home, ".claude", "settings.json"), json.dumps({"hooks": {}}))
    frame = "/py /u/.claude/scripts/hook-dispatch.py PreToolUse"
    records = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "The refusal said: [block-deploy.sh] Blocked: deploy"}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "PreToolUse:Bash hook error: [%s]: [guard-git-write.sh] first\n\n"
                        "[block-git-stash.sh] second" % frame}]}},
    ]
    put(os.path.join(home, ".claude", "projects", "fixture", "session.jsonl"),
        "".join(json.dumps(rec) + "\n" for rec in records))
    proc = subprocess.run([sys.executable, os.path.join(HERE, "hook-firing-report.py")],
                          capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, HOME=home, AGENT_CONTEXT_STORE=STORE))
    report = proc.stdout

    def fired(name):
        return re.search(r"^\s+%s\s+\d+" % re.escape(name), report, re.M) is not None

    check("prose quoting [block-deploy.sh] is not counted as a firing",
          report.strip() and not fired("block-deploy"), clip(report, 600))
    check("both reasons inside one dispatcher refusal are counted",
          fired("guard-git-write") and fired("block-git-stash"), clip(report, 600))


def check_continue_false():
    print("[7] continue:false through the merge")
    s = Scene("edge-continue")
    s.write({"Stop": [{"hooks": [
        say_py(s, "halt.py", {"continue": False, "stopReason": "halt-now"}),
        say_py(s, "note.py", {"systemMessage": "still-said"})]}]})
    r = s.run("Stop", lifecycle("Stop"))
    check("continue:false and its stopReason reach the merged output",
          r.rc == 0 and r.field("continue") is False and "halt-now" in (r.field("stopReason") or "")
          and "still-said" in (r.field("systemMessage") or ""), str(r))


def main():
    try:
        for step in (check_orphan_child, check_bytes_payload, check_stdin_buffer,
                     check_double_dispatcher, check_invalid_regex, check_firing_pattern,
                     check_continue_false):
            try:
                step()
            except Exception as exc:  
                check("%s ran to completion" % step.__name__, False,
                      "%s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (passed, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
