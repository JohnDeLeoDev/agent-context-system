#!/usr/bin/env python3

'approval-question: user approves a guarded action by answering a structured question.\n\nuser types no commands to approve: the agent asks a structured question. Three guards\nopen only on a grant user gives: guard-git-write (a single-use git-write token),\nblock-write-outside-home (a directory grant), and block-locked-test-edit with\nlocked-test-drift-gate (a removed test lock). The agent asks, and this hook runs the\nconsent script for that grant when user picks Approve, so the guards keep reading the\nfiles they always read.\n\nTwo events, one file (registered on both, like lsp-failure-tripwire).\n  PreToolUse(AskUserQuestion) refuses, exit 2:\n    - any call whose input already carries `answers` or `annotations`. The dialog fills\n      those from user\'s choice; a model that sets them is answering for him.\n    - an approval question (header "Approval", or a question holding "[approval:") that\n      is not exactly the shape render() produces, so the target and minutes user reads\n      are the ones that get granted.\n  PostToolUse(AskUserQuestion) grants each approval question whose recorded answer is\n  exactly "Approve", but only for a call PreToolUse let through. PreToolUse leaves a\n  mark under $XDG_STATE_HOME/agent-context/approval-marks/ (by tool_use_id, and by\n  session plus question text) and PostToolUse consumes it. By PostToolUse the harness\n  has written user\'s choice into tool_input.answers, so `answers` there cannot tell a\n  forged call from a real one. The mark can, because a forged call never passes\n  PreToolUse, and a dispatcher that skipped PreToolUse leaves no mark. PostToolUse also\n  re-checks the shape. The answer comes from tool_response, then tool_input, then the\n  harness-written toolUseResult for this tool_use_id in the transcript. Every outcome\n  reaches the agent as additionalContext.\n\nWhat this is not. One click in a dialog an agent opens is not cryptographic authority,\nand the consent scripts\' own caveat still holds. The shape keeps the click honest: the\nvisible text is rendered from the tag, git grants stay single use, and every grant is\nlogged with its session and tool use.\n\nThis file\'s two events are Claude Code\'s. Codex and opencode ask through their own\nstructured question tools, and codex-test-unlock.py and opencode-approval.py call\nvalidate, take_record and grant here, so every harness gives the same grant. pi and\nCopilot CLI have no structured question tool and no approval route.\n\nFail mode. PreToolUse fails open on an unreadable payload, since PostToolUse still\nrefuses to grant. PostToolUse fails closed: any error grants nothing and says so.\nPython 3.8-safe: the Synology nodes run hooks on 3.8.'
import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

HOME = hp.home()
SCRIPTS = os.environ.get("APPROVAL_SCRIPTS_DIR") or hp.scripts_dir(HOME)
LOCK_TOOL = os.environ.get("TEST_LOCK_TOOL") or os.path.join(SCRIPTS, "test-lock.py")
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"),
                     "agent-context")

RECORDS = os.path.join(STATE, "approval-marks")

RECORD_TTL = 12 * 3600



try:
    GRANT_BUDGET = float(os.environ.get("APPROVAL_GRANT_BUDGET") or 75)
except ValueError:
    GRANT_BUDGET = 75.0
HEADER = "Approval"
LABELS = ["Approve", "Deny"]
FILLED_KEYS = ("answers", "annotations")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

KINDS = {
    "git-write": ("Allow one git commit, push or merge in {t} within the next {m} minutes?",
                  1, 120, "git-write-consent.py", "git-write-consent.log"),
    "write-outside-home": ("Allow agent writes under {t} for the next {m} minutes?",
                           1, 240, "write-outside-home-consent.py",
                           "write-outside-home-consent.log"),
    "test-unlock": ("Unlock the locked test {t}?",
                    0, 0, "test-lock-consent.py", "test-lock-consent.log"),
}
TAG = re.compile(r"\[approval:([a-z-]+):(/[^\]]*):(\d+)\]\s*\Z")

