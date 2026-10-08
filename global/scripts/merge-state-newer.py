#!/usr/bin/env python3
'git merge driver for small generated state files under global/state/.\n\nKeeps the side with the newer `last_run` when that stamp is the only difference.\nTwo machines that run context-audit-autorun close together each rewrite\nglobal/state/context-audit.json whole, and the one line that differs is "last_run".\nGit\'s line merge conflicts on it, and every later sync on that machine fails until\nsomeone resolves it by hand.\n\nRefuses (exit 1, so git reports the conflict as before) when:\n  - either side is not a JSON object, or its last_run is not a number\n  - the sides differ in any key other than last_run\n  - either side shows a run in progress (running_since set): a person decides that\n\nInvoked by git as:  merge-state-newer.py %O %A %B %P\n  %O ancestor, %A ours (also the output file), %B theirs, %P path.\nRegistered per machine by the daemon, through register-merge-drivers.py. Python 3.8-safe: the Synology nodes run\nhooks on the system 3.8.15.'
import json
import sys


def _stamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def main(argv):
    if len(argv) < 5:
        return 1
    ours_path, theirs_path = argv[2], argv[3]
    try:
        with open(ours_path, encoding="utf-8") as fh:
            ours = json.load(fh)
        with open(theirs_path, encoding="utf-8") as fh:
            theirs = json.load(fh)
    except (OSError, ValueError):
        return 1
    if not (isinstance(ours, dict) and isinstance(theirs, dict)):
        return 1
    if not (_stamp(ours.get("last_run")) and _stamp(theirs.get("last_run"))):
        return 1
    if ours.get("running_since") or theirs.get("running_since"):
        return 1

    def rest(d):
        return {k: v for k, v in d.items() if k != "last_run"}

    if rest(ours) != rest(theirs):
        return 1
    if theirs["last_run"] > ours["last_run"]:
        with open(ours_path, "w", encoding="utf-8") as fh:
            json.dump(theirs, fh, indent=2)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)
