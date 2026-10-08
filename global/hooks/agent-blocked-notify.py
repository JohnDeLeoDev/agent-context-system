#!/usr/bin/env python3
'Notification: the agent is waiting on a human. Say so, in the harness\'s own words.\n\nThe Notification event is the only place this is knowable. A transcript watcher\ncannot see it: a permission prompt is a question the harness asks outside the\ntranscript, and an agent that stopped to ask writes nothing new for the watcher to\nnotice -- which is exactly why an agent could sit blocked for an hour and the phone\nstayed silent.\n\nThe event carries the harness\'s `message`, and that is what gets pushed. A generic\n"something needs you" is the notification this was built to stop shipping: the whole\nvalue is in naming what is being asked.\n\nDetached and fail-silent for the same reason as the Stop side -- see\nagent-turn-finished-notify, which this is the other half of.'
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
            [py, script, "--event", "blocked"],
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
