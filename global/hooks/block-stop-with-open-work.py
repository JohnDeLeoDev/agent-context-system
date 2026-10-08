#!/usr/bin/env python3
'Stop: refuse a turn that ends with work the agent says it will do itself.\n\nWHY THIS EXISTS\n    An agent that ends a turn on a "Remaining" list of its own edits, on "I will run\n    it once the edits are complete" or on "Back to it." has stopped while needing\n    nothing from user. The Communication instruction says to do open work and end\n    only on a step user has to take. This hook enforces it.\n\nWHAT IT BLOCKS\n    A final message whose visible prose (fences, code spans and quoted lines removed)\n    does any of these:\n      - a line going back to work: "Back to it.", "Continuing with the store side."\n      - says what the agent will do: "I will run", "I\'ll draft", "Next I will update",\n        "Will run the gate after the edits"\n      - says its own work is unfinished: "I have not run the gate yet"\n      - opens a "Remaining", "TODO" or "Next steps" list, bulleted or not. A label\n        with nothing after it ("Remaining risk: none") is a finished report\n    Exit 2 with the reason on stderr, which Claude Code feeds back to the agent.\n\nWHAT IT ALLOWS\n    - a turn that asked user through AskUserQuestion\n    - a turn that started something that wakes the session: a background Bash, an\n      Agent, Monitor, Workflow or ScheduleWakeup, a SendMessage idle subscription\n    - a step only user can take: a login, a password, sudo, 2FA, an approval, an unlock\n    - a wait on something outside the session: a machine asleep or offline, "when pc\n      wakes", "blocked on", "waiting on", work running in the background\n    - a Remaining list given as the answer to user asking what is left or for status.\n      A resume line or "I will <do>" is never excused that way: answering a question\n      partway through the work does not end the work.\n\nLOOP SAFETY\n    Refuses at most MAX_BLOCKS times per prompt, counted from this hook\'s own feedback\n    in the transcript, then allows: a run that is stuck ends and can be reviewed.\n    Anthropic\'s Opus 5.5 guide puts the cap for automatic continuations at\n    two or three. stop_hook_active with none of this hook\'s feedback in the turn (another\n    hook refused, or no readable transcript) allows. Fails open on any parse problem. AGENT_CONTEXT_DISABLE_HOOKS stands it down for an eval\n    control arm.\n\nCROSS-HARNESS\n    Claude Code only. The other harnesses raise no Stop event.\n\nPython 3.8-safe: the Synology nodes run hooks on the system 3.8.15.'
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import transcript_records

NAME = "block-stop-with-open-work"

FENCE = re.compile(r"```.*?```", re.S)
CODE = re.compile(r"`[^`\n]*`")
QUOTE = re.compile(r"^\s*>.*$", re.M)


_BULLET = r"(?:[-*•]\s+)?"
RESUME = re.compile(
    r"^\s*" + _BULLET + r"(?:back to (?:it|work)|continuing|resuming|moving on)"
    r"(?:\s*[.!]?\s*$|\s+(?:with|to|on)\b)", re.I | re.M)
_WORK = (r"(?:run|re-?run|edit|fix|draft|write|update|finish|land|start|continue|do|add|"
         r"port|rewrite|verify|test|commit|push|deploy|check|implement|move|delete|"
         r"remove|convert|migrate|wire|build|make)")

FUTURE_SELF = re.compile(
    r"(?:\b(?:i'?ll|i will|i am going to|i'?m going to|i still need to|i still have to|"
    r"next,? i(?:'ll| will)?|then i(?:'ll| will)?)"
    r"|(?:^|[.!?:]\s+)(?:will|going to|still need to))"
    r"\s+(?:\w+\s+){0,2}?" + _WORK + r"\b", re.I | re.M)
NOT_DONE = re.compile(
    r"\bi (?:have not|haven'?t) (?:yet )?(?:run|finished|done|started|landed|verified|"
    r"made|applied)\b", re.I)


REMAINING = re.compile(
    r"^\s*" + _BULLET + r"(?:#+\s*)?(?:\*\*)?(?:remaining|still to do|left to do|"
    r"not (?:yet )?done|todo|next steps?)(?:\*\*)?\s*(?::|$)"
    r"(?!\s*(?:\*\*)?\s*(?:none|nothing|n/a)\b)", re.I | re.M)

ONLY_JOHN = re.compile(
    r"\blog\s*in\b|\blogin\b|sign\s*-?in|password|passphrase|\b2fa\b|touch\s*id|"
    r"\bsudo\b|\bapprove\b|\bapproval\b|\bunlock\b|only\s+you\s+can", re.I)
WAIT_EXTERNAL = re.compile(
    r"\bwhen \S+ wakes\b|\basleep\b|\boffline\b|\bblocked on\b|\bwaiting on\b|"
    r"\bin the background\b|\bstill running\b", re.I)
