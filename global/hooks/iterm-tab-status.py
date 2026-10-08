#!/usr/bin/env python3
"Set the iTerm2 tab's Claude Code status from a host iTerm2 is not running on.\n\niTerm2's own integration (~/.config/iterm2/cc-status, wired by iTerm2 into this\nmachine's settings.json) talks to iTerm2 through its local API socket, so it only\nworks on the Mac iTerm2 runs on. A Claude Code session on ls, reached over ssh in\na tmux -CC tab, never showed a status at all.\n\niTerm2 also takes the status as an escape sequence, OSC 21337 (`status=`,\n`indicator=`, `status-color=`, `detail=`), and an escape sequence crosses ssh and\ntmux. A hook has no terminal of its own, so this writes to the pane's tty: bare\nunder a -CC client (tmux relays pane bytes to it verbatim), wrapped for tmux\npassthrough under a plain client (allow-passthrough is on in the chezmoi\ntmux.conf). Terminals that do not know the sequence ignore it.\n\nStates and colors follow cc-status: working #ff9500, waiting #5f87ff, idle with a\n#00d75f dot and #888888 text.\n\nWhere cc-status is installed this does nothing, so a tab never gets two writers.\nOutside tmux it does nothing either: without TMUX_PANE there is no tty to name."
import json
import os
import re
import subprocess
import sys

WORKING = ("working", "#ff9500", "#ff9500")
WAITING = ("waiting", "#5f87ff", "#5f87ff")
IDLE = ("idle", "#00d75f", "#888888")
ASKING_TOOLS = ("AskUserQuestion", "ExitPlanMode")
CC_STATUS = "/Applications/iTerm.app/Contents/Resources/utilities/cc-status"


def clean(text, limit=120):
    "One line, no separators or control bytes: `;` splits the sequence's keys and\n    an escape byte would end it early."
    text = re.sub(r"[\x00-\x1f\x7f;]+", " ", str(text or "")).strip()
    return text[:limit]


def first_question(tool_input):
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if isinstance(questions, list) and questions and isinstance(questions[0], dict):
        return questions[0].get("question", "")
    return ""


def tool_detail(name, tool_input):
    if not isinstance(tool_input, dict):
        return name
    for key in ("description", "command", "file_path", "pattern", "url", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return f"{name}: {value}"
    return name


def status_for(event):
    '(state, detail) for one hook payload, or None to leave the tab as it is.\n    ("clear", "") empties the status.'
    name = event.get("hook_event_name", "")
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    if name == "SessionStart":
        return IDLE, ""
    if name == "UserPromptSubmit":
        return WORKING, event.get("prompt", "")
    if name == "PreToolUse":
        if tool == "AskUserQuestion":
            return WAITING, first_question(tool_input)
        if tool == "ExitPlanMode":
            return WAITING, "Plan ready for review"
        return WORKING, tool_detail(tool, tool_input)
    if name == "PostToolUse":
        
        return WORKING, "" if tool in ASKING_TOOLS else tool_detail(tool, tool_input)
    if name == "Notification":
        kind = event.get("notification_type", "")
        if kind == "idle_prompt":
            return IDLE, event.get("message", "")
        if kind in ("permission_prompt", "elicitation_dialog", "agent_needs_input"):
            return WAITING, event.get("message", "")
        return None
    if name == "Stop":
        message = event.get("last_assistant_message") or ""
        return IDLE, message.strip().splitlines()[0] if message.strip() else ""
    if name == "StopFailure":
        return IDLE, event.get("error", "") or "Stopped on an error"
    if name == "SessionEnd":
        return "clear", ""
    return None


def sequence(result, bare=False):
    state, detail = result
    if state == "clear":
        body = "status=;indicator="
    else:
        text, dot, color = state
        body = f"status={text};indicator={dot};status-color={color};detail={clean(detail)}"
    osc = f"\x1b]21337;{body}\x07"
    if bare:
        return osc
    return "\x1bPtmux;" + osc.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def pane_facts(pane):
    "(control mode, pane tty, the tmux session's LC_TERMINAL line) from one tmux call; each\n    event used to start four. Under a control-mode client (iTerm2's tmux -CC) tmux relays a\n    pane's bytes verbatim, so the sequence must go bare: iTerm2 ignores the passthrough\n    wrapper there (same rule as __emit_osc in the chezmoi zsh tree). The LC_TERMINAL line is\n    empty when the session does not set it."
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{client_control_mode} #{pane_tty}",
             ";", "show-environment", "-t", pane, "LC_TERMINAL"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "", ""
    lines = out.stdout.splitlines()
    control, _, tty = (lines[0] if lines else "").partition(" ")
    return control == "1", tty.strip(), lines[1].strip() if len(lines) > 1 else ""


def iterm_client(control, lc_terminal):
    "True when the terminal on the far end is iTerm2: a control-mode client, or\n    LC_TERMINAL=iTerm2 in the tmux session's environment (tmux refreshes it from\n    each attaching client, update-environment in the chezmoi tmux.conf) or in\n    this process's own. Terminal.app and Ghostty get nothing: OSC 21337 is\n    iTerm2's alone, and Terminal.app names itself LC_TERMINAL=Apple_Terminal."
    if control:
        return True
    if lc_terminal:
        return lc_terminal == "LC_TERMINAL=iTerm2"
    return os.environ.get("LC_TERMINAL") == "iTerm2"


def main():
    
    
    
    if os.path.exists(CC_STATUS):
        return 0
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return 0
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    result = status_for(event)
    if result is None:
        return 0
    control, tty, lc_terminal = pane_facts(pane)
    if not iterm_client(control, lc_terminal):
        return 0
    if not tty.startswith("/dev/"):
        return 0
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            os.write(fd, sequence(result, control).encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
