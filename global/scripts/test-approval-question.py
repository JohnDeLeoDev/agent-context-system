#!/usr/bin/env python3
'Battery for approval-question: user approves a guarded action by answering a question.\n\nRun: python3 ~/.claude/scripts/test-approval-question.py'
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")


def _pick(store_rel, home_rel):
    path = os.path.join(STORE, "global", store_rel)
    return path if os.path.exists(path) else os.path.expanduser(home_rel)


HOOK = os.environ.get("APPROVAL_HOOK") or _pick("hooks/approval-question.py",
                                                "~/.claude/hooks/approval-question.py")
HOOKS = _pick("hooks", "~/.claude/hooks")
SCRIPTS = _pick("scripts", "~/.claude/scripts")
LOCK_TOOL = os.path.join(SCRIPTS, "test-lock.py")
GUARD_GIT = os.path.join(HOOKS, "guard-git-write.py")
OUTSIDE_GUARD = os.path.join(HOOKS, "block-write-outside-home.py")
LOCKED_EDIT = os.path.join(HOOKS, "block-locked-test-edit.py")



FIXTURES = os.path.expanduser("~/.cache/hook-test-fixtures")
os.makedirs(FIXTURES, exist_ok=True)
TMP = os.path.realpath(tempfile.mkdtemp(prefix="approval-question-", dir=FIXTURES))
STATE_HOME = os.path.join(TMP, "state")
STATE = os.path.join(STATE_HOME, "agent-context")
REPO = os.path.join(TMP, "repo")
LOCKED = os.path.join(REPO, "tests", "test_a.py")
OUTSIDE = os.path.join(os.path.realpath("/tmp"), "approval-question-%d" % os.getpid())
TOKEN = os.path.join(STATE, "git-write-consent")
GRANTS = os.path.join(STATE, "write-outside-home-consent")
UNLOCK_LOG = os.path.join(STATE, "test-lock-consent.log")
SESSION, TOOL_USE = "sess-approval", "toolu_approval"

TEXT = {
    "git-write": "Allow one git commit, push or merge in {t} within the next {m} minutes?",
    "write-outside-home": "Allow agent writes under {t} for the next {m} minutes?",
    "test-unlock": "Unlock the locked test {t}?",
}
ORDINARY: Dict[str, Any] = {"question": "Which color?", "header": "Color", "multiSelect": False,
            "options": [{"label": "Red", "description": "r"},
                        {"label": "Blue", "description": "b"}]}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:700].replace("\n", "\n        ")
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


def env(**extra):
    e = dict(os.environ)
    e.pop("TEST_LOCK_STATE_DIR", None)
    e.update({"XDG_STATE_HOME": STATE_HOME, "TEST_LOCK_TOOL": LOCK_TOOL,
              "APPROVAL_SCRIPTS_DIR": SCRIPTS})
    e.update(extra)
    return e


def call(path, payload, **extra):
    argv = ["bash", path] if path.endswith(".sh") else [sys.executable, path]
    return subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                          timeout=120, env=env(**extra), cwd=TMP)


def denies(proc):
    if proc.returncode == 2:
        return True
    try:
        doc = json.loads(proc.stdout or "{}")
    except ValueError:
        return False
    out = doc.get("hookSpecificOutput") or {}
    return str(out.get("permissionDecision") or "").lower() == "deny"


def context(proc):
    try:
        doc = json.loads(proc.stdout or "{}")
    except ValueError:
        return proc.stdout or ""
    out = doc.get("hookSpecificOutput") or {}
    return str(out.get("additionalContext") or doc.get("systemMessage") or "")


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def git(*args):
    subprocess.run(["git", "-C", REPO, *args], check=True, capture_output=True, text=True)


