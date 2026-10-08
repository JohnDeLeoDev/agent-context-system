#!/usr/bin/env python3

'terse-output-gate — a turn may not end on unearned prose.\n\nWhy this exists alongside terse-output-check. The judgment is identical and lives\nin one place (`terse-judge`). Delivery is what differs, and two separate facts\nmake the difference matter:\n\n  * A Stop hook\'s `systemMessage` is terminal output for the human and\n    never enters model context; `{"decision": "block"}` does reach the model. The\n    advisory hook runs on UserPromptSubmit for this reason and\n    corrects the next turn.\n  * A ralph loop re-feeds by having its own Stop hook return decision:block\n    (stop-hook.sh, ralph-cleanup-stop.py). That path raises no\n    UserPromptSubmit event, so terse-output-check, verification-claim-check,\n    scaffold-change-notice and sync-fault-notice are silent inside a loop, which\n    is where output drifts most.\n\nStop is the one event a loop cannot skip, because firing it is how the loop\nadvances. Putting the gate here covers loops with no patch to the loop machinery\nand keeps the re-feed pointer at one line.\n\nWhy it cannot trap a session. Three independent stops, any one sufficient:\n\n  1. `stop_hook_active` means the model is already continuing because a stop hook\n     blocked. Never block twice in a row.\n  2. For length, a per-session ledger allows one block per session. Everything\n     after that is left to the advisory hook and to the telemetry. Banned language has\n     no cap: user never wants to see it. Stop 1 keeps it from\n     looping.\n  3. Every failure path — unreadable transcript, unwritable ledger, malformed\n     payload — allows. Broken bookkeeping must never present as a violation.\n\nIt records every turn. The block is capped; the telemetry is not. Stop fires\nonce per turn everywhere, loops included, so this is the one place that can log\nthe trend across a session. `terse-judge` does the writing.'

import importlib.util
import json
import os
import sys
TYPE_CHECKING = False  
if TYPE_CHECKING:
    from typing import NoReturn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp


REISSUE_SEPARATOR = ("\n\nStart the re-issued message with this line on its own: "
                     "`--- hook block: re-issued below ---`")

STATE_DIR = os.environ.get("TERSE_GATE_STATE_DIR") or os.path.join(
    hp.state_dir(), "terse-gate")

JUDGE = os.environ.get("TERSE_JUDGE") or os.path.join(
    hp.scripts_dir(), "terse-judge.py")


def allow() -> "NoReturn":
    sys.exit(0)


def already_blocked(session_id, kind="len"):
    'Has this session already been blocked once for `kind`? Failure allows.\n\n    Length and word choice keep separate ledgers, so a turn stopped for running\n    long does not spend the one chance to stop a banned word later in the same\n    session. Two independent faults, two independent budgets.'
    if not session_id:
        return True                     
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        return os.path.exists(os.path.join(STATE_DIR, "%s.%s" % (session_id, kind)))
    except OSError:
        return True


def record_block(session_id, kind="len"):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "%s.%s" % (session_id, kind)), "w") as fh:
            fh.write("1")
    except OSError:
        pass                            


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    if not isinstance(payload, dict):
        allow()

    try:
        spec = importlib.util.spec_from_file_location("terse_judge", JUDGE)
        if spec is None or spec.loader is None:
            raise ImportError(JUDGE)
        tj = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tj)
    except Exception:
        allow()                         

    
    
    
    
    
    try:
        verdict = tj.judge(payload, "Stop", bump_depth=True)
    except Exception:
        allow()

    
    if payload.get("stop_hook_active"):
        allow()

    if verdict.get("empty"):
        allow()

    session_id = payload.get("session_id")

    
    
    
    
    
    
    
    
    hits = verdict.get("banned") or []
    if hits:
        reason = (
            "This turn is about to end using language user has banned: %s.\n\n"
            "Re-state the message without those. State the fact alone: cut the "
            "intensifier, the comparison, the narration or the offer. An em dash "
            "becomes a period, a comma, or a colon. A quotation of a banned phrase "
            "goes in backticks. Full lists and his own examples: "
            'get_doc("plain-language.md").\n\n'
            "Do not add an apology or an explanation of the correction. Give the "
            "message again, clean." + REISSUE_SEPARATOR
        ) % ", ".join(hits)
        json.dump({"decision": "block", "reason": reason}, sys.stdout)
        sys.stdout.write("\n")
        sys.exit(0)

    if not verdict.get("over"):
        allow()

    
    if already_blocked(session_id, "len"):
        allow()

    record_block(session_id, "len")

    if verdict["in_loop"]:
        tail = (
            "This is a loop iteration, where the budget is tighter still: an "
            "iteration's visible output is ONE line (`done:` / `blocked:` / "
            "`no-op:` and why), with everything else going to the loop's own "
            "ledger. A loop that narrates each turn buries the one turn that "
            "mattered.")
    else:
        tail = (
            "Cut the recap of steps user watched and the file inventory. Keep "
            "tables, charts and lists that carry real information. A completed "
            "task is its result and nothing else.")

    reason = (
        "This turn is about to end on %d words. The budget is %d, and this turn "
        "did not earn more: no structured question was asked, no tool failed, "
        "and user did not ask for a report.\n\n"
        "%s\n\n"
        "Re-state it. Do not add to it. Lead with the answer, keep what only "
        "you know, and stop. Prose is earned by a question, a decision that "
        "needs user, a failure, or a finding he would not otherwise see; none of "
        "those is present here.\n\n"
        "Fenced code and command output are not counted against the budget, so "
        "there is no need to drop evidence to fit."
    ) % (verdict["words"], verdict["budget"], tail) + REISSUE_SEPARATOR

    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        
        allow()
