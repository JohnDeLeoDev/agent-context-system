#!/usr/bin/env python3
"warn-long-foreground-command: advisory nudge when a Bash call held the turn too long.\n\nWHY. The always-loaded instruction requires backgrounding anything expected to run\nover 30 s (memory agents-work-asynchronously-and-stay-available). A rule\nthat depends on remembering mid-turn is the failure this store keeps rediscovering,\nso it is a hook. This never blocks: by PostToolUse the command already finished, and\na deny here would only scold work that is already done.\n\nTWO EVENTS, ONE FILE (registered on both, like approval-question and\nlsp-failure-tripwire). READ, not guessed: the Claude Code hooks reference and every\nPostToolUse hook already in this store were checked for a duration/elapsed field, and\nneither has one -- the only way to measure how long a call held the turn is to time it\nacross both events ourselves.\n\n  PreToolUse(Bash) stamps a start time under STATE, keyed by session_id then\n  tool_use_id.\n  PostToolUse(Bash) reads that stamp, removes it either way, and -- unless the call\n  ran with run_in_background: true or finished in 30 s or less -- emits\n  additionalContext naming the elapsed seconds.\n\nFAILS OPEN. A missing or unreadable start record is silent, never an error: a call a\nPreToolUse guard denied, a hook that crashed, or a PostToolUseFailure (which never\nreaches this file's post(), same as failure-trace) all leave no record, and that is\nnot evidence of anything."
import json
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"),
                     "agent-context", "foreground-command-timer")


WARN_THRESHOLD = 30.0
RECORD_TTL = 6 * 3600
_UNSAFE = re.compile(r"[^\w.-]")


def _safe(name):
    return _UNSAFE.sub("_", str(name or "")) or "x"


def _session_dir(sid):
    return os.path.join(STATE, _safe(sid))


def _prune(d):
    'Drop start records older than RECORD_TTL, e.g. a call whose PostToolUse never\n    arrived (denied, crashed, or a PostToolUseFailure).'
    try:
        cutoff = time.time() - RECORD_TTL
        for name in os.listdir(d):
            path = os.path.join(d, name)
            try:
                if os.stat(path).st_mtime < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def pre(payload):
    tool_use_id = payload.get("tool_use_id")
    if not tool_use_id:
        return 0
    sid = payload.get("session_id") or "nosession"
    d = _session_dir(sid)
    try:
        os.makedirs(d, exist_ok=True)
        _prune(d)
        with open(os.path.join(d, _safe(tool_use_id)), "w", encoding="utf-8") as fh:
            fh.write(repr(time.time()))
    except OSError:
        pass
    return 0


def _take_start(sid, tool_use_id):
    'The recorded start time, or None. Removes the record either way.'
    path = os.path.join(_session_dir(sid), _safe(tool_use_id))
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        os.remove(path)
    except OSError:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def emit(text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": text}}))


def post(payload):
    tool_use_id = payload.get("tool_use_id")
    if not tool_use_id:
        return 0
    sid = payload.get("session_id") or "nosession"
    start = _take_start(sid, tool_use_id)
    if start is None:
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool_input.get("run_in_background"):
        return 0
    elapsed = time.time() - start
    if elapsed <= WARN_THRESHOLD:
        return 0
    emit(
        "warn-long-foreground-command: that Bash call held the turn for %d s. Run "
        "anything expected over 30 s with run_in_background (or Monitor) and keep "
        "working on the next independent step." % round(elapsed))
    return 0


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        return pre(payload)
    if event == "PostToolUse":
        return post(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        sys.exit(0)
