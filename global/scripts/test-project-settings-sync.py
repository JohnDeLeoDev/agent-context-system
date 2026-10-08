#!/usr/bin/env python3
"Tests for the shell-to-Python cutover in project-settings-sync.py (project scope).\n\nCovers REQUIRED BEHAVIOR 1-7 of the project-phase brief: wt-seed and post-edit-format wire\nthe .py command when the project has it on disk, else keep today's .sh command; wt-sweep\nstays wired exactly as today, off the presence of .agents/scripts/wt-sweep.sh on disk (the\nshim itself is not deleted -- it is the marker), but both hook sites now run the global\nwt-sweep.py directly instead of execing the shim. OWNED_MARKERS recognizes both old and new\nentries so a run replaces rather than duplicates; hand-added hooks and no-file projects are\nuntouched; a second run is a no-op.\n\nRuns the real project-settings-sync.py (~/.agent-context/global/scripts/) against fixture\nproject dirs and a fixture store, via AGENT_CONTEXT_STORE. AGENT_CONTEXT_PYTHON pins the\nrendered interpreter to sys.executable so expected command strings are host-independent."
import json
import os
import shutil
import subprocess
import sys
import tempfile

STORE = os.path.expanduser("~/.agent-context")
SCRIPT = os.path.join(STORE, "global", "scripts", "project-settings-sync.py")

INTERP = sys.executable

MATERIALIZE_CMD = ('bash -c \'%s "$HOME/.agent-context/global/scripts/project-materialize.py" '
                    '"$CLAUDE_PROJECT_DIR" || true\'' % INTERP)

WT_SWEEP_CMD = 'bash -c \'%s "$HOME/.agent-context/global/scripts/wt-sweep.py" "$CLAUDE_PROJECT_DIR" || true\'' % INTERP

SH_POST_EDIT_FORMAT_CMD = "$CLAUDE_PROJECT_DIR/.agents/hooks/post-edit-format.sh"
PY_POST_EDIT_FORMAT_CMD = '%s "$CLAUDE_PROJECT_DIR/.agents/hooks/post-edit-format.py"' % INTERP


def sh_wt_cmd(script):
    return ('bash -c \'f="$CLAUDE_PROJECT_DIR/.agents/scripts/%s"; [ -f "$f" ] && bash "$f" '
            '"$CLAUDE_PROJECT_DIR" || true\'' % script)


def py_wt_cmd(script):
    return ('bash -c \'f="$CLAUDE_PROJECT_DIR/.agents/scripts/%s"; [ -f "$f" ] && %s "$f" '
            '"$CLAUDE_PROJECT_DIR" || true\'' % (script, INTERP))


def seed_group(cmd):
    return {"matcher": "EnterWorktree", "hooks": [{"type": "command", "command": cmd, "timeout": 15}]}


