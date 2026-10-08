#!/usr/bin/env python3

'Stop: refuse a turn that asks user to decide something in prose.\n\nWHY THIS EXISTS\n    `present-options-as-structured-questions` is an always-loaded memory, and a\n    memory alone drifts: a wrap-up can still put a choice in a paragraph headed\n    "Open decision, for when you\'re next at it". A rule that relies on the model\n    remembering decays, so this hook enforces it.\n\nWHAT IT DOES\n    On Stop, look at the final assistant message. If it ends by offering user a\n    choice, asking permission, or naming an open/deferred decision, and\n    AskUserQuestion was not called since the last user message, exit 2. The agent\n    gets stderr back and a turn to re-issue the question through the tool.\n\nWHAT IT DOES NOT DO\n    It does not fire on every question mark. A rhetorical question, a question in\n    quoted text, and a question inside code are all normal; a gate that trips on\n    those is a gate that gets switched off. Matching is on a small set\n    of decision-shaped phrases, over text with fenced blocks and code spans stripped.\n    It also does not judge whether asking was warranted -- the rule is about how a\n    choice is presented, not whether to ask. Something with a sensible default should\n    not have become a question in the first place, and no hook can tell the difference.\n\nCROSS-HARNESS\n    AskUserQuestion is a Claude Code tool. pi, opencode and Copilot CLI have no\n    equivalent and no Stop-hook wiring for this, so this hook enforces nothing there\n    and the rule stays instruction-level in those harnesses.\n\nLOOP SAFETY\n    `stop_hook_active` means this hook already blocked once and the agent is being\n    given another turn. It allows the stop unconditionally in that case, so the worst\n    case is one extra turn, never a wedged session. That also covers `claude -p`,\n    where there is no human to answer and AskUserQuestion cannot be used.\n\n    Fails open on any parse problem, like every other guard here.'
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp
import transcript_records






PHRASES = os.path.join(hp.scripts_dir(), "decision-phrases.py")


def _load_phrases():
    import importlib.util
    spec = importlib.util.spec_from_file_location("decision_phrases", PHRASES)
    if spec is None or spec.loader is None:
        raise ImportError(PHRASES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

FEEDBACK = """BLOCKED by require-structured-questions: this turn ends by asking user to decide
something in prose, and AskUserQuestion was never called.

Matched: {matched}

Re-issue the decision through the AskUserQuestion tool, in this same turn:
  - one question per decision, up to 4 per call
  - the option you recommend first, labeled "(Recommended)"
  - multiSelect when the choices are not mutually exclusive
  - keep the findings, diagnosis and reasoning in prose -- only the choices move
    into the tool

This applies to a decision you are deferring, too. A wrap-up that names something
user will have to choose later is an AskUserQuestion in the message that raises it,
not a paragraph.

If you are not asking him to choose -- the phrase was rhetorical, quoted, or
you have already decided and are just reporting -- rewrite the sentence so it does not
read as a question, and stop again. Do not call AskUserQuestion to satisfy this hook
when there is no real decision: something with a sensible default should have been
decided by you."""


def asked_structurally(transcript_path: str) -> bool:
    'Did AskUserQuestion run since the last user message?\n\n    Per-line parsing, because one malformed transcript line must not decide the\n    verdict for the whole turn.'
    used = False
    for rec in transcript_records.recent(transcript_path):
        role = rec.get("type") or rec.get("role")
        message = rec.get("message") or {}
        if role == "user" or message.get("role") == "user":
            
            
            content = message.get("content")
            if isinstance(content, str):
                used = False
            elif isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "text" for b in content
            ):
                used = False
            continue
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") == "AskUserQuestion"):
                    used = True
    return used


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0                                   

    
    
    if data.get("stop_hook_active"):
        return 0

    msg = data.get("last_assistant_message") or ""
    if not isinstance(msg, str) or not msg.strip():
        return 0

    try:
        hits = _load_phrases().find_decisions(msg)
    except Exception:
        return 0                                   
    if not hits:
        return 0

    if asked_structurally(data.get("transcript_path") or ""):
        return 0                                   

    seen, matched = set(), []
    for h in hits:
        k = h.lower()
        if k not in seen:
            seen.add(k)
            matched.append(f'"{h}"')
    print(FEEDBACK.format(matched=", ".join(matched[:5])), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
