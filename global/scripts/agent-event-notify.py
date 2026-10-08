#!/usr/bin/env python3
'agent-event-notify — push the two events that mean something, not every line.\n\n    agent-event-notify --event turn-finished < hook-json\n    agent-event-notify --event blocked       < hook-json\n\n`agent-notify-watch` posts whenever an agent writes prose. That is a running\ncommentary: it cannot tell "your turn is done" or "something is waiting on you"\nfrom the middle of a turn. A harness hook can say what a transcript watcher\ncannot: the Stop event is the turn ending, and the Notification event is the\nharness asking for a human.\n\nReads the harness\'s hook JSON on stdin (`session_id`, `transcript_path`, `cwd`,\nand for Notification a `message`) and posts one `kind: "alert"` to the relay\ncarrying `host` and `session` — the pair the phone resolves a tap against.\n\nFour harnesses call this, not one. Claude Code and copilot pipe their own hook\nJSON straight in, unmodified, from a hook (agent-turn-finished-notify.py,\nagent-blocked-notify.py), the same two files, installed under each harness\'s\nown hook config, because neither harness needs a translated payload. opencode\nhas no hook file at all, so a generated plugin (agent-context-notify.js)\nconstructs an equivalent payload itself and spawns this the same detached way.\npi needs nothing installed for it here: pi-code\'s Claude Hooks Extension\nalready bridges `~/.claude/settings.json`\'s Stop and Notification hooks onto\npi\'s own lifecycle, so pi already invokes this exact script through the exact\nsame two shell hooks Claude Code uses — see the `notification_type` gate below\nfor what that bridge gets wrong without help.\n\n**This never fails a turn.** Every path exits 0: a hook that can break the\nharness it reports on is worse than no hook, which is `token-usage-collect`\'s\nrule and is this one\'s too.'
import argparse
import json
import os
import platform
import subprocess
import sys
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
CONFIG = os.path.join(HOME, ".config/agent-context/notifications", "config")
MUTED = os.path.join(HOME, ".config/agent-context/notifications", "muted")























BLOCKED_NOTIFICATION_TYPES = {"permission_prompt", "elicitation_dialog"}


def read_config():
    '`KEY=value` lines, sourced by sh elsewhere — read the same forgiving way\n    agent-notify-watch reads it, so the two never disagree about the file.'
    values = {}
    try:
        with open(CONFIG) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return values


def muted_terms():
    try:
        with open(MUTED) as handle:
            return {line.strip() for line in handle if line.strip()}
    except OSError:
        return set()


def tmux_session(cwd):
    "The tmux session this agent is running in, which is what the phone opens a\n    conversation by.\n\n    `$TMUX_PANE` first, because a hook runs inside the agent's own process and\n    therefore inside its pane — that is an exact answer, where matching on the\n    working directory is a guess that two panes in one directory make wrong. The\n    directory match is the fallback for an agent started outside tmux entirely,\n    and it deliberately returns nothing when two sessions share the directory:\n    an unnamed notification still arrives, while a wrongly-named one opens the\n    wrong conversation.\n\n    Harness-agnostic on purpose: `$TMUX_PANE` and `tmux list-panes` come from the\n    terminal, not from Claude Code, opencode, pi or copilot, so this needs no\n    per-harness branch at all."
    pane = os.environ.get("TMUX_PANE", "")
    if pane:
        try:
            name = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane, "#S"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if name:
                return name
        except (OSError, subprocess.SubprocessError):
            pass
    if not cwd:
        return None
    try:
        output = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{pane_current_path}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = set()
    for line in output.splitlines():
        marker = line.find(":/")
        if marker != -1 and line[marker + 1:] == cwd:
            found.add(line[:marker])
    return found.pop() if len(found) == 1 else None


def last_assistant_text(transcript_path):
    'The last thing the agent actually said, for a turn-finished push.\n\n    Read backwards over the tail rather than parsing the file: a transcript runs\n    to megabytes and a hook sits between the person and their prompt coming\n    back. 64 KB is comfortably more than one turn\'s final message and costs one\n    seek. A transcript that cannot be read is not an error here — the push still\n    goes out saying the turn finished, which is the part that matters.\n\n    Two shapes are read, not one: Claude Code writes `{"type":"assistant",\n    "message":{"content":[...]}}`; pi writes `{"type":"message","message":\n    {"role":"assistant","content":[...]}}`. pi\'s own Stop bridge (pi-code\'s\n    Claude Hooks Extension) points `transcript_path` at pi\'s own session file,\n    which is a different shape from Claude\'s, not a missing one — so reading\n    only Claude\'s shape here would silently degrade every pi push to the\n    generic "turn finished" text. opencode keeps no transcript_path in the\n    payload this script is handed at all, so it degrades the same way for a\n    different, honest reason: there is nothing to read.'
    try:
        with open(transcript_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            tail = handle.read()
    except OSError:
        return ""
    for chunk in reversed(tail.split(b"\n")):
        if not chunk.strip():
            continue
        try:
            entry = json.loads(chunk)
        except ValueError:
            continue  
        kind = entry.get("type")
        message = entry.get("message") or {}
        if kind == "assistant":
            pass  
        elif kind == "message" and message.get("role") == "assistant":
            pass  
        else:
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text = "\n".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        ).strip()
        if text:
            return text
    return ""


def post(relay, wake_key, text, host, session):
    body = {"wakeKey": wake_key, "kind": "alert", "text": text[:300]}
    
    
    
    if host and session:
        body["host"] = host
        body["session"] = session
    
    
    
    
    
    
    request = urllib.request.Request(
        f"{relay.rstrip('/')}/wake",
        data=json.dumps(body).encode(),
        
        
        headers={
            "content-type": "application/json",
            "user-agent": "agent-event-notify (example-project)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except (urllib.error.URLError, OSError) as error:
        print(f"agent-event-notify: relay refused: {error}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(prog="agent-event-notify")
    parser.add_argument("--event", required=True,
                        choices=("turn-finished", "blocked"))
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent and send nothing")
    args = parser.parse_args()

    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}

    cwd = event.get("cwd") or os.getcwd()
    
    
    
    
    
    transcript = event.get("transcript_path") or event.get("transcriptPath") or ""
    project = os.path.basename(cwd.rstrip("/")) or "agent"

    muted = muted_terms()
    if project in muted or cwd in muted or (transcript and transcript in muted):
        return

    config = read_config()
    relay = config.get("AGENTORCHESTRA_RELAY", "")
    wake_key = config.get("AGENTORCHESTRA_WAKE_KEY", "")
    host = config.get("AGENTORCHESTRA_HOST_ALIAS", "") or platform.node().split(".")[0]
    session = tmux_session(cwd)

    if args.event == "blocked":
        notification_type = event.get("notification_type")
        if notification_type is not None and notification_type not in BLOCKED_NOTIFICATION_TYPES:
            
            
            return
        
        
        
        message = (event.get("message") or "").strip() or "is waiting on you"
        text = f"{project}: {message}"
    else:
        said = last_assistant_text(transcript)
        text = f"{project}: finished — {said}" if said else f"{project}: turn finished"

    if args.dry_run or not (relay and wake_key):
        named = f"  [{host}/{session}]" if host and session else "  [unnamed]"
        print(f"would notify — {text[:200]}{named}")
        return

    post(relay, wake_key, text, host, session)


if __name__ == "__main__":
    
    
    
    try:
        main()
    except Exception as error:  
        print(f"agent-event-notify: {error}", file=sys.stderr)
