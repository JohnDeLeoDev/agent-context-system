'Grant a guarded action only from its pending Codex structured approval reply.'

import hashlib
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(__file__)
APPROVAL = os.path.join(HERE, "approval-question.py")
REPLY = re.compile(
    r"\s*<send_user_message_question_reply>\s*(\[.*\])\s*"
    r"</send_user_message_question_reply>\s*", re.DOTALL
)
CALL_ID = re.compile(r"^call_[A-Za-z0-9]+$")


def approval_module():
    spec = importlib.util.spec_from_file_location("approval_question", APPROVAL)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % APPROVAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reply_item(prompt):
    'Return one host-structured answer, never an ordinary free-text prompt.'
    match = REPLY.fullmatch(prompt)
    if not match:
        return None
    try:
        items = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    item = items[0]
    try:
        ident = json.loads(item.get("questionItemId", ""))
    except (TypeError, ValueError):
        return None
    if (not isinstance(ident, list) or len(ident) != 3
            or ident[0] != "request_user_input_async"
            or not isinstance(ident[1], str) or not CALL_ID.fullmatch(ident[1])
            or type(ident[2]) is not int or ident[2] != 0):
        return None
    if not isinstance(item.get("question"), str):
        return None
    return ident[1], item["question"], item.get("answer")


def claim_reply(approval, session_id, call_id):
    'Claim this host reply once before granting; any state error fails closed.'
    digest = hashlib.sha256((session_id + "\0" + call_id).encode("utf-8")).hexdigest()
    path = os.path.join(approval.RECORDS, "codex-reply-" + digest)
    try:
        os.makedirs(approval.RECORDS, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as record:
            record.write(str(time.time()))
    except OSError:
        return False
    return True


def emit(message):
    json.dump({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                      "additionalContext": message}}, sys.stdout)
    sys.stdout.write("\n")


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (TypeError, ValueError):
        return 0
    if (not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit"
            or not isinstance(payload.get("turn_id"), str)
            or not isinstance(payload.get("session_id"), str)):
        return 0
    item = reply_item(payload.get("prompt", ""))
    if item is None:
        return 0
    call_id, title, answer = item
    if answer != "Approve":
        return 0
    approval = approval_module()
    question = {"question": title, "header": "Approval", "multiSelect": False,
                "options": [{"label": "Approve"}, {"label": "Deny"}]}
    spec, problem = approval.validate(question)
    if problem or spec is None:
        return 0
    
    
    pending = {"session_id": payload["session_id"], "tool_use_id": call_id}
    if not approval.take_record(pending, [question]):
        return 0
    if not claim_reply(approval, payload["session_id"], call_id):
        return 0
    result = approval.grant(spec, payload, time.monotonic() + approval.GRANT_BUDGET)
    emit(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  
        emit("codex-test-unlock: approval failed; nothing was granted: %r" % (exc,))
        raise SystemExit(0)
