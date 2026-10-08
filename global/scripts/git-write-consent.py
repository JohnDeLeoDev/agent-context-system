#!/usr/bin/env python3
'git-write-consent.py -- YOU (the human) run this to authorize ONE git write that\nguard-git-write.py would otherwise block.\n\nUsage:\n  python3 ~/.agent-context/global/scripts/git-write-consent.py              # 10 min, any repo\n  python3 ~/.agent-context/global/scripts/git-write-consent.py 30           # 30 min, any repo\n  python3 ~/.agent-context/global/scripts/git-write-consent.py 10 .         # 10 min, THIS repo only\n  python3 ~/.agent-context/global/scripts/git-write-consent.py --revoke     # cancel an unused token\n  python3 ~/.agent-context/global/scripts/git-write-consent.py --log        # what it has authorized'
import os
import subprocess
import sys
import time
import datetime





HELP_TEXT = __doc__

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import git_write_token  

STATE_DIR = git_write_token.state_dir()
TOKEN = git_write_token.path()
LOG = os.path.join(STATE_DIR, "git-write-consent.log")


def _hhmmss(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def _arg_or_default(argv, index, default):
    if len(argv) > index and argv[index] != "":
        return argv[index]
    return default


def main(argv):
    os.makedirs(STATE_DIR, exist_ok=True)

    first = argv[0] if argv else ""
    if first in ("--revoke", "-r"):
        try:
            os.remove(TOKEN)
        except FileNotFoundError:
            pass
        print("consent token revoked (none may have been active).")
        return 0
    if first in ("--log", "-l"):
        if os.path.isfile(LOG) and os.path.getsize(LOG) > 0:
            print("== git writes authorized by consent token ==")
            with open(LOG, encoding="utf-8", errors="replace") as fh:
                sys.stdout.write(fh.read())
        else:
            print("no consent token has ever been used.")
        return 0
    if first in ("--help", "-h"):
        print(HELP_TEXT)
        return 0

    mins = _arg_or_default(argv, 0, "10")
    if mins == "" or not mins.isdigit():
        print("error: minutes must be a whole number (got '%s')" % mins, file=sys.stderr)
        return 1
    minutes = int(mins)
    if minutes < 1 or minutes > 120:
        print("error: minutes must be 1..120", file=sys.stderr)
        return 1

    scope = "any"
    arg2 = argv[1] if len(argv) > 1 else ""
    if arg2 != "":
        proc = subprocess.run(["git", "-C", arg2, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True)
        if proc.returncode != 0 or not proc.stdout:
            print("error: '%s' is not inside a git repo" % arg2, file=sys.stderr)
            return 1
        scope = proc.stdout.rstrip("\n")

    expires = int(time.time()) + minutes * 60
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(TOKEN, "w", encoding="utf-8") as fh:
        fh.write("expires=%s\nscope=%s\ncreated=%s\n" % (expires, scope, created))
    try:
        os.chmod(TOKEN, 0o600)
    except OSError:
        pass

    print("Authorized ONE git commit/push/merge that the worktree guard would block.")
    print("  valid for : %s minute(s)  (until %s)" % (mins, _hhmmss(expires)))
    print("  scope     : %s" % scope)
    print("  uses      : 1 (consumed on the first allowed write)")
    print("  log       : %s" % LOG)
    print()
    print("Revoke early with: python3 %s --revoke" % sys.argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