STATUS_QUESTION = re.compile(
    r"\bwhat'?s left\b|\bwhat is left\b|\bstatus\b|\bprogress\b|\bremaining\b|"
    r"\bwhere are (?:we|you|things)\b|\bwhat are (?:you|we|they) doing\b", re.I)
WAKES = ("Agent", "Task", "Monitor", "Workflow", "ScheduleWakeup")

MAX_BLOCKS = 2
FEEDBACK_MARK = "BLOCKED by block-stop-with-open-work"
FEEDBACK_PREFIX = "Stop hook feedback:"

FEEDBACK = FEEDBACK_MARK + """: this turn ends with work you said you would do yourself ({matched}).
{items}Do the work now, in this turn, then report the result. If one item is blocked, say what blocks it.
End a turn only when the work is done, the rest needs user (AskUserQuestion, a login, a password),
or a background task, subagent or Monitor you started will wake this session."""

LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")


def open_items(prose, start):
    'The list items that follow a Remaining/TODO heading, up to five.'
    items = []
    for line in prose[start:].splitlines()[1:]:
        m = LIST_ITEM.match(line)
        if m:
            items.append(m.group(1))
            if len(items) == 5:
                break
        elif items:
            break
    return items


def role(rec):
    return rec.get("type") or rec.get("role")


def content(rec):
    return (rec.get("message") or {}).get("content")


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_feedback(rec):
    "A Stop hook's feedback, which the harness writes as a user record."
    return role(rec) == "user" and text_of(content(rec)).lstrip().startswith(
        FEEDBACK_PREFIX)


def is_prompt(rec):
    if role(rec) != "user" or is_feedback(rec):
        return False
    c = content(rec)
    if isinstance(c, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in c)
    return bool(c)


def turn_start(records):
    'Index of the last real prompt that an assistant record follows, else None.'
    for idx in range(len(records) - 1, -1, -1):
        if is_prompt(records[idx]) and any(role(r) == "assistant"
                                           for r in records[idx + 1:]):
            return idx
    return None


def own_blocks(path):
    'How many times this hook refused since the last real prompt; None when the\n    transcript has no prompt. Permissive on any error.'
    records = transcript_records.recent(path)
    start = turn_start(records)
    if start is None:
        return None
    return sum(1 for rec in records[start + 1:]
               if is_feedback(rec) and FEEDBACK_MARK in text_of(content(rec)))


def read_turn(path):
    '(asked, wakes, prompt) for the turn that just ended. The turn runs from the last\n    real prompt, past any Stop hook feedback. Permissive on any error.'
    asked = wakes = False
    prompt = ""
    records = transcript_records.recent(path)
    start = turn_start(records)
    if start is None:
        return asked, wakes, prompt
    prompt = text_of(content(records[start]))
    for rec in records[start + 1:]:
        c = content(rec)
        if role(rec) != "assistant" or not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name") or ""
            args = b.get("input") if isinstance(b.get("input"), dict) else {}
            if name == "AskUserQuestion":
                asked = True
            elif name in WAKES or args.get("run_in_background") is True \
                    or (name == "SendMessage" and args.get("notify_when_idle")):
                wakes = True
    return asked, wakes, prompt


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    names = [n.strip() for n in (os.environ.get("AGENT_CONTEXT_DISABLE_HOOKS") or "").split(",")]
    if NAME in names or NAME + ".py" in names:
        return 0
    msg = data.get("last_assistant_message") or ""
    if not isinstance(msg, str) or not msg.strip():
        return 0
    
    msg = msg.replace("’", "'")
    path = data.get("transcript_path") or ""
    asked, wakes, prompt = read_turn(path)
    blocks = own_blocks(path)
    
    if data.get("stop_hook_active") and not blocks:
        return 0
    if asked or wakes or (blocks or 0) >= MAX_BLOCKS:
        return 0
    if ONLY_JOHN.search(msg) or WAIT_EXTERNAL.search(msg):
        return 0
    prose = QUOTE.sub(" ", CODE.sub(" ", FENCE.sub(" ", msg)))
    hits = []
    for rx in (RESUME, FUTURE_SELF, NOT_DONE):
        m = rx.search(prose)
        if m:
            hits.append(" ".join(m.group(0).split()))
    remaining = REMAINING.search(prose)
    if not hits and remaining and not STATUS_QUESTION.search(prompt):
        hits.append(" ".join(remaining.group(0).split()))
    if not hits:
        return 0
    items = open_items(prose, remaining.start()) if remaining else []
    listed = "".join("  - %s\n" % i for i in items)
    print(FEEDBACK.format(matched=", ".join('"%s"' % h for h in hits),
                          items="Still open, by your own list:\n" + listed if listed else ""),
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
