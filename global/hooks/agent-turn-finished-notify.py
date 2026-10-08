#!/usr/bin/env python3
"Stop: the turn ended. Tell the phone -- and say WHICH conversation ended.\n\nThis exists because nothing on the fleet could say it. `agent-notify-watch` pushes\nwhenever an agent writes prose, so a phone got a running commentary and never a\ncompletion: mid-turn output and a finished turn were the same event to every notifier\nthis system had. The Stop event IS the turn ending, and only a hook can see it.\n\nDETACHED, and fail-silent on every path, for `token-usage-collect`'s reason: this\nsits between the person pressing enter and getting their turn back, and a\ntelemetry-shaped hook that can fail a turn is worse than no hook at all. The relay\ncall is a network round trip and must never be on that path."
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp


def main():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    script = os.path.join(hp.scripts_dir(home), "agent-event-notify.py")
    if not os.path.isfile(script):
        return 0

    py = shutil.which("python3")
    if not py:
        return 0

    
    
    
    payload = sys.stdin.read()
    try:
        proc = subprocess.Popen(
            [py, script, "--event", "turn-finished"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            if proc.stdin is not None:
                proc.stdin.write(payload.encode("utf-8", "replace"))
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
