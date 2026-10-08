#!/usr/bin/env python3

'verification-claim-gate — a turn may not end on a verification it never ran.\n\nThe sibling of verification-claim-check, and why both exist. The judgment is\nidentical and lives in one place (`verification-claim-judge`). What differs is\ndelivery:\n\n  * A Stop hook\'s `systemMessage` is terminal output for the human. It does not\n    enter model context.\n  * A Stop hook\'s `{"decision": "block"}` does reach the model: the turn does\n    not end, and `reason` is given to it as the thing to address.\n\nverification-claim-check therefore lives on UserPromptSubmit and corrects the\nnext turn. That is the right home for advice, and it has one structural blind\nspot: a single-turn run (`claude -p`, which eval-run drives) has no next turn,\nso the advisory never applies there.\n\nThis gate closes that: the claim is caught before the turn is allowed to end.\n\nSecond check: a turn that edited source, checked nothing, and says\nnothing about it. user\'s standard is that every work session ends in a tested outcome.\nThe judgment is `edited_without_running` in the same judge. It has its own\none-block ledger (`<session>.edit`), so a claim block earlier in the session\ndoes not silence it, and the reverse.\n\nWhy it cannot trap a session. A blocking hook that fires on something the model\ncannot satisfy is far worse than the defect it prevents, so there are three\nindependent stops, any one of which is sufficient:\n\n  1. `stop_hook_active` in the payload means the model is already continuing\n     because a stop hook blocked. Never block twice in a row.\n  2. A per-session ledger allows one block per session per check. A\n     second qualifying turn is left to the advisory hook.\n  3. Every failure path -- unreadable transcript, unwritable ledger, malformed\n     payload -- allows. Silence must never be the outcome of broken bookkeeping,\n     and an unknown must never masquerade as a violation.\n\nThe reason text asks for one of two things, both cheap: run the\ncommand, or say what was not verified. It never asks the model to prove\na negative.'

import json
import os
import sys
TYPE_CHECKING = False  
if TYPE_CHECKING:
    from typing import NoReturn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

STATE_DIR = os.environ.get("VERIFICATION_GATE_STATE_DIR") or os.path.join(
    hp.state_dir(), "verification-gate")

JUDGE = os.environ.get("VERIFICATION_CLAIM_JUDGE") or os.path.join(
    hp.scripts_dir(), "verification-claim-judge.py")

CLAIM_LEDGER = ""
EDIT_LEDGER = ".edit"

CLAIM_REASON = (
    "This turn is about to end on a claim that something was verified, and "
    "nothing in this turn ran it.\n\n"
    "  claimed: %s\n\n"
    "No test, build, lint or typecheck command appears anywhere in this "
    "turn's tool calls, and no subagent was spawned that could have run one "
    "out of sight.\n\n"
    "Do one of these, then finish:\n"
    "  1. Run it. The command is usually one line, and then the claim is "
    "true and supported.\n"
    "  2. Say what you did not verify. \"The change is written but I "
    "have not run the tests\" is a complete report and costs nothing.\n\n"
    "Do not rephrase the claim to slip past this. The defect being caught is "
    "not the wording, it is the gap between what was checked and what was "
    "said."
)

EDIT_REASON = (
    "This turn edited source and is about to end without checking it.\n\n"
    "  edited: %s\n\n"
    "No test, build, lint or typecheck command ran in this turn, no edited file "
    "was run, no subagent was spawned that could have run one, and the closing "
    "message does not say the change is unverified.\n\n"
    "Do one of these, then finish:\n"
    "  1. Run the project's tests or build, and report the result from the output.\n"
    "  2. Say that the change is not verified, and why: no tests exist, "
    "you were told not to run anything, or the check needs user. That is a "
    "complete report.\n\n"
    "user's standard is that work ends in a tested outcome. If the task is "
    "non-trivial, the test-first-delivery skill is the procedure."
)


def allow() -> "NoReturn":
    sys.exit(0)


def already_blocked(session_id, suffix):
    'Has this check already blocked this session once? Failure here allows.'
    if not session_id:
        return True                     
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        return os.path.exists(os.path.join(STATE_DIR, str(session_id) + suffix))
    except OSError:
        return True


def record_block(session_id, suffix):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, str(session_id) + suffix), "w") as fh:
            fh.write("1")
    except OSError:
        pass                            



REISSUE_SEPARATOR = ("\n\nStart the re-issued message with this line on its own: "
                     "`--- hook block: re-issued below ---`")


def block(reason):
    json.dump({"decision": "block", "reason": reason + REISSUE_SEPARATOR}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()

    if not isinstance(payload, dict):
        allow()

    
    if payload.get("stop_hook_active"):
        allow()

    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        allow()

    session_id = payload.get("session_id")

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("vcj", JUDGE)
        if spec is None or spec.loader is None:
            raise ImportError(JUDGE)
        vcj = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vcj)
    except Exception:
        allow()                         

    
    if not already_blocked(session_id, CLAIM_LEDGER):
        try:
            sentence = vcj.judge_transcript(transcript)
        except Exception:
            sentence = None
        if sentence:
            record_block(session_id, CLAIM_LEDGER)
            block(CLAIM_REASON % sentence)

    if not already_blocked(session_id, EDIT_LEDGER):
        try:
            edited = vcj.judge_unverified_edit(transcript)
        except Exception:
            allow()
        if edited:
            record_block(session_id, EDIT_LEDGER)
            shown = ", ".join(edited[:5]) + (" (+%d more)" % (len(edited) - 5)
                                            if len(edited) > 5 else "")
            block(EDIT_REASON % shown)

    allow()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        
        allow()