HOW_TO_ASK = """Ask with one AskUserQuestion question per approval: header "Approval", multiSelect
false, options "Approve" (its description says what this is for) and "Deny",
and one of these question texts, with nothing added or changed:

  git-write, 1-120 minutes, the repository's top level:
    Allow one git commit, push or merge in <repo> within the next <minutes> minutes? [approval:git-write:<repo>:<minutes>]
  write-outside-home, 1-240 minutes, a resolved directory outside home:
    Allow agent writes under <dir> for the next <minutes> minutes? [approval:write-outside-home:<dir>:<minutes>]
  test-unlock, 0 minutes, a locked test file:
    Unlock the locked test <file>? [approval:test-unlock:<file>:0]

Paths are absolute and already resolved (no symlinks, no ..). When user picks Approve,
this hook grants it and tells you. Never fill in answers yourself."""

EVENT = None


def render(kind, target, minutes):
    return KINDS[kind][0].format(t=target, m=minutes) + " [approval:%s:%s:%d]" % (
        kind, target, minutes)


def is_approval(question):
    return isinstance(question, dict) and (
        question.get("header") == HEADER or "[approval:" in str(question.get("question") or ""))


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def target_problem(kind, target):
    'Why this target cannot be granted, or None.'
    if kind == "git-write":
        try:
            out = subprocess.run(["git", "-C", target, "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            return "git could not check %s: %s" % (target, exc)
        top = out.stdout.strip() if out.returncode == 0 else ""
        if not top:
            return "%s is not inside a git repository" % target
        if os.path.realpath(top) != target:
            return "name the repository's top level, %s" % os.path.realpath(top)
    elif kind == "write-outside-home":
        home = os.path.realpath(HOME)
        if target == "/":
            return "approve a specific directory, not /"
        if target == home or target.startswith(home + os.sep):
            return "%s is inside the home directory, which needs no approval" % target
    elif kind == "test-unlock":
        
        
        
        try:
            locked = load_module(LOCK_TOOL, "test_lock").is_locked(target)
        except Exception as exc:  
            return "could not ask the store whether %s is locked: %s" % (target, exc)
        if not locked:
            return "%s is not a locked test" % target
    return None


def validate(question):
    '((kind, target, minutes) or None, None when the question is exact, else why not).'
    text = str(question.get("question") or "")
    match = TAG.search(text)
    if not match:
        return None, "the question does not end in an [approval:<kind>:<absolute path>:<minutes>] tag"
    kind, target, minutes = match.group(1), match.group(2), int(match.group(3))
    if kind not in KINDS:
        return None, "%r is not an approval kind (%s)" % (kind, ", ".join(sorted(KINDS)))
    low, high = KINDS[kind][1], KINDS[kind][2]
    if not low <= minutes <= high:
        return None, "%s takes %d to %d minutes, not %d" % (kind, low, high, minutes)
    spec = (kind, target, minutes)
    if os.path.realpath(target) != target:
        return spec, "the path must be absolute and resolved: %s" % os.path.realpath(target)
    problem = target_problem(kind, target)
    if problem:
        return spec, problem
    if text != render(kind, target, minutes):
        return spec, "the question text differs from the text rendered from its tag"
    if question.get("header") != HEADER:
        return spec, 'the header must be "%s"' % HEADER
    if question.get("multiSelect"):
        return spec, "multiSelect must be false"
    options = question.get("options")
    labels = [o.get("label") if isinstance(o, dict) else None
              for o in (options if isinstance(options, list) else [])]
    if labels != LABELS:
        return spec, 'the options must be "Approve" and "Deny", in that order'
    return spec, None


def record_keys(payload, questions):
    "The names PreToolUse's mark goes under: the tool_use_id, and session plus text."
    texts = [str(q.get("question") or "") for q in questions]
    digest = hashlib.sha256(json.dumps([str(payload.get("session_id") or ""), texts])
                            .encode("utf-8")).hexdigest()[:32]
    keys = ["q-" + digest]
    tool_use_id = payload.get("tool_use_id")
    if isinstance(tool_use_id, str) and SAFE_ID.match(tool_use_id):
        keys.insert(0, "id-" + tool_use_id)
    return keys


def leave_record(payload, questions):
    'Mark a call PreToolUse let through. A failure leaves no mark, so nothing grants.'
    try:
        os.makedirs(RECORDS, exist_ok=True)
        now = time.time()
        for name in os.listdir(RECORDS):
            old = os.path.join(RECORDS, name)
            try:
                if now - os.path.getmtime(old) > RECORD_TTL:
                    os.remove(old)
            except OSError:
                pass
        body = json.dumps({"questions": [str(q.get("question") or "") for q in questions],
                           "at": now})
        for key in record_keys(payload, questions):
            with open(os.path.join(RECORDS, key), "w", encoding="utf-8") as fh:
                fh.write(body)
    except OSError as exc:
        sys.stderr.write("approval-question: could not mark this call, so it cannot grant: %s\n"
                         % exc)


def consume(key, expected=None):
    'True when a fresh mark contains exactly these questions. Removes it either way.'
    path = os.path.join(RECORDS, key)
    try:
        with open(path, encoding="utf-8") as fh:
            marked = json.load(fh)
        fresh = time.time() - os.path.getmtime(path) <= RECORD_TTL
        matches = isinstance(marked, dict) and marked.get("questions") == expected
    except (OSError, ValueError):
        fresh = matches = False
    try:
        os.remove(path)
    except OSError:
        pass
    return fresh and matches


def take_record(payload, questions):
    'True when PreToolUse let this call through.\n\n    With a tool_use_id, only the id mark counts. The session plus text mark is shared by\n    every identical question in a session, so honoring it here would let a pending mark\n    from one call vouch for another that never passed PreToolUse. The text mark is the\n    fallback for a payload with no id.'
    keys = record_keys(payload, questions)
    expected = [str(q.get("question") or "") for q in questions]
    if keys[0].startswith("id-"):
        found = consume(keys[0], expected)
        if found:
            consume(keys[1], expected)
        return found
    return consume(keys[0], expected)


def refuse(text):
    sys.stderr.write(text.rstrip("\n") + "\n")
    return 2


def pre(payload):
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    filled = [k for k in FILLED_KEYS if k in tool_input]
    if filled:
        return refuse(
            "Blocked by approval-question: this AskUserQuestion call already carries %s.\n\n"
            "The dialog fills those in from user's choice. A call that sets them answers for "
            "him, so it is refused whatever the question is. Send the questions alone."
            % " and ".join(filled))
    approvals = []
    for question in tool_input.get("questions") or []:
        if not is_approval(question):
            continue
        spec, reason = validate(question)
        if not reason:
            approvals.append(question)
            continue
        lines = ["Blocked by approval-question: this approval question is not in the exact "
                 "approval shape, so an Approve on it could not grant anything.",
                 "  Problem: %s" % reason, ""]
        if spec:
            expected = {"questions": [{
                "question": render(*spec), "header": HEADER, "multiSelect": False,
                "options": [{"label": "Approve", "description": "<what this is for>"},
                            {"label": "Deny", "description": "Refuse it"}]}]}
            lines += ["The same approval, in the exact shape:",
                      json.dumps(expected, indent=2), ""]
        lines.append(HOW_TO_ASK)
        return refuse("\n".join(lines))
    if approvals:
        leave_record(payload, approvals)
    return 0


def transcript_answers(path, tool_use_id):
    'The harness-written answers for this tool use, from the transcript, or None.'
    if not path or not isinstance(tool_use_id, str) or not tool_use_id \
            or not os.path.isfile(path):
        return None
    found = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if tool_use_id not in line or "toolUseResult" not in line:
                continue
            try:
                rec = json.loads(line, strict=False)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "user":
                continue
            content = (rec.get("message") or {}).get("content")
            ids = [b.get("tool_use_id") for b in content
                   if isinstance(b, dict) and b.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            result = rec.get("toolUseResult")
            if (tool_use_id in ids and isinstance(result, dict)
                    and isinstance(result.get("answers"), dict)):
                found = result["answers"]
    return found


def recorded_answers(payload):
    response = payload.get("tool_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            response = None
    if isinstance(response, dict) and isinstance(response.get("answers"), dict) \
            and response["answers"]:
        return response["answers"]
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("answers"), dict) \
            and tool_input["answers"]:
        return tool_input["answers"]
    return transcript_answers(payload.get("transcript_path"), payload.get("tool_use_id"))


def log_grant(kind, target, payload):
    "Append the approval to the kind's consent log. Returns an error string or ''."
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = "%s\tAPPROVED-BY-QUESTION\t%s\t%s\tsession=%s\ttool_use=%s\n" % (
        stamp, kind, target, payload.get("session_id") or "?",
        payload.get("tool_use_id") or "?")
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, KINDS[kind][4]), "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        return " Writing the approval log FAILED: %s." % exc
    return ""


def grant(spec, payload, deadline):
    kind, target, minutes = spec
    script = KINDS[kind][3]
    remaining = deadline - time.monotonic()
    if remaining < 1:
        return ("approval-question: user approved, but the grant FAILED: no time left in this "
                "hook's %d s budget. Ask again for %s %s." % (GRANT_BUDGET, kind, target))
    args = {"git-write": [str(minutes), target],
            "write-outside-home": [target, str(minutes)],
            "test-unlock": [target]}[kind]
    try:
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)] + args,
                              capture_output=True, text=True, timeout=min(60.0, remaining))
    except (OSError, subprocess.SubprocessError) as exc:
        return "approval-question: user approved, but the grant FAILED: %s could not run: %s" % (
            script, exc)
    output = " ".join((proc.stdout + proc.stderr).split())
    if proc.returncode != 0 or (kind == "test-unlock" and "Unlocked:" not in proc.stdout):
        return "approval-question: user approved, but the grant FAILED (%s exit %d): %s" % (
            script, proc.returncode, output)
    logged = log_grant(kind, target, payload)
    if kind == "test-unlock":
        return "approval-question: granted. user approved unlocking %s; the lock is removed.%s" % (
            target, logged)
    until = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).strftime("%H:%M:%S")
    what = ("one git commit, push or merge in %s (single use)" % target
            if kind == "git-write" else "agent writes under %s" % target)
    return "approval-question: granted. user approved %s, valid until %s.%s" % (
        what, until, logged)


