#!/usr/bin/env python3
"e1  A mark PreToolUse left for call A must not vouch for call C, even with the same\n      session and question text (measured: C granted with no dialog of its own).\n  e2  With tool_use_ids missing on both events, the session plus text key still pairs a\n      call with its own PreToolUse, so a harness that omits ids is not locked out.\n  e3  The mark directory approval-question writes is one block-consent-self-grant\n      guards. The review measured a full self-grant while the two names differed.\n  e4  Grants in one call share a time budget that ends before the dispatcher's\n      registered PostToolUse timeout, so a slow consent script is reported, not killed\n      mid-grant with no word to the agent.\n\nPrivate XDG_STATE_HOME throughout; nothing real is granted.\nRun: python3 ~/.claude/scripts/test-approval-question-edges.py"
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")


def _pick(store_rel, home_rel):
    path = os.path.join(STORE, "global", store_rel)
    return path if os.path.exists(path) else os.path.expanduser(home_rel)


HOOK = os.environ.get("APPROVAL_HOOK") or _pick("hooks/approval-question.py",
                                                "~/.claude/hooks/approval-question.py")
SELF_GRANT = os.environ.get("SELF_GRANT_HOOK") or _pick(
    "hooks/block-consent-self-grant.py", "~/.claude/hooks/block-consent-self-grant.py")
SETTINGS_SYNC = _pick("scripts/home-settings-sync.py", "~/.claude/scripts/home-settings-sync.py")

FIXTURES = os.path.expanduser("~/.cache/hook-test-fixtures")
os.makedirs(FIXTURES, exist_ok=True)
TMP = os.path.realpath(tempfile.mkdtemp(prefix="approval-edges-", dir=FIXTURES))
STATE_HOME = os.path.join(TMP, "state")
GRANTS = os.path.join(STATE_HOME, "agent-context", "write-outside-home-consent")
OUTSIDE = os.path.join(os.path.realpath("/tmp"), "approval-edges-%d" % os.getpid())
OTHER = os.path.join(os.path.realpath("/tmp"), "approval-edges-other-%d" % os.getpid())

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:600]
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def question(target, minutes=5):
    return {"question": "Allow agent writes under %s for the next %d minutes? "
                        "[approval:write-outside-home:%s:%d]" % (target, minutes, target, minutes),
            "header": "Approval", "multiSelect": False,
            "options": [{"label": "Approve", "description": "edge case"},
                        {"label": "Deny", "description": "Refuse it"}]}


def call(event, questions, tool_use_id, answers=None, **env_extra):
    tool_input = {"questions": questions}
    if answers is not None:
        tool_input["answers"] = answers
    payload = {"hook_event_name": event, "tool_name": "AskUserQuestion",
               "tool_input": tool_input, "session_id": "sess-edges", "cwd": TMP,
               "transcript_path": ""}
    if event == "PostToolUse":
        payload["tool_response"] = {}
    if tool_use_id is not None:
        payload["tool_use_id"] = tool_use_id
    env = dict(os.environ, XDG_STATE_HOME=STATE_HOME)
    env.update(env_extra)
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=120, env=env)


def context(proc):
    try:
        doc = json.loads(proc.stdout or "{}")
    except ValueError:
        return proc.stdout or ""
    return str((doc.get("hookSpecificOutput") or {}).get("additionalContext") or "")


def reset():
    shutil.rmtree(STATE_HOME, ignore_errors=True)
    os.makedirs(os.path.join(STATE_HOME, "agent-context"), exist_ok=True)


def main():
    print("hook under test: %s" % HOOK)
    q = question(OUTSIDE)
    approve = {q["question"]: "Approve"}

    print("\n[e1] a mark vouches only for its own tool use")
    reset()
    call("PreToolUse", [q], "toolu_A")
    p = call("PostToolUse", [q], "toolu_C", approve)
    check("PostToolUse for a call that never passed PreToolUse grants nothing",
          OUTSIDE not in read(GRANTS), read(GRANTS) + p.stdout + p.stderr)
    check("the agent is told it was never let through", "never let" in context(p), context(p))
    p = call("PostToolUse", [q], "toolu_A", approve)
    check("the call that left the mark still grants", OUTSIDE in read(GRANTS),
          p.stdout + p.stderr)

    print("\n[e2] without tool_use_ids the session and text key pairs a call")
    reset()
    call("PreToolUse", [q], None)
    p = call("PostToolUse", [q], None, approve)
    check("a PreToolUse and PostToolUse with no ids grant", OUTSIDE in read(GRANTS),
          p.stdout + p.stderr)

    print("\n[e3] the mark directory is guarded against agent writes")
    source = read(HOOK)
    found = re.search(r'RECORDS\s*=\s*os\.path\.join\(STATE,\s*"([^"]+)"\)', source)
    marks = found.group(1) if found else ""
    check("approval-question names its mark directory", marks, "no RECORDS assignment found")
    if marks:
        payload = {"tool_name": "Bash", "tool_input": {
            "command": "touch ~/.local/state/agent-context/%s/id-forged" % marks}}
        proc = subprocess.run([sys.executable, SELF_GRANT], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=30)
        check("block-consent-self-grant refuses writing a mark in %s" % marks,
              proc.returncode == 2, proc.stdout + proc.stderr)

    print("\n[e4] grants share a budget inside the registered timeout")
    reset()
    stub = os.path.join(TMP, "slow-scripts")
    os.makedirs(stub)
    with open(os.path.join(stub, "write-outside-home-consent.py"), "w", encoding="utf-8") as fh:
        fh.write("import time; time.sleep(2)")
    q2 = question(OTHER)
    pair = [q, q2]
    call("PreToolUse", pair, "toolu_slow")
    start = time.time()
    p = call("PostToolUse", pair, "toolu_slow", {q["question"]: "Approve", q2["question"]: "Approve"},
             APPROVAL_SCRIPTS_DIR=stub, APPROVAL_GRANT_BUDGET="3")
    took = time.time() - start
    ctx = context(p)
    check("the hook stops granting when its budget runs out (%.1fs)" % took, took < 6, ctx)
    check("the approval left without time is reported as FAILED",
          "FAILED" in ctx and ("time" in ctx.lower()), ctx + p.stderr)
    budget = re.search(r'APPROVAL_GRANT_BUDGET"\)\s*or\s*(\d+)', source)
    sync = read(SETTINGS_SYNC)
    timeouts = [int(t) for t in re.findall(
        r'"approval-question\.py"\),\s*\{"timeout":\s*(\d+)\}', sync)]
    check("the default budget is named in the hook", budget, "no APPROVAL_GRANT_BUDGET default")
    if budget and timeouts:
        check("the registered PostToolUse timeout (%ds) leaves 10s over the budget (%ss)"
              % (max(timeouts), budget.group(1)),
              max(timeouts) >= int(budget.group(1)) + 10, str(timeouts))


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)
