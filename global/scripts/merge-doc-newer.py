#!/usr/bin/env python3
'Merge driver for generated project docs — `merge=acdocnewer`.\n\nThis is the same class the JSON id-union driver already solved for\n`audit-observations.json`; that file was then split one-per-id and the driver went\nidle, while the conflict class simply moved to these docs and nothing followed it.\n\nWHAT IT DOES, in order:\n\nNever used for global instructions, memories or hooks: a conflict there is two\npeople disagreeing and must surface. Scope lives in `.gitattributes`.\n\nArgs, per gitattributes(5): %O ancestor  %A ours (RESULT is written here)  %B theirs\n                            %P pathname'
import re
import sys

TS = re.compile(r'^updated_at:\s*"?([0-9T:\-]+Z?)"?\s*$', re.M)


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def stamp(text):
    'The updated_at value from frontmatter, or None. Frontmatter only: a match\n    deeper in the body would let prose about a timestamp decide a merge.'
    if not text:
        return None
    head = text[:4000]
    m = TS.search(head)
    return m.group(1) if m else None


def main(argv):
    if len(argv) < 4:
        return 1
    _ancestor, ours_path, theirs_path = argv[1], argv[2], argv[3]
    path = argv[4] if len(argv) > 4 else ours_path

    ours, theirs = read(ours_path), read(theirs_path)
    if ours is None or theirs is None:
        return 1

    o_empty, t_empty = not ours.strip(), not theirs.strip()

    
    
    if o_empty != t_empty:
        winner = theirs if o_empty else ours
        why = "non-empty side (other was truncated)"
    else:
        o_ts, t_ts = stamp(ours), stamp(theirs)
        if not o_ts or not t_ts or o_ts == t_ts:
            sys.stderr.write(
                "merge-doc-newer: %s — cannot order these two sides "
                "(ours=%s theirs=%s); leaving the conflict for a human\n"
                % (path, o_ts or "no updated_at", t_ts or "no updated_at"))
            return 1
        winner = theirs if t_ts > o_ts else ours
        why = "newer updated_at (%s)" % max(o_ts, t_ts)

    try:
        with open(ours_path, "w", encoding="utf-8") as fh:
            fh.write(winner)
    except OSError as exc:
        sys.stderr.write("merge-doc-newer: %s — write failed: %s\n" % (path, exc))
        return 1

    sys.stderr.write("merge-doc-newer: %s — resolved by %s\n" % (path, why))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