def emit(text):
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": text}}, sys.stdout)
    sys.stdout.write("\n")


def post(payload):
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    questions = [q for q in tool_input.get("questions") or [] if is_approval(q)]
    if not questions:
        return 0
    deadline = time.monotonic() + GRANT_BUDGET
    seen = take_record(payload, questions)
    answers = recorded_answers(payload) if seen else None
    notes = []
    for question in questions:
        text = str(question.get("question") or "")
        if not seen:
            notes.append("approval-question: not granted, PreToolUse never let this call "
                         "through, so its answer cannot be trusted: %s" % text)
            continue
        spec, reason = validate(question)
        if reason:
            notes.append("approval-question: not granted, the question was not in the exact "
                         "approval shape (%s): %s" % (reason, text))
            continue
        answer = (answers or {}).get(text)
        if answer is None:
            notes.append("approval-question: not granted, no answer was recorded for: %s" % text)
        elif answer == "Deny":
            notes.append("approval-question: denied by user: %s Do not ask again for the same "
                         "thing without a new reason." % text)
        elif answer != "Approve":
            notes.append("approval-question: not granted, the answer was %r, not the Approve "
                         "option: %s If user wrote what he wants instead, follow that." % (
                             answer, text))
        else:
            notes.append(grant(spec, payload, deadline))
    emit("\n".join(notes))
    return 0


def main():
    global EVENT
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "AskUserQuestion":
        return 0
    EVENT = payload.get("hook_event_name")
    if EVENT == "PreToolUse":
        return pre(payload)
    if EVENT == "PostToolUse":
        return post(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("approval-question: hook failed: %r\n" % (exc,))
        if EVENT == "PostToolUse":
            emit("approval-question: the hook failed, so nothing was granted: %r" % (exc,))
        sys.exit(0)
