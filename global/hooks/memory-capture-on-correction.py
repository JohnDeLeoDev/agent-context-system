#!/usr/bin/env python3
"memory-capture-on-correction: Stop hook that asks the agent to save a correction.\n\nContext overhaul decision H: when user corrects an agent, prompt an `upsert_memory`.\n\nFires when this turn's last real user message reads as a correction (OPENERS_RE and\nHOLDS_RE hold the phrase list) and the turn made no memory write (`upsert_memory`,\nor `edit_body` with kind memory). Blocks once.\n\nA short reply (under 6 words) to an assistant message that asked a question or\ncalled AskUserQuestion is an answer, not a correction, and is skipped.\n\nAllows on stop_hook_active and on every failure path: an unreadable transcript,\na malformed payload or a missing user message never reads as a correction."
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import transcript_records

NAME = "memory-capture-on-correction"

SYSREM_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S | re.I)
COMMAND_RE = re.compile(r"<command-[^>]*>.*?</command-[^>]*>", re.S | re.I)
NOISE = ("<command-", "<local-command-", "<ci-monitor-event>", "Stop hook feedback:",
         "[Request interrupted", "<task-notification>", "Base directory for this skill:",
         "This session is being continued from a previous conversation")

MID_TURN = ("Stop hook feedback:", "[Request interrupted", "Base directory for this skill:")


OPENERS_RE = re.compile(r"^\s*(?:no|nope|wrong|stop)\b", re.I)
HOLDS_RE = re.compile(
    r"\bdon'?t\b|\bdo not\b|\bnever\b|\balways\b|\bi said\b|\bi told you\b|"
    r"\bnot what i asked\b|\bthat'?s not\b|\binstead of\b|\bfrom now on\b", re.I)

MEMORY_WRITE_TOOL = "mcp__agent-context__upsert_memory"
EDIT_BODY_TOOL = "mcp__agent-context__edit_body"

REASON = (
    "user corrected you this turn. If the correction holds beyond this task (a "
    "preference, a rule, a fact about his setup), save it with `upsert_memory` "
    "as a feedback memory with origin user, or fold it into the memory that "
    "owns the topic. If it applies only to this task, end the turn."
)


def allow():
    sys.exit(0)


def role(rec):
    return rec.get("type") or rec.get("role")


def content(rec):
    return (rec.get("message") or {}).get("content")


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") or "" for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_real_user_prompt(rec):
    'A user-typed prompt: not a tool result, not harness noise, not a bare command tag.'
    if role(rec) != "user" or rec.get("isMeta"):
        return False
    c = content(rec)
    if isinstance(c, str):
        has_text, only_tool_result = bool(c.strip()), False
    elif isinstance(c, list):
        has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in c)
        only_tool_result = len(c) > 0 and all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
    else:
        return False
    if not has_text or only_tool_result:
        return False
    cleaned = " ".join(COMMAND_RE.sub(" ", SYSREM_RE.sub(" ", text_of(c))).split())
    return bool(cleaned) and not cleaned.startswith(NOISE)


def clean_text(c):
    return " ".join(COMMAND_RE.sub(" ", SYSREM_RE.sub(" ", text_of(c))).split())


def is_mid_turn(rec):
    "A user-role record written inside a turn: a tool result, a skill body, a Stop\n    hook's feedback, an interrupt marker. It neither starts a turn nor ends the search."
    if rec.get("isMeta"):
        return True
    c = content(rec)
    if isinstance(c, list) and c and all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return True
    return clean_text(c).startswith(MID_TURN)


def from_harness(rec):
    'from harness.'
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return True
    return rec.get("promptSource") == "system"


def find_turn(records):
    '(user_text, prev_assistant_text, prev_asked_question, turn_records) for the turn\n    that just ended, or (None, "", False, ()) when user did not start it.'
    start = None
    for idx in range(len(records) - 1, -1, -1):
        rec = records[idx]
        if role(rec) != "user" or is_mid_turn(rec):
            continue
        if from_harness(rec) or not is_real_user_prompt(rec):
            return None, "", False, ()
        start = idx
        break
    if start is None:
        return None, "", False, ()

    user_text = clean_text(content(records[start]))

    prev_text = ""
    prev_asked = False
    for j in range(start - 1, -1, -1):
        rec = records[j]
        if role(rec) != "assistant":
            continue
        c = content(rec)
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use" \
                        and b.get("name") == "AskUserQuestion":
                    prev_asked = True
        prev_text = text_of(c).strip()
        break

    return user_text, prev_text, prev_asked, records[start:]


def is_correction(user_text, prev_text, prev_asked):
    if not (OPENERS_RE.search(user_text) or HOLDS_RE.search(user_text)):
        return False
    prev_is_question = prev_asked or prev_text.rstrip().endswith("?")
    short_reply = len(user_text.split()) < 6
    if prev_is_question and short_reply:
        return False
    return True


def turn_wrote_memory(turn_records):
    for rec in turn_records:
        if role(rec) != "assistant":
            continue
        c = content(rec)
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name") or ""
            args = b.get("input") if isinstance(b.get("input"), dict) else {}
            if name == MEMORY_WRITE_TOOL:
                return True
            if name == EDIT_BODY_TOOL and args.get("kind") == "memory":
                return True
    return False


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    if not isinstance(payload, dict):
        allow()
    if payload.get("stop_hook_active"):
        allow()

    names = [n.strip() for n in (os.environ.get("AGENT_CONTEXT_DISABLE_HOOKS") or "").split(",")]
    if NAME in names or NAME + ".py" in names:
        allow()

    try:
        records = transcript_records.recent(payload.get("transcript_path") or "")
    except Exception:
        allow()

    try:
        user_text, prev_text, prev_asked, turn_records = find_turn(records)
    except Exception:
        allow()
    if not user_text:
        allow()

    try:
        corrected = is_correction(user_text, prev_text, prev_asked)
    except Exception:
        allow()
    if not corrected:
        allow()

    try:
        if turn_wrote_memory(turn_records):
            allow()
    except Exception:
        allow()

    json.dump({"decision": "block", "reason": REASON}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        allow()
