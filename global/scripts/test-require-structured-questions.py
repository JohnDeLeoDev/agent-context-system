#!/usr/bin/env python3
'Test battery for the require-structured-questions Stop hook.\n\nThe must-ALLOW cases outnumber the must-BLOCK ones deliberately. A Stop hook that\nblocks a legitimate turn end is worse than one that misses a case: the first gets\ndisabled, and then nothing is enforced at all.\n\nRun: python3 ~/.claude/scripts/test-require-structured-questions.py'
import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.expanduser("~/.claude/hooks/require-structured-questions.py")
if not os.path.exists(HOOK):
    HOOK = os.path.expanduser(
        "~/.agent-context/global/hooks/require-structured-questions.py")

BLOCK, ALLOW = 2, 0
results = []


def transcript(*, asked_after_user=False, asked_before_user=False):
    'Write a JSONL transcript fixture; return its path (or "" for none).'
    rows = []
    if asked_before_user:
        rows += [
            {"type": "user", "message": {"role": "user",
                                         "content": [{"type": "text", "text": "earlier"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "AskUserQuestion", "input": {}}]}},
        ]
    rows.append({"type": "user", "message": {"role": "user",
                                            "content": [{"type": "text", "text": "do the thing"}]}})
    
    
    rows.append({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}})
    if asked_after_user:
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "AskUserQuestion", "input": {}}]}})
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for r in rows:
        fh.write(json.dumps(r) + "\n")
    fh.close()
    return fh.name


def check(name, expect, message, *, tpath="", stop_active=False, raw=None):
    payload = json.dumps({
        "last_assistant_message": message,
        "transcript_path": tpath,
        "stop_hook_active": stop_active,
        "hook_event_name": "Stop",
    }) if raw is None else raw
    proc = subprocess.run([sys.executable, HOOK], input=payload,
                          capture_output=True, text=True, timeout=30)
    ok = proc.returncode == expect
    results.append((ok, name, expect, proc.returncode))
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}"
          + ("" if ok else f"   (expected {expect}, got {proc.returncode})"))


t_none = transcript()
t_asked = transcript(asked_after_user=True)
t_stale = transcript(asked_before_user=True)

print("must BLOCK — a decision handed over in prose:")
check("want me to", BLOCK, "All set. Want me to add it to the ignore list?", tpath=t_none)
check("should I", BLOCK, "The rename is ready. Should I run it now?", tpath=t_none)
check("would you like", BLOCK, "Done. Would you like me to also push?", tpath=t_none)
check("do you want", BLOCK, "Do you want the strict variant instead?", tpath=t_none)
check("let me know which", BLOCK, "Two paths exist. Let me know which you prefer.", tpath=t_none)
check("your call", BLOCK, "Both are defensible — your call.", tpath=t_none)
check("deferred decision (2026-08-10)", BLOCK,
      "## Wrap-up\nShipped milestone 1.\n\nOpen decision, for when you're next at it: "
      "libgit2 versus reading the repo over SFTP.", tpath=t_none)
check("options heading", BLOCK,
      "Here is where it stands.\n\nOptions:\n- keep polling\n- switch to a webhook", tpath=t_none)
check("AskUserQuestion used BEFORE this user turn", BLOCK,
      "Want me to land it?", tpath=t_stale)

print("\nmust ALLOW — everything that is not a prose decision:")
check("AskUserQuestion already called", ALLOW, "Want me to land it?", tpath=t_asked)
check("stop_hook_active (never block twice)", ALLOW,
      "Want me to land it?", tpath=t_none, stop_active=True)
check("plain completion", ALLOW, "Landed on main at aeab35f0. Worktree removed.", tpath=t_none)
check("question mark, no decision", ALLOW,
      "Why did the gate fail? Because GIT_CONFIG_PARAMETERS leaked into the subprocess.",
      tpath=t_none)
check("rhetorical question", ALLOW,
      "Is that safe? It is: the guard exempts the store entirely.", tpath=t_none)
check("decision phrase inside a fence", ALLOW,
      "Example prompt:\n```\nWant me to add it to the ignore list?\n```\nThat is the shape to avoid.",
      tpath=t_none)
check("decision phrase in a code span", ALLOW,
      "The old copy said `Should I proceed?` and was replaced.", tpath=t_none)
check("decision phrase in a quoted line", ALLOW,
      "user's correction:\n> Want me to do X? — do not end a message like that\nNoted.",
      tpath=t_none)
check("'options' as a normal word", ALLOW,
      "The tool exposes several options for the timeout and I picked 30s.", tpath=t_none)
check("empty message", ALLOW, "", tpath=t_none)
check("missing transcript path", ALLOW, "All done, nothing outstanding.", tpath="")
check("malformed stdin fails OPEN", ALLOW, "", raw="{not json")

for p in (t_none, t_asked, t_stale):
    try:
        os.unlink(p)
    except OSError:
        pass

bad = [r for r in results if not r[0]]
print(f"\n{len(results) - len(bad)}/{len(results)} passed")
sys.exit(1 if bad else 0)
