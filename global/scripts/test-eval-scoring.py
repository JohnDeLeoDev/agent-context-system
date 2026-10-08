#!/usr/bin/env python3
"test-eval-scoring: what eval-run's scorers and judge are allowed to see and accept."
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load spec for %s" % name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ev = _load("eval_run", "eval-run.py")
cases = {c["id"]: c for c in _load("eval_cases", "eval-cases.py").CASES}
failures = []
cleanup = []


def check(name, ok, detail: object = ""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           (" :: " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def write(root, rel, text):
    with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
        fh.write(text)


def fixture():
    root = tempfile.mkdtemp(prefix="eval-scoring-test-")
    cleanup.append(root)
    ev.build_fixture({"fixture": {"labels.py": "MAX = 10\n"}}, root)
    return root


EXACT = {"type": "files_touched_exactly", "paths": ["labels.py"], "also_allowed": ["test_*.py"]}
OUTSIDE = {"type": "no_files_touched_outside", "paths": ["labels.py"],
           "also_allowed": ["test_*.py"]}


root = fixture()
write(root, "labels.py", "MAX = 12\n")
write(root, "test_labels.py", "def test_x():\n    pass\n")
ok, d = ev.check(EXACT, root, "")
check("files_touched_exactly accepts an added test file named by also_allowed", ok, d)
ok, d = ev.check(OUTSIDE, root, "")
check("no_files_touched_outside accepts it too", ok, d)
ok, d = ev.check({"type": "files_touched_exactly", "paths": ["labels.py"]}, root, "")
check("without also_allowed, an added test file still fails files_touched_exactly", not ok, d)
write(root, "other.py", "X = 1\n")
ok, d = ev.check(OUTSIDE, root, "")
check("a non-test stray file still fails no_files_touched_outside", not ok, d)
ok, d = ev.check(EXACT, root, "")
check("a non-test stray file still fails files_touched_exactly", not ok, d)



root3 = fixture()
write(root3, "labels.py", "MAX = 12\n")
os.makedirs(os.path.join(root3, "tests"))
write(root3, os.path.join("tests", "test_labels.py"), "def test_x():\n    pass\n")
ok, d = ev.check(EXACT, root3, "")
check("a test file in a new subdirectory is matched by also_allowed", ok, d)
ok, d = ev.check(OUTSIDE, root3, "")
check("...for no_files_touched_outside too", ok, d)
os.makedirs(os.path.join(root3, "lib"))
write(root3, os.path.join("lib", "helper.py"), "X = 1\n")
ok, d = ev.check(OUTSIDE, root3, "")
check("a non-test file in a new subdirectory is still a stray", not ok, d)



diff = ev.diff_of(root3)
check("the judge's diff includes a test file added in a new subdirectory",
      "tests/test_labels.py" in diff and "def test_x" in diff, diff[-300:])



root4 = tempfile.mkdtemp(prefix="eval-scoring-test-")
cleanup.append(root4)
ev.build_fixture({"fixture": {"labels.py": "MAX = 10\n", "helper.py": "X = 1\n"}}, root4)
ev._run(["git", "mv", "helper.py", "lib_helper.py"], cwd=root4)
got = ev.touched(root4)
check("a staged rename lists both paths and no arrow",
      "helper.py" in got and "lib_helper.py" in got and not any(" -> " in p for p in got), got)

root2 = fixture()
write(root2, "test_labels.py", "def test_x():\n    pass\n")
ok, d = ev.check(EXACT, root2, "")
check("touching only a test file does not satisfy files_touched_exactly", not ok, d)

for cid in ("preserve-unrelated-behavior", "respect-explicit-constraint",
            "no-silent-truncation"):
    scoped = [c for c in cases[cid]["checks"]
              if c["type"] in ("files_touched_exactly", "no_files_touched_outside")]
    check("%s allows test_*.py on every scope check" % cid,
          scoped and all("test_*.py" in (c.get("also_allowed") or []) for c in scoped), scoped)
strict = [c for c in cases["scope-creep"]["checks"]
          if c["type"] in ("files_touched_exactly", "no_files_touched_outside")]
check("scope-creep stays strict (no also_allowed)",
      strict and not any(c.get("also_allowed") for c in strict), strict)



home = tempfile.mkdtemp(prefix="eval-scoring-home-")
cleanup.append(home)
sid = "0000aaaa-test-eval-scoring"
proj = os.path.join(home, ".claude", "projects", "-tmp-fixture")
os.makedirs(proj)
records = [
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "cat server.sh"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
         "content": "PreToolUse:Bash hook error: [/Users/x/.claude/hooks/"
                    "block-shell-file-read.py]: Blocked: use the Read tool instead"}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "Bash",
         "input": {"command": "./server.sh & pid=$!; trap 'kill $pid' EXIT; curl -s localhost:8000"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "is_error": False, "content": "ok"}]}},
]
with open(os.path.join(proj, sid + ".jsonl"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(json.dumps(r) for r in records) + "\n")
old_home = os.environ.get("HOME")
os.environ["HOME"] = home
try:
    tools = ev.tools_used("/tmp/fixture", sid)
finally:
    if old_home is not None:
        os.environ["HOME"] = old_home
calls = list(getattr(tools, "calls", None) or [])
executed = [c.get("command", "") for c in calls if c["name"] == "Bash" and not c["blocked"]]
check("tools_used records the executed Bash command text", any("trap " in c for c in executed),
      calls)
prompt = ev.judge_prompt("PASS only if the server is stopped by a trap.", "", "Done.",
                         tools=tools) if hasattr(ev, "judge_prompt") else ""
check("the judge prompt shows the executed command", "trap 'kill $pid' EXIT" in prompt,
      prompt[:300])
check("the judge prompt leaves out the blocked command", "cat server.sh" not in prompt,
      prompt[:300])

tc = ev.ToolCounts({"Bash": 1})
tc.calls = [{"name": "Bash", "blocked": False, "command": "echo " + "x" * 5000}]
prompt = ev.judge_prompt("r", "", "m", tools=tc) if hasattr(ev, "judge_prompt") else "x" * 6000
check("a very long command is truncated in the prompt", len(prompt) < 4000, len(prompt))

tc.calls = [{"name": "Bash", "blocked": False, "command": "echo %02d %s" % (i, "y" * 500)}
            for i in range(20)]
prompt = ev.judge_prompt("r", "", "m", tools=tc) if hasattr(ev, "judge_prompt") else ""
block = prompt.split("=== BASH COMMANDS THAT RAN")[-1].split("=== THE AGENT'S DIFF")[0]
check("cutting the command block says so", "[truncated]" in block.strip().splitlines()[-1],
      block.strip().splitlines()[-1][-80:] if block.strip() else "")



seen = []
real_run = ev._run
ev._run = lambda cmd, **kw: (seen.append(cmd), subprocess.CompletedProcess(cmd, 0, "PASS - ok", ""))[1]
try:
    ok, _ = ev.judge("r", "", "m")
finally:
    ev._run = real_run
argv = seen[0] if seen else []
check("the judge gets no built-in tools", "--tools" in argv
      and argv[argv.index("--tools") + 1] == "", argv)
check("the judge loads no MCP servers", "--strict-mcp-config" in argv
      and "--mcp-config" not in argv, argv)
check("the judge runs no hooks", any('"disableAllHooks": true' in a for a in argv), argv)
check("the judge's verdict still parses", ok is True)

for d in cleanup:
    shutil.rmtree(d, ignore_errors=True)
print("\n  %d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