def format_group(cmd):
    return {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": cmd, "timeout": 30}]}


def sweep_exit_group(cmd):
    return {"matcher": "ExitWorktree", "hooks": [{"type": "command", "command": cmd, "timeout": 15}]}


def build_expected(sweep_cmd=None, seed_group_=None, format_group_=None):
    session = [{"type": "command", "command": MATERIALIZE_CMD, "timeout": 15}]
    if sweep_cmd:
        session.append({"type": "command", "command": sweep_cmd, "timeout": 15, "async": True})
    post = []
    if format_group_:
        post.append(format_group_)
    if seed_group_:
        post.append(seed_group_)
    if sweep_cmd:
        post.append(sweep_exit_group(sweep_cmd))
    out = {"worktree": {"baseRef": "head"}, "hooks": {"SessionStart": [{"hooks": session}]}}
    if post:
        out["hooks"]["PostToolUse"] = post
    return out


tmp = tempfile.mkdtemp(prefix="pss-test-")
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


def make_project(name, files=()):
    proj = os.path.join(tmp, name)
    os.makedirs(os.path.join(proj, ".agents"), exist_ok=True)
    for rel in files:
        full = os.path.join(proj, ".agents", rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write("# fixture\n")
    return proj


def seed_settings(project, data):
    path = os.path.join(project, ".agents", "claude", "settings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def settings_of(project):
    path = os.path.join(project, ".agents", "claude", "settings.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def run(project, store):
    env = {"HOME": os.environ.get("HOME", os.path.expanduser("~")), "PATH": "/usr/bin:/bin",
           "AGENT_CONTEXT_STORE": store, "AGENT_CONTEXT_PYTHON": INTERP}
    return subprocess.run([sys.executable, SCRIPT, project], capture_output=True, text=True, env=env)


EMPTY_STORE = os.path.join(tmp, "empty-store")
os.makedirs(os.path.join(EMPTY_STORE, "projects"), exist_ok=True)

print("project-settings-sync: project-phase (shell-to-python)")


p = make_project("seed-py-only", files=["scripts/wt-seed.py"])
run(p, EMPTY_STORE)
got = settings_of(p)
want = build_expected(seed_group_=seed_group(py_wt_cmd("wt-seed.py")))
check("wt-seed.py alone wires the python EnterWorktree command", got == want, "got %r" % got)


p = make_project("seed-sh-only", files=["scripts/wt-seed.sh"])
run(p, EMPTY_STORE)
got = settings_of(p)
want = build_expected(seed_group_=seed_group(sh_wt_cmd("wt-seed.sh")))
check("wt-seed.sh alone keeps today's exact EnterWorktree command (must not change)", got == want, "got %r" % got)


p = make_project("seed-both", files=["scripts/wt-seed.py", "scripts/wt-seed.sh"])
run(p, EMPTY_STORE)
got = settings_of(p)
want = build_expected(seed_group_=seed_group(py_wt_cmd("wt-seed.py")))
check("wt-seed.py wins when both wt-seed.py and wt-seed.sh are present", got == want, "got %r" % got)


p = make_project("format-py-only", files=["hooks/post-edit-format.py"])
run(p, EMPTY_STORE)
got = settings_of(p)
want = build_expected(format_group_=format_group(PY_POST_EDIT_FORMAT_CMD))
check("post-edit-format.py alone wires the python Edit|Write command", got == want, "got %r" % got)


p = make_project("format-sh-only", files=["hooks/post-edit-format.sh"])
run(p, EMPTY_STORE)
got = settings_of(p)
want = build_expected(format_group_=format_group(SH_POST_EDIT_FORMAT_CMD))
check("post-edit-format.sh alone keeps today's exact Edit|Write command (must not change)",
      got == want, "got %r" % got)


p = make_project("sweep-absent", files=["scripts/wt-seed.py"])
run(p, EMPTY_STORE)
got = settings_of(p)
want = build_expected(seed_group_=seed_group(py_wt_cmd("wt-seed.py")))
check("wt-sweep.sh absent wires no sweep hooks", got == want, "got %r" % got)



p = make_project("full-sh", files=["scripts/wt-seed.sh", "scripts/wt-sweep.sh", "hooks/post-edit-format.sh"])
run(p, EMPTY_STORE)
got = settings_of(p)
want = build_expected(sweep_cmd=WT_SWEEP_CMD, seed_group_=seed_group(sh_wt_cmd("wt-seed.sh")),
                       format_group_=format_group(SH_POST_EDIT_FORMAT_CMD))
got_post = (got or {}).get("hooks", {}).get("PostToolUse", [])
want_post = want["hooks"]["PostToolUse"]
check("full .sh project: post-edit-format and wt-seed commands are byte-identical to today",
      len(got_post) == 3 and got_post[0] == want_post[0] and got_post[1] == want_post[1],
      "got %r" % got_post)
check("full .sh project: wt-sweep command switches to the global wt-sweep.py (intended change)",
      got == want, "got %r" % got)



p = make_project("cutover-dedup", files=["scripts/wt-seed.py"])
seed_settings(p, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": MATERIALIZE_CMD,
                                                          "timeout": 15}]}],
                            "PostToolUse": [seed_group(sh_wt_cmd("wt-seed.sh"))]}})
