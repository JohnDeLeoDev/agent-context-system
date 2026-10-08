#!/usr/bin/env python3
"git merge driver for audit observation records, global/audit-observations/NNNN.json.\n\nMerges field by field against the ancestor, and resolves only the fields that are\nappend-only or monotonic. Git's line merge conflicts on the notes and updated_at lines\neven when both sides agree on the status, which stops a machine's sync until the record\nis merged by hand. A disagreement between two sessions (resolved on one machine,\nre-triaged on another) still needs a person.\n\nPer field, with O the ancestor, A ours, B theirs:\n  - equal on both sides, or changed on one side only: take the changed value\n  - notes (timestamped entries appended by update_audit_observation): the union of both\n    sides' entries, in timestamp order\n  - evidence (segments joined by a --- line): A's segments, then B's new ones\n  - updated_at, last_seen: the later stamp\n  - recurrences: the ancestor plus both sides' increments. An equal value on both sides\n    counts once, the way git treats an identical change, because a replayed commit\n    (a rebase copy of the other side's bump) looks exactly like that.\nRefuses (exit 1, so git reports the conflict as before) when:\n  - either side is not a JSON object, or the ids differ\n  - any other field changed on both sides to different values (status, severity,\n    resolution_note and the rest): two sessions decided different things, a person picks\n\nInvoked by git as:  merge-observation.py %O %A %B %P\n  %O ancestor, %A ours (also the output file), %B theirs, %P path.\nOutput matches audit.py: json indent=1, ensure_ascii=False, trailing newline.\nRegistered per machine by the daemon, through register-merge-drivers.py. Python 3.8-safe: the Synology nodes run\nhooks on the system 3.8.15.\n\nObservations guarded: #366."
import json
import re
import sys

_MISSING = object()
_ENTRY = re.compile(r"\n(?=\[\d{4}-\d{2}-\d{2}T)")
_STAMP = re.compile(r"\[(\d{4}-\d{2}-\d{2}T[^\]]*)\]")


class Refuse(Exception):
    pass


def _load(path, allow_empty=False):
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        raise Refuse()
    if allow_empty and not text.strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        raise Refuse()
    if not isinstance(value, dict):
        raise Refuse()
    return value


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _merge_notes(a, b):
    if a.startswith(b):
        return a
    if b.startswith(a):
        return b
    seen, lead, entries = set(), [], []
    for side in (a, b):
        for i, chunk in enumerate(_ENTRY.split(side)):
            if chunk in seen:
                continue
            seen.add(chunk)
            m = _STAMP.match(chunk)
            if m:
                entries.append((m.group(1), chunk))
            elif i == 0:
                lead.append(chunk)
            else:
                raise Refuse()
    if len(lead) > 1:
        raise Refuse()
    entries.sort(key=lambda e: e[0])
    return "\n".join(lead + [c for _, c in entries])


def _merge_evidence(a, b):
    if a.startswith(b):
        return a
    if b.startswith(a):
        return b
    sep = "\n---\n"
    parts = a.split(sep)
    parts += [p for p in b.split(sep) if p not in parts]
    return sep.join(parts)


def merge(o, a, b):
    if a.get("id") != b.get("id"):
        raise Refuse()
    out = {}
    for key in list(a) + [k for k in b if k not in a]:
        ov, av, bv = o.get(key, _MISSING), a.get(key, _MISSING), b.get(key, _MISSING)
        if av == bv or bv == ov:
            value = av
        elif av == ov:
            value = bv
        elif key in ("notes", "evidence") and isinstance(av, str) and isinstance(bv, str):
            value = _merge_notes(av, bv) if key == "notes" else _merge_evidence(av, bv)
        elif key in ("updated_at", "last_seen") and isinstance(av, str) and isinstance(bv, str):
            value = max(av, bv)
        elif key == "recurrences" and _int(av) and _int(bv):
            base = ov if _int(ov) else 1
            value = av + bv - base
        else:
            raise Refuse()
        if value is not _MISSING:
            out[key] = value
    return out


def main(argv):
    if len(argv) < 5:
        return 1
    try:
        ancestor = _load(argv[1], allow_empty=True)
        ours = _load(argv[2])
        theirs = _load(argv[3])
        merged = merge(ancestor, ours, theirs)
    except Refuse:
        return 1
    with open(argv[2], "w", encoding="utf-8") as fh:
        fh.write(json.dumps(merged, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)
