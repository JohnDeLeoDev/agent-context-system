#!/usr/bin/env python3
'UserPromptSubmit: the previous message ran long and the turn did not earn it.\n\nuser\'s rule, stated repeatedly and enforced nowhere until this hook: "Terse is\nthe default and the ceiling is low: a status is one line; a completed task is its\nresult and nothing else. Prose is earned by a question asked, a decision that\nneeds him, a failure, or a finding he would not otherwise see."\n\nWARN, NEVER BLOCK, for the reason block-shell-file-read gives for warning on\ngrep. He asks for a full report perhaps once a day, and a guard that refuses one\nhe asked for is a guard that gets switched off, after which it protects nothing.\n\nWHAT THIS HOOK IS NOT. It does not judge and it does not measure; `terse-judge`\ndoes both, and terse-output-gate calls the same function on Stop. Two copies of\n"did this earn its length" would have drifted the first time either budget moved,\nand the fleet has paid for that shape before — a lesson applied at one site and\nnot its sibling.\n\nITS ONE STRUCTURAL BLIND SPOT, now covered elsewhere. UserPromptSubmit does not\nfire when a ralph loop re-feeds, because a loop re-feeds through its own Stop\nhook\'s decision:block. This hook has therefore never run inside a loop and never\nwill. terse-output-gate is the loop\'s coverage; leave this one advisory.\n\nTELEMETRY. terse-judge records every judgment it makes, so rows appear here\ntagged event="UserPromptSubmit" as well as from the gate tagged "Stop". Read the\nStop rows for a per-turn trend — those fire exactly once per turn in every\nsession — and treat these as the advisory\'s own firing record.'

import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp











MAX_PER_SESSION = 5
STATE_DIR = os.environ.get("TERSE_CHECK_STATE_DIR",
                           os.path.expanduser("~/.local/state/agent-context/terse-check"))

JUDGE = os.environ.get("TERSE_JUDGE") or os.path.join(
    hp.scripts_dir(), "terse-judge.py")


def budget_left(session: str) -> bool:
    'True if this session may still be told, and records the telling.\n\n    Fails OPEN: if the counter cannot be read or written, the hook still speaks. A\n    guard that goes silent because its bookkeeping broke is the failure mode this\n    store keeps paying for — silence is indistinguishable from "nothing to report".'
    if not session:
        return True
    path = os.path.join(STATE_DIR, session)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        n = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                n = int((fh.read() or "0").strip() or 0)
        if n >= MAX_PER_SESSION:
            return False
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(n + 1))
    except (OSError, ValueError):
        return True
    return True


def same_hits_as_last(session: str, hits) -> bool:
    'True when this session was last told these same hits. Records the new set.\n\n    One user turn can span many injected messages (task notifications, peer messages),\n    and each one re-runs this hook on the same previous-turn text. Naming the same words\n    again each time spends context and says nothing new. Fails open: an unreadable\n    record tells again.'
    if not session:
        return False
    key = "\n".join(sorted({h.lower() for h in hits})) + "\n"
    path = os.path.join(STATE_DIR, session + ".words")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                if fh.read() == key:
                    return True
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(key)
    except OSError:
        return False
    return False


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    try:
        spec = importlib.util.spec_from_file_location("terse_judge", JUDGE)
        if spec is None or spec.loader is None:
            raise ImportError(JUDGE)
        tj = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tj)
        
        
        
        verdict = tj.judge(data, "UserPromptSubmit", bump_depth=False)
    except Exception:
        return 0

    if verdict.get("empty"):
        return 0

    hits = verdict.get("banned") or []
    if not verdict.get("over") and not hits:
        return 0

    
    
    
    if hits:
        if same_hits_as_last(data.get("session_id") or "", hits):
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "plain-language: your previous turn used language user has "
                "banned: %s. Do not use it again in this session. State the fact "
                "alone. An em dash becomes a period, a comma, or a colon. A quotation "
                "goes in backticks. Lists and his examples: "
                'get_doc("plain-language.md").' % ", ".join(hits))}}))
        return 0

    if not budget_left(data.get("session_id") or ""):
        return 0

    
    
    
    
    
    
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": (
            f"terse-output-check: your previous message was {verdict['words']} "
            f"words; the budget is {verdict['budget']} unless the turn earned "
            "prose. That turn asked no question, hit no failure, and user did not "
            "ask for a report. Cut the recap of steps he watched, the file "
            "inventory and the summary tables. A completed task is its result "
            "and nothing else. In this turn, lead with the answer and stop.")}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