run(p, EMPTY_STORE)
got = settings_of(p)
got_post = (got or {}).get("hooks", {}).get("PostToolUse")
check("old .sh wt-seed entry is replaced by the new .py entry, not duplicated",
      got_post == [seed_group(py_wt_cmd("wt-seed.py"))], "got %r" % got_post)



p = make_project("sweep-dedup", files=["scripts/wt-sweep.sh"])
seed_settings(p, {"hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": MATERIALIZE_CMD, "timeout": 15},
                                 {"type": "command", "command": sh_wt_cmd("wt-sweep.sh"),
                                  "timeout": 15, "async": True}]}],
    "PostToolUse": [sweep_exit_group(sh_wt_cmd("wt-sweep.sh"))]}})
run(p, EMPTY_STORE)
got = settings_of(p)
want = build_expected(sweep_cmd=WT_SWEEP_CMD)
check("old shim-exec wt-sweep entries are replaced by the direct global command, not duplicated",
      got == want, "got %r" % got)


p = make_project("idempotent", files=["scripts/wt-seed.py", "hooks/post-edit-format.py"])
run(p, EMPTY_STORE)
path = os.path.join(p, ".agents", "claude", "settings.json")
mtime1 = os.path.getmtime(path)
proc2 = run(p, EMPTY_STORE)
mtime2 = os.path.getmtime(path)
check("a second run does not rewrite settings.json", mtime1 == mtime2,
      "mtime1=%r mtime2=%r stdout=%r" % (mtime1, mtime2, proc2.stdout))


p = make_project("hand-added", files=[])
seed_settings(p, {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": "echo hi", "timeout": 5}]}]}})
run(p, EMPTY_STORE)
got = settings_of(p)
check("a hand-added hook entry survives untouched",
      (got or {}).get("hooks", {}).get("PostToolUse") ==
      [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi", "timeout": 5}]}],
      "got %r" % got)


p = make_project("no-files", files=[])
run(p, EMPTY_STORE)
got = settings_of(p)
want = build_expected()
check("a project with none of the managed files gets only the materialize hook",
      got == want, "got %r" % got)




p = make_project("loop-guard-stale", files=[])
seed_settings(p, {"hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": MATERIALIZE_CMD, "timeout": 15}]}],
    "PostToolUse": [sweep_exit_group(sh_wt_cmd("wt-loop-guard.sh"))]}})
run(p, EMPTY_STORE)
got = settings_of(p)
check("a stale entry naming wt-loop-guard is dropped", got == build_expected(), "got %r" % got)

p = make_project("loop-guard-mixed", files=[])
seed_settings(p, {"hooks": {"PostToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi", "timeout": 5}]},
    {"matcher": "ExitWorktree", "hooks": [
        {"type": "command", "command": sh_wt_cmd("wt-loop-guard.sh"), "timeout": 15},
        {"type": "command", "command": "echo keep-me", "timeout": 5}]}]}})
run(p, EMPTY_STORE)
got = settings_of(p)
post = (got or {}).get("hooks", {}).get("PostToolUse", [])
flat = json.dumps(post)
check("the wt-loop-guard entry is dropped and the hand-added hooks beside it survive",
      "wt-loop-guard" not in flat and "echo hi" in flat and "echo keep-me" in flat, "got %r" % post)

p = make_project("loop-guard-idempotent", files=[])
seed_settings(p, {"hooks": {"PostToolUse": [sweep_exit_group(sh_wt_cmd("wt-loop-guard.sh"))]}})
run(p, EMPTY_STORE)
path = os.path.join(p, ".agents", "claude", "settings.json")
mtime1 = os.path.getmtime(path)
run(p, EMPTY_STORE)
check("a second run after dropping it does not rewrite settings.json",
      mtime1 == os.path.getmtime(path))


with open(SCRIPT, encoding="utf-8") as fh:
    source_text = fh.read()
check("the docstring states the one-cycle sh/py fallback rule", "one cycle" in source_text.lower())

shutil.rmtree(tmp, ignore_errors=True)
print("\nproject-settings-sync project-phase: %d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
