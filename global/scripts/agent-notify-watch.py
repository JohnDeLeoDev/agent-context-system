#!/usr/bin/env python3
"agent-notify-watch — tell the phone when an agent says something.\n\n    agent-notify-watch [--interval 5] [--dry-run]\n\nRuns on the machine, not the phone: iOS cannot watch a file on someone else's computer,\nand the relay already exists to carry exactly this. Every few seconds it reads whatever\nhas been appended to each agent transcript, and when an agent writes prose — not a tool\ncall, not its own thinking — it posts one line to the relay, which pushes it.\n\nTwo clients are watched: Claude Code, under ~/.claude/projects, and pi, under\n~/.pi/agent/sessions. Both write JSONL and both say the same things in it; what differs\nis where the role lives and where the working directory is recorded, which `new_messages`\nand `pi_cwd` handle between them."
import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.path.expanduser("~")
CONFIG = os.path.join(HOME, ".config/agent-context/notifications", "config")
MUTED = os.path.join(HOME, ".config/agent-context/notifications", "muted")
STATE = os.path.join(HOME, ".config/agent-context/notifications", "notify-state.json")
PROJECTS = hp.projects_dir(HOME)
PI_SESSIONS = os.path.join(HOME, ".pi", "agent", "sessions")


def read_config():
    'The shell config, read as `KEY=value` lines — it is sourced by sh elsewhere, so\n    it stays a shell file rather than becoming a second format to keep in sync.'
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


def live_directories():
    'Directories with a tmux pane in them, mapped to the tmux session they belong to.\n    A printable separator, and the split anchors on the path being absolute: tmux 3.7\n    rewrites control characters in format output, so a tab would come back as an\n    underscore.\n\n    The session name is kept so a notification can say which conversation it is about:\n    the phone opens a conversation by host alias and tmux session name, and this watcher\n    is the only thing that knows a turn happened. A directory with\n    panes in two different sessions maps to `None` rather than to whichever pane tmux\n    listed last: the notification is still worth sending, but nothing here can say which\n    of the two it belongs to, and guessing would land a tap in the wrong conversation.'
    try:
        output = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{pane_current_path}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    directories = {}
    for line in output.splitlines():
        marker = line.find(":/")
        if marker == -1:
            continue
        session, directory = line[:marker], line[marker + 1:]
        if directory in directories and directories[directory] != session:
            directories[directory] = None
        else:
            directories.setdefault(directory, session)
    return directories


def load_state():
    try:
        with open(STATE) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".partial"
    with open(tmp, "w") as handle:
        json.dump(state, handle)
    os.replace(tmp, STATE)


def transcripts():
    'Every session file on this machine, whichever client wrote it.\n\n    Two roots because two clients keep sessions here and both are agents somebody is\n    waiting on. They are the same shape to walk — a tree of `.jsonl` — and different\n    shapes to read, which `new_messages` handles per file rather than per root.'
    for root in (PROJECTS, PI_SESSIONS):
        for directory, _dirs, files in os.walk(root):
            for name in files:
                if name.endswith(".jsonl"):
                    yield os.path.join(directory, name)


def is_pi(path):
    return path.startswith(PI_SESSIONS + os.sep)







_pi_cwds = {}


def pi_cwd(path):
    if path in _pi_cwds:
        return _pi_cwds[path]
    cwd = None
    try:
        with open(path, "rb") as handle:
            first = handle.readline()
        entry = json.loads(first)
        if entry.get("type") == "session":
            cwd = entry.get("cwd")
    except (OSError, ValueError):
        cwd = None
    
    
    _pi_cwds[path] = cwd
    return cwd


def new_messages(path, offset, pi=False):
    'Whatever was appended since `offset`, as (cwd, text) for agent prose only.\n\n    A truncated final line is left for the next pass rather than parsed: transcripts are\n    appended to while this reads them, and half a line is not an error, it is timing.'
    messages = []
    cwd = None
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size < offset:  
            return [], size, None
        handle.seek(offset)
        data = handle.read()

    consumed = offset
    for chunk in data.split(b"\n"):
        if not chunk:
            consumed += 1
            continue
        try:
            entry = json.loads(chunk)
        except ValueError:
            break  
        consumed += len(chunk) + 1
        cwd = entry.get("cwd", cwd)
        
        
        
        
        if pi:
            if entry.get("type") != "message":
                continue
            if ((entry.get("message") or {}).get("role")) != "assistant":
                continue
        elif entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        text = "\n".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        ).strip()
        if text:
            messages.append(text)
    return messages, min(consumed, offset + len(data)), cwd


def notify(relay, wake_key, title, text, dry_run, host="", session=None):
    line = f"{title}: {text}"
    if dry_run:
        named = f"  [{host}/{session}]" if host and session else ""
        print(f"would notify — {line[:160]}{named}")
        return
    body = {
        "wakeKey": wake_key,
        "kind": "alert",
        "text": line[:300],
    }
    
    
    
    
    
    if host and session:
        body["host"] = host
        body["session"] = session
    payload = json.dumps(body).encode()
    
    
    
    
    
    
    request = urllib.request.Request(
        f"{relay.rstrip('/')}/wake",
        data=payload,
        
        
        
        headers={
            "content-type": "application/json",
            "user-agent": "agent-notify-watch (example-project)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            answer = json.loads(response.read() or b"{}")
        
        if answer.get("ok") is False:
            print(f"agent-notify-watch: relay refused: {answer.get('reason', answer)}",
                  file=sys.stderr)
    
    
    
    
    
    
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf8", "replace")[:200]
        print(f"agent-notify-watch: relay refused: HTTP {error.code}: {detail}",
              file=sys.stderr)
    except urllib.error.URLError as error:
        print(f"agent-notify-watch: relay refused: {error}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(prog="agent-notify-watch")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent and send nothing")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args()

    config = read_config()
    relay = config.get("AGENTORCHESTRA_RELAY", "")
    wake_key = config.get("AGENTORCHESTRA_WAKE_KEY", "")
    
    
    
    
    
    
    
    host_alias = config.get("AGENTORCHESTRA_HOST_ALIAS", "") or platform.node().split(".")[0]
    if not args.dry_run and not (relay and wake_key):
        sys.exit(f"agent-notify-watch: set AGENTORCHESTRA_RELAY and AGENTORCHESTRA_WAKE_KEY in {CONFIG}")

    state = load_state()
    while True:
        muted = muted_terms()
        live = live_directories()
        for path in transcripts():
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if path not in state:
                
                
                state[path] = size
                continue
            if size == state[path]:
                continue

            pi = is_pi(path)
            messages, offset, cwd = new_messages(path, state[path], pi=pi)
            state[path] = offset
            
            if pi and not cwd:
                cwd = pi_cwd(path)
            if not messages or not cwd or cwd not in live:
                continue
            project = os.path.basename(cwd.rstrip("/"))
            if project in muted or path in muted or cwd in muted:
                continue
            notify(relay, wake_key, project, messages[-1], args.dry_run,
                   host=host_alias, session=live[cwd])

        save_state(state)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
