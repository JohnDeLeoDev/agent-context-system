#!/usr/bin/env python3
'state-migrate.py: move the legacy ~/.claude/state into the neutral state dir.\n\nState and health files belong to the agent-context store, not to one harness, so they live\nin harness_paths.state_dir() (~/.local/state/agent-context). Older machines still hold them\nunder ~/.claude/state. This moves them over once and leaves no symlink behind.\n\nRules:\n  - A name only the old dir holds is moved.\n  - A directory on both sides is merged, recursively.\n  - A file on both sides: the newer mtime wins. The loser is dropped and a note says so.\n  - A file against a directory (a type clash) is left where it is and reported.\n  - The old dir is removed only when it ends up empty.\n  - A second run finds no old dir and does nothing.\n\nUsage:  python3 state-migrate.py        # prints what it moved; exit 0'
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp


def _is_dir(path):
    return os.path.isdir(path) and not os.path.islink(path)


def _merge(src, dst, rel, notes, counter):
    'Move the entries of src into dst. Returns nothing; notes and counter collect results.'
    for name in sorted(os.listdir(src)):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        label = os.path.join(rel, name)
        try:
            if not os.path.lexists(d):
                shutil.move(s, d)
                counter[0] += 1
            elif _is_dir(s) and _is_dir(d):
                _merge(s, d, label, notes, counter)
                if not os.listdir(s):
                    os.rmdir(s)
            elif _is_dir(s) or _is_dir(d):
                notes.append(f"clash {label}: a file and a directory share the name; left both")
            elif os.lstat(s).st_mtime > os.lstat(d).st_mtime:
                os.replace(s, d)
                counter[0] += 1
                notes.append(f"conflict {label}: the old file was newer and replaced the new one")
            else:
                os.unlink(s)
                notes.append(f"conflict {label}: kept the newer file in the new state dir")
        except OSError as e:
            notes.append(f"FAILED {label}: {e}")


def migrate(home=None):
    'Move the legacy state dir into the new one. Returns a list of report lines.'
    legacy = os.path.join(hp.claude_home(home), "state")
    dst = hp.state_dir(home)
    if not _is_dir(legacy):
        return []
    notes = []
    counter = [0]
    os.makedirs(dst, exist_ok=True)
    _merge(legacy, dst, "", notes, counter)
    try:
        os.rmdir(legacy)
    except OSError:
        notes.append(f"left {legacy}: not empty after the merge")
    notes.insert(0, f"state-migrate: moved {counter[0]} entr{'y' if counter[0] == 1 else 'ies'} "
                    f"from {legacy} to {dst}")
    return notes


def main():
    for line in migrate():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
