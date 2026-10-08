#!/usr/bin/env python3
'opencode-approval: user approves a guarded action through opencode\'s question tool.\n\nopencode has no AskUserQuestion. Its `question` tool takes the same structured\nquestions (question, header, options, multiple), and the generated guard plugin\n(agent-context-guards.js, from harness-materialize.py) calls this hook around it:\n\n  PreToolUse   from the plugin\'s tool `execute.before`. An approval question (header\n               "Approval", or a question holding "[approval:") must be the only question\n               in its call and in the exact shape approval-question renders, or the call\n               is refused, exit 2. A call let through leaves approval-question\'s mark\n               under its call id.\n  PostToolUse  from the plugin\'s tool `execute.after`. Grants when the answers opencode\n               built from user\'s form reply are one answer, "Approve", and the\n               mark for this call id is there. The grant is approval-question\'s own\n               (same consent script, same record, same expiry, same log). Whatever\n               happened is printed for the plugin to append to the tool result.\n\nWHERE THE ANSWER COMES FROM. The plugin copies `tool_response.answers` from the\n`result.output` opencode hands `execute.after`; the model writes the tool input and\nnever that field. The model\'s own arguments (`tool_input`) are read only for the\nquestion text, and a grant needs the mark PreToolUse left for the same call id.\n\nWHAT THIS IS NOT. opencode 2.x hosts every session on a local server whose form reply\nroute takes a password kept in a file the agent\'s shell can read. block-consent-self-grant\nrefuses a shell command that calls that route, runs this hook or writes a mark, the\nsame friction the Claude Code path rests on. It is friction and an audit trail, never\ncryptographic authority.\n\nFail mode. PreToolUse fails open on an unreadable payload, since PostToolUse still\nrefuses to grant without a mark. PostToolUse fails closed: any error grants nothing and\nsays so. Python 3.8-safe.'
import hashlib
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APPROVAL = os.path.join(HERE, "approval-question.py")
TOOL = "question"

HOW_TO_ASK = """Ask with opencode's question tool: one call holding only this question, header
"Approval", multiple false, options "Approve" (its description says what this is for)
and "Deny", and the question text as given, with nothing added or changed."""


def approval_module():
    spec = importlib.util.spec_from_file_location("approval_question", APPROVAL)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % APPROVAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def as_claude(question):
    "opencode's question, in the shape approval-question validates."
    return {"question": question.get("question"), "header": question.get("header"),
            "multiSelect": bool(question.get("multiple")), "options": question.get("options")}


def pending(approval, payload):
    "The mark's key: this session and call. A call id the mark file cannot be named\n    after is hashed, the same way on both events."
    call = payload.get("tool_use_id")
    if not isinstance(call, str) or not call:
        return None
    if not approval.SAFE_ID.match(call):
        call = "h" + hashlib.sha256(call.encode("utf-8")).hexdigest()[:40]
    return {"session_id": str(payload.get("session_id") or ""), "tool_use_id": call}


def questions_of(payload):
    tool_input = payload.get("tool_input")
    found = tool_input.get("questions") if isinstance(tool_input, dict) else None
    return [q for q in found if isinstance(q, dict)] if isinstance(found, list) else []


def refuse(text):
    sys.stderr.write(text.rstrip("\n") + "\n")
    return 2


def pre(approval, payload):
    questions = questions_of(payload)
    hits = [q for q in questions if approval.is_approval(as_claude(q))]
    if not hits:
        return 0
    if len(questions) != 1:
        return refuse(
            "Blocked by opencode-approval: an approval question goes in a question call of "
            "its own, and this call holds %d questions. An Approve here could not grant "
            "anything.\n\n%s" % (len(questions), HOW_TO_ASK))
    question = as_claude(hits[0])
    spec, reason = approval.validate(question)
    if reason:
        lines = ["Blocked by opencode-approval: this approval question is not in the exact "
                 "approval shape, so an Approve on it could not grant anything.",
                 "  Problem: %s" % reason, ""]
        if spec:
            expected = {"questions": [{
                "question": approval.render(*spec), "header": approval.HEADER,
                "multiple": False,
                "options": [{"label": "Approve", "description": "<what this is for>"},
                            {"label": "Deny", "description": "Refuse it"}]}]}
            lines += ["The same approval, in the exact shape:",
                      json.dumps(expected, indent=2), ""]
        lines.append(HOW_TO_ASK)
        return refuse("\n".join(lines))
    key = pending(approval, payload)
    if key is None:
        return refuse("Blocked by opencode-approval: this question call has no call id, so "
                      "its answer could not be tied to it and nothing could be granted.")
    approval.leave_record(key, [question])
    return 0


def claim(approval, key):
    "Claim this call's reply once before granting; any state error fails closed."
    digest = hashlib.sha256((key["session_id"] + "\0" + key["tool_use_id"])
                            .encode("utf-8")).hexdigest()
    path = os.path.join(approval.RECORDS, "opencode-reply-" + digest)
    try:
        os.makedirs(approval.RECORDS, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as record:
            record.write(str(time.time()))
    except OSError:
        return False
    return True


def harness_answers(payload):
    'The answers opencode built from the form reply: one list of labels per question.'
    response = payload.get("tool_response")
    answers = response.get("answers") if isinstance(response, dict) else None
    return answers if isinstance(answers, list) else None


def post(approval, payload):
    questions = questions_of(payload)
    hits = [q for q in questions if approval.is_approval(as_claude(q))]
    if not hits:
        return ""
    question = as_claude(hits[0])
    text = str(question.get("question") or "")
    key = pending(approval, payload)
    if len(questions) != 1 or key is None or not approval.take_record(key, [question]):
        return ("opencode-approval: not granted, the plugin's pre-tool check never let this "
                "call through, so its answer cannot be trusted: %s" % text)
    spec, reason = approval.validate(question)
    if reason or spec is None:
        return ("opencode-approval: not granted, the question was not in the exact approval "
                "shape (%s): %s" % (reason, text))
    answers = harness_answers(payload)
    if not answers or len(answers) != 1 or not isinstance(answers[0], list) or not answers[0]:
        return "opencode-approval: not granted, no answer was recorded for: %s" % text
    answer = answers[0]
    if answer == ["Deny"]:
        return ("opencode-approval: denied by user: %s Do not ask again for the same thing "
                "without a new reason." % text)
    if answer != ["Approve"]:
        return ("opencode-approval: not granted, the answer was %r, not the Approve option: "
                "%s If user wrote what he wants instead, follow that." % (answer, text))
    if not claim(approval, key):
        return "opencode-approval: not granted, this reply was already used once: %s" % text
    log = dict(payload, tool_use_id=key["tool_use_id"])
    return approval.grant(spec, log, time.monotonic() + approval.GRANT_BUDGET)


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != TOOL:
        return 0
    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        return pre(approval_module(), payload)
    if event == "PostToolUse":
        try:
            note = post(approval_module(), payload)
        except Exception as exc:  
            note = "opencode-approval: the hook failed, so nothing was granted: %r" % (exc,)
        if note:
            sys.stdout.write(note + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("opencode-approval: hook failed: %r\n" % (exc,))
        sys.exit(0)
