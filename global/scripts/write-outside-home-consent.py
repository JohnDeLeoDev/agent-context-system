#!/usr/bin/env python3
'write-outside-home-consent.py: YOU (the human) run this to approve agent writes\nunder one directory outside your home directory, for a limited time.\n\nblock-write-outside-home refuses every agent write outside $HOME. This is the\napproval route. A grant covers the directory and everything below it, lasts the\ngiven minutes (default 10, max 240), and every write it allows is logged.\nAgents ask through AskUserQuestion, and the approval-question hook runs this when\nyou pick Approve; block-consent-self-grant refuses an agent that runs it itself.\nLike git-write-consent, this buys friction and an audit trail, not cryptographic\nauthority.\n\nUsage:\n  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py /tmp/build       # 10 min\n  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py /tmp/build 60    # 60 min\n  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py --list           # active grants\n  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py --revoke         # revoke all\n  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py --log            # grants and writes'
import os
import re
import sys
import time
import datetime





HELP_TEXT = """write-outside-home-consent.py: YOU (the human) run this to approve agent writes
under one directory outside your home directory, for a limited time.

block-write-outside-home refuses every agent write outside $HOME. This is the
approval route. A grant covers the directory and everything below it, lasts the
given minutes (default 10, max 240), and every write it allows is logged.
Agents ask through AskUserQuestion, and the approval-question hook runs this when
you pick Approve; block-consent-self-grant refuses an agent that runs it itself. Like git-write-consent, this buys friction and an audit trail, not
cryptographic authority.

Usage:
  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py /tmp/build       # 10 min
  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py /tmp/build 60    # 60 min
  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py --list           # active grants
  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py --revoke         # revoke all
  python3 ~/.agent-context/global/scripts/write-outside-home-consent.py --log            # grants and writes
"""

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.join(os.environ.get("HOME", ""), ".local", "state"),
    "agent-context")
GRANTS = os.path.join(STATE_DIR, "write-outside-home-consent")
LOG = GRANTS + ".log"


def _hhmmss(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def _now_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _awk_leading_number(text):
    m = re.match(r"\s*[+-]?(\d+\.?\d*|\.\d+)", text)
    return float(m.group()) if m else 0.0


def main(argv):
    os.makedirs(STATE_DIR, exist_ok=True)
    now = int(time.time())

    first = argv[0] if argv else ""
    if first in ("", "--help", "-h"):
        print(HELP_TEXT)
        return 0
    if first in ("--revoke", "-r"):
        try:
            os.remove(GRANTS)
        except FileNotFoundError:
            pass
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%s\tREVOKE\tall\n" % _now_stamp())
        print("All write-outside-home grants revoked.")
        return 0
    if first in ("--log", "-l"):
        if os.path.isfile(LOG) and os.path.getsize(LOG) > 0:
            with open(LOG, encoding="utf-8", errors="replace") as fh:
                sys.stdout.write(fh.read())
        else:
            print("No grant has been created yet.")
        return 0
    if first == "--list":
        found = False
        if os.path.isfile(GRANTS) and os.path.getsize(GRANTS) > 0:
            with open(GRANTS, encoding="utf-8", errors="replace") as fh:
                for line in fh.read().splitlines():
                    fields = line.split("\t")
                    exp = fields[0] if len(fields) > 0 else ""
                    directory = fields[1] if len(fields) > 1 else ""
                    if exp == "" or not exp.isdigit():
                        continue
                    if int(exp) > now:
                        found = True
                        print("%s  until %s" % (directory, _hhmmss(int(exp))))
        if not found:
            print("No active grants.")
        return 0

    directory = argv[0]
    mins = argv[1] if len(argv) > 1 and argv[1] != "" else "10"
    if mins == "" or not mins.isdigit():
        print("error: minutes must be a whole number (got '%s')" % mins, file=sys.stderr)
        return 1
    minutes = int(mins)
    if minutes < 1 or minutes > 240:
        print("error: minutes must be 1..240", file=sys.stderr)
        return 1

    real = os.path.realpath(os.path.expanduser(directory))
    home_real = os.path.realpath(os.path.expanduser("~"))
    if real == "/":
        print("error: approve a specific directory, not /", file=sys.stderr)
        return 1
    if real == home_real or real.startswith(home_real + os.sep):
        print("%s is inside your home directory. No grant is needed." % real)
        return 0

    expires = now + minutes * 60
    tmp = "%s.new.%d" % (GRANTS, os.getpid())
    kept_lines = []
    if os.path.isfile(GRANTS) and os.path.getsize(GRANTS) > 0:
        with open(GRANTS, encoding="utf-8", errors="replace") as fh:
            for line in fh.read().splitlines():
                fields = line.split("\t")
                if fields and _awk_leading_number(fields[0]) > now:
                    kept_lines.append(line)
    with open(tmp, "w", encoding="utf-8") as fh:
        for line in kept_lines:
            fh.write(line + "\n")
        fh.write("%s\t%s\t%s\n" % (expires, real, _now_stamp()))
    os.replace(tmp, GRANTS)
    try:
        os.chmod(GRANTS, 0o600)
    except OSError:
        pass
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("%s\tGRANT\t%s\t%s min\n" % (_now_stamp(), real, mins))

    print("Approved agent writes under: %s" % real)
    print("  valid for : %s minute(s)  (until %s)" % (mins, _hhmmss(expires)))
    print("  log       : %s" % LOG)
    print()
    print("Revoke early with: python3 %s --revoke" % sys.argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