def setup_repo():
    os.makedirs(REPO)
    subprocess.run(["git", "init", "-q", "-b", "main", REPO], check=True, capture_output=True)
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    
    key = os.path.join(REPO, ".git", "fixture-signing-key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    os.chmod(key, 0o600)  
    for k, v in (("gpg.format", "ssh"), ("gpg.ssh.program", "ssh-keygen"),
                 ("user.signingkey", key), ("commit.gpgsign", "true")):
        git("config", k, v)
    write(os.path.join(REPO, "README"), "fixture\n")
    git("add", "README")
    git("commit", "-q", "-m", "init")
    write(LOCKED, "def test_a():\n    assert 0\n")
    write(os.path.join(REPO, "app.py"), "x = 1\n")
    git("add", "app.py", "tests/test_a.py")


def reset():
    shutil.rmtree(STATE_HOME, ignore_errors=True)
    os.makedirs(STATE, exist_ok=True)
    subprocess.run([sys.executable, LOCK_TOOL, "lock", LOCKED], check=True,
                   capture_output=True, text=True, env=env())


def approval(kind, target, minutes, reason="needed for the task") -> Dict[str, Any]:
    return {"question": TEXT.get(kind, TEXT["git-write"]).format(t=target, m=minutes)
            + " [approval:%s:%s:%d]" % (kind, target, minutes),
            "header": "Approval", "multiSelect": False,
            "options": [{"label": "Approve", "description": reason},
                        {"label": "Deny", "description": "Refuse it"}]}


def pre(questions, **extra_input):
    tool_input = {"questions": questions}
    tool_input.update(extra_input)
    return {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
            "tool_input": tool_input, "tool_use_id": TOOL_USE, "session_id": SESSION,
            "transcript_path": "", "cwd": TMP}


def post(questions, given, response=True, transcript="", **extra_input):
    tool_input = {"questions": questions}
    tool_input.update(extra_input)
    tool_response = ({"questions": questions, "answers": given, "annotations": {}}
                     if response else {})
    return {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
            "tool_input": tool_input, "tool_response": tool_response,
            "tool_use_id": TOOL_USE, "session_id": SESSION,
            "transcript_path": transcript, "cwd": TMP}


def ask(payload, **extra):
    "PreToolUse on the call's own input, then PostToolUse, in the harness's order."
    before = dict(payload, hook_event_name="PreToolUse")
    before.pop("tool_response", None)
    call(HOOK, before, **extra)
    return call(HOOK, payload, **extra)


GIT_PAYLOAD = {"tool_name": "Bash", "tool_input": {"command": "git commit -m change"},
               "cwd": REPO, "session_id": SESSION}
OUTSIDE_PAYLOAD = {"tool_name": "Write", "cwd": TMP, "session_id": SESSION,
                   "tool_input": {"file_path": os.path.join(OUTSIDE, "f.txt"),
                                  "content": "x\n"}}
LOCKED_PAYLOAD = {"tool_name": "Write", "cwd": REPO, "session_id": SESSION,
                  "tool_input": {"file_path": LOCKED,
                                 "content": "def test_a():\n    pass\n"}}




BAD = [
    ("a refusal telling the agent user unlocks in his terminal",
     "If the test itself is wrong, stop and tell user which assertion is wrong and why.\n"
     "He unlocks it in his own terminal:\n\n"
     "    python3 ~/.claude/scripts/test-lock-consent.py tests/test_a.py\n"),
    ("a refusal telling the agent to ask them to run it",
     "they can authorize it -- ASK THEM to run:\n"
     "  python3 ~/.claude/scripts/git-write-consent.py          # 10 min, single use, logged\n"),
    ("a bootstrap rule where user approves by running a command",
     "and every other path outside `$HOME` are refused. user\n  approves a directory by "
     "running\n  `python3 ~/.claude/scripts/write-outside-home-consent.py <dir> [minutes]`.\n"),
    ("a skill step where only user unlocks with a command",
     "- Only user unlocks, with `python3 ~/.claude/scripts/test-lock-consent.py <file>`. Never\n"),
    ("a Stop hook that lets a consent command through as user's step",
     'ONLY_JOHN = re.compile(\n    r"consent\\.sh|\\blog\\s*in\\b|\\blogin\\b",\n    re.I,\n)\n'),
]
GOOD = [
    ("a pointer to the structured question",
     "Ask through AskUserQuestion with the Approval header; approval-question runs "
     "test-lock-consent.py when he picks Approve.\n"),
    ("a consent script's own usage line",
     "#   python3 ~/.claude/scripts/git-write-consent.py 30           # 30 min, any repo\n"),
    ("history that names a consent script",
     "That misdiagnosis cost the user two needless runs of\n# `git-write-consent.py`. "
     "One fetch first, and the identical push goes through.\n"),
]


def main():
    setup_repo()
    print("hook under test: %s" % HOOK)

    print("\n[1] git-write approved")
    reset()
    q = approval("git-write", REPO, 30)
    check("baseline: guard-git-write refuses the commit with no grant",
          denies(call(GUARD_GIT, GIT_PAYLOAD)))
    start = time.time()
    p = ask(post([q], {q["question"]: "Approve"}))
    token = read(TOKEN)
    fields = dict(line.split("=", 1) for line in token.splitlines() if "=" in line)
    try:
        expires = int(fields.get("expires") or 0)
    except ValueError:
        expires = 0
    check("the grant writes the git-write token", token, p.stdout + p.stderr)
    check("the token expires 30 minutes out", abs(expires - (start + 1800)) < 120, token)
    check("the token is scoped to the repo", fields.get("scope") == REPO, token)
    check("the token records when it was created", fields.get("created"), token)
    check("guard-git-write now allows one commit", not denies(call(GUARD_GIT, GIT_PAYLOAD)))
    check("the next commit is refused again (single use)",
          denies(call(GUARD_GIT, GIT_PAYLOAD)))
    log = read(TOKEN + ".log")
    check("[4] the git-write log names the question, session and tool use",
          all(s in log for s in ("APPROVED-BY-QUESTION", SESSION, TOOL_USE, REPO)), log)
    ctx = context(p)
    check("[5] the agent is told the grant is live and until when",
          "granted" in ctx.lower() and "until" in ctx.lower(), ctx)

    print("\n[2] write-outside-home approved")
    reset()
    q = approval("write-outside-home", OUTSIDE, 45)
    check("baseline: block-write-outside-home refuses the write with no grant",
          denies(call(OUTSIDE_GUARD, OUTSIDE_PAYLOAD)))
    p = ask(post([q], {q["question"]: "Approve"}))
    check("the grant adds a line for the directory", "\t%s\t" % OUTSIDE in read(GRANTS),
          read(GRANTS) + p.stdout + p.stderr)
    check("block-write-outside-home now allows the write",
          not denies(call(OUTSIDE_GUARD, OUTSIDE_PAYLOAD)))
    log = read(GRANTS + ".log")
    check("[4] the write-outside-home log names the question, session and tool use",
          all(s in log for s in ("APPROVED-BY-QUESTION", SESSION, TOOL_USE, OUTSIDE)), log)
    ctx = context(p)
    check("[5] the agent is told the grant is live and until when",
          "granted" in ctx.lower() and "until" in ctx.lower(), ctx)

    print("\n[3] test-unlock approved")
    reset()
    q = approval("test-unlock", LOCKED, 0)
    check("baseline: block-locked-test-edit refuses an edit to the locked test",
          denies(call(LOCKED_EDIT, LOCKED_PAYLOAD)))
    p = ask(post([q], {q["question"]: "Approve"}))
    status = subprocess.run([sys.executable, LOCK_TOOL, "status", REPO],
                            capture_output=True, text=True, env=env())
    check("the grant removes the lock", "No locked tests" in status.stdout,
          status.stdout + status.stderr + p.stdout + p.stderr)
    check("block-locked-test-edit now allows the edit",
          not denies(call(LOCKED_EDIT, LOCKED_PAYLOAD)))
    log = read(UNLOCK_LOG)
    check("[4] the unlock log records the unlock and names the question",
          all(s in log for s in ("UNLOCK", "APPROVED-BY-QUESTION", SESSION, TOOL_USE)), log)
    check("[5] the agent is told the grant is live", "granted" in context(p).lower(),
          context(p))

    print("\n[6] answers filled in by the agent are refused")
    reset()
    qa = approval("git-write", REPO, 30)
    check("an ordinary question carrying answers is refused",
          denies(call(HOOK, pre([ORDINARY], answers={ORDINARY["question"]: "Red"}))))
    check("an approval question carrying answers is refused",
          denies(call(HOOK, pre([qa], answers={qa["question"]: "Approve"}))))
    check("a question carrying annotations is refused",
          denies(call(HOOK, pre([ORDINARY], annotations={}))))
    p = call(HOOK, pre([ORDINARY]))
    check("[15] an ordinary question passes silently",
          p.returncode == 0 and not p.stdout.strip() and not p.stderr.strip(),
          p.stdout + p.stderr)

    print("\n[7] only the exact approval shape is let through")
    good = approval("git-write", REPO, 30)

    def variant(name, question):
        proc = call(HOOK, pre([question]))
        check(name + " is refused", denies(proc), proc.stdout + proc.stderr)
        return proc

    variant("an unknown kind",
            dict(good, question=good["question"].replace("[approval:git-write:",
                                                         "[approval:deploy:")))
    p = variant("visible text that differs from the tag",
                dict(good, question="Allow a quick commit? [approval:git-write:%s:30]" % REPO))
    check("the refusal shows the exact question to ask",
          TEXT["git-write"].format(t=REPO, m=30) in p.stderr, p.stderr)
    variant("options other than Approve and Deny",
            dict(good, options=[{"label": "Yes", "description": "y"},
                                {"label": "No", "description": "n"}]))
    variant("a third option",
            dict(good, options=good["options"] + [{"label": "Later", "description": "l"}]))
    variant("multiSelect", dict(good, multiSelect=True))
    variant("a different header", dict(good, header="Commit?"))
    variant("git-write for 0 minutes", approval("git-write", REPO, 0))
    variant("git-write for 121 minutes", approval("git-write", REPO, 121))
    variant("write-outside-home for 241 minutes", approval("write-outside-home", OUTSIDE, 241))
    variant("test-unlock with minutes", approval("test-unlock", LOCKED, 5))
    variant("a relative path", approval("git-write", "repo", 30))
    variant("a path that is not its own realpath",
            approval("git-write", REPO + "/../repo", 30))
    variant("git-write for a subdirectory of the repo",
            approval("git-write", os.path.join(REPO, "tests"), 30))
    variant("git-write for a directory that is not a repo",
            approval("git-write", STATE_HOME, 30))
    variant("write-outside-home for a directory inside home",
            approval("write-outside-home", TMP, 30))
    variant("write-outside-home for /", approval("write-outside-home", "/", 30))
    variant("test-unlock for a file that is not locked",
            approval("test-unlock", os.path.join(REPO, "app.py"), 0))
    for kind, target, minutes in (("git-write", REPO, 30),
                                  ("write-outside-home", OUTSIDE, 45),
                                  ("test-unlock", LOCKED, 0)):
        proc = call(HOOK, pre([approval(kind, target, minutes)]))
        check("the exact %s question is allowed" % kind, proc.returncode == 0,
              proc.stdout + proc.stderr)

    print("\n[8] nothing is granted without an Approve answer from the dialog")
    q = approval("git-write", REPO, 30)

    def no_grant(name, payload, words=(), **extra):
        reset()
        proc = ask(payload, **extra)
        check(name + ": no token", not os.path.exists(TOKEN), read(TOKEN) + proc.stdout + proc.stderr)
        for word in words:
            check(name + ": the agent is told (%s)" % word, word in context(proc).lower(),
                  context(proc) + proc.stderr)

    no_grant("Deny", post([q], {q["question"]: "Deny"}), ("denied",))
    no_grant("typed Other text 'approve'", post([q], {q["question"]: "approve"}),
             ("not granted",))
    no_grant("typed 'yes'", post([q], {q["question"]: "yes"}))
    no_grant("'Approve (Recommended)'", post([q], {q["question"]: "Approve (Recommended)"}))
    no_grant("no answer", post([q], {}), ("not granted", "no answer"))
    no_grant("answers the model filled in (PreToolUse refuses, so no mark)",
             post([q], {q["question"]: "Approve"}, answers={q["question"]: "Approve"}),
             ("not granted",))
    odd = dict(q, options=[{"label": "Approve", "description": "a"},
                           {"label": "Later", "description": "l"}])
    no_grant("a non-standard approval question", post([odd], {odd["question"]: "Approve"}),
             ("not granted",))
    no_grant("an answer keyed to a different question",
             post([q], {"Some other question?": "Approve"}))

    print("\n[8b] a grant needs the single-use mark PreToolUse leaves")
    reset()
    p = call(HOOK, post([q], {q["question"]: "Approve"}))
    check("PostToolUse with no PreToolUse mark grants nothing", not os.path.exists(TOKEN),
          read(TOKEN) + p.stdout + p.stderr)
    check("the agent is told PreToolUse never let the call through",
          "never let" in context(p), context(p))
    reset()
    harness = post([q], None, response=False, answers={q["question"]: "Approve"})
    call(HOOK, pre([q]))
    p = call(HOOK, harness)
    check("the harness order grants: the answer in tool_input, none in tool_response",
          os.path.exists(TOKEN), p.stdout + p.stderr)
    if os.path.exists(TOKEN):
        os.remove(TOKEN)
    p = call(HOOK, harness)
    check("replaying the same PostToolUse grants nothing", not os.path.exists(TOKEN),
          read(TOKEN) + p.stdout + p.stderr)

    print("\n[9] the transcript is the fallback record of the answer")

    def transcript(answers, tool_use_id):
        rec = {"type": "user",
               "message": {"role": "user",
                           "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                        "content": "The user answered."}]},
               "toolUseResult": {"questions": [q], "answers": answers, "annotations": {}}}
        path = os.path.join(TMP, "t-%s.jsonl" % tool_use_id)
        write(path, json.dumps({"type": "user", "message": {"role": "user", "content": "go"}})
              + "\n" + json.dumps(rec) + "\n")
        return path

    reset()
    p = ask(post([q], None, response=False,
                        transcript=transcript({q["question"]: "Approve"}, TOOL_USE)))
    check("an Approve answer found in the transcript grants", os.path.exists(TOKEN),
          p.stdout + p.stderr)
    no_grant("no answer in the payload or the transcript",
             post([q], None, response=False, transcript=os.path.join(TMP, "absent.jsonl")),
             ("not granted", "no answer"))
    no_grant("an Approve in the transcript for another tool use",
             post([q], None, response=False,
                  transcript=transcript({q["question"]: "Approve"}, "toolu_other")))

    print("\n[10] a failing grant reaches the agent")
    reset()
    stub = os.path.join(TMP, "stub-scripts")
    write(os.path.join(stub, "git-write-consent.py"), "import sys; sys.exit('boom')")
    p = ask(post([q], {q["question"]: "Approve"}), APPROVAL_SCRIPTS_DIR=stub)
    check("no token", not os.path.exists(TOKEN), read(TOKEN))
    check("the agent is told the grant FAILED, with the script's error",
          "FAILED" in context(p) and "boom" in context(p), context(p) + p.stderr)

    print("\n[11] several approvals in one call are judged one by one")
    reset()
    qg, qo = approval("git-write", REPO, 30), approval("write-outside-home", OUTSIDE, 45)
    p = ask(post([qg, qo], {qg["question"]: "Approve", qo["question"]: "Deny"}))
    check("the approved one is granted", os.path.exists(TOKEN), p.stdout + p.stderr)
    check("the denied one is not", OUTSIDE not in read(GRANTS), read(GRANTS))

    print("\n[12] an ordinary question beside an approval is left alone")
    reset()
    p = call(HOOK, pre([ORDINARY, qo]))
    check("PreToolUse allows the pair", p.returncode == 0, p.stdout + p.stderr)
    p = ask(post([ORDINARY, qo], {ORDINARY["question"]: "Red", qo["question"]: "Approve"}))
    check("the approval is granted", "\t%s\t" % OUTSIDE in read(GRANTS),
          read(GRANTS) + p.stdout + p.stderr)
    check("the ordinary answer is not reported on", "Which color" not in context(p), context(p))
    p = ask(post([ORDINARY], {ORDINARY["question"]: "Red"}))
    check("[15] an ordinary question's result passes silently",
          p.returncode == 0 and not p.stdout.strip(), p.stdout + p.stderr)

    print("\n[17/18] no text tells an agent to hand user a consent command")
    spec = importlib.util.spec_from_file_location(
        "invariant_check", os.path.join(SCRIPTS, "invariant-check.py"))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load invariant-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    detect = getattr(mod, "_typed_consent_instruction", None)
    check("invariant-check has the detector", callable(detect))
    if callable(detect):
        for name, text in BAD:
            check("flags " + name, detect("/sample", text), text)
        for name, text in GOOD:
            check("passes " + name, not detect("/sample", text), text)
    inv = [i for i in getattr(mod, "REGISTRY", []) if i.id == "no-typed-consent-instructions"]
    check("the invariant is registered", inv)
    if inv:
        found, checked = inv[0].run()
        check("every site passes (%d checked)" % checked, not found,
              "\n".join("%s: %s" % (f["site"], f["detail"]) for f in found))


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)
