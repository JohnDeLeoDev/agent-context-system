#!/usr/bin/env python3
'health-record — turn a "(non-fatal)" branch into something a session can see.\n\nThe materialize chain is full of lines shaped like:\n\n    python3 "$CL/scripts/harness-materialize.py" || echo "... skipped (non-fatal)" >&2\n\nThat pattern is right about not aborting SessionStart and wrong about everything\nafter: the message goes to a stderr stream nothing reads, the hook exits 0, and the\nsession begins on a half-projected config that looks entirely normal. Audit\nobservations #215 and #222 are both this shape -- a change written to a materialized\nfile that is silently reverted, and a hook projected but never registered while the\nsyncer reports "already in sync".\n\n"Non-fatal" should mean "does not abort the session", never "nobody ever hears about\nit". So each of those branches now ALSO records the failure here, where\npreflight-core-health.py finds it and puts it in the next session\'s context.\n\nSuccess must record too (--ok), because a stale failure file is its own silent lie:\nwithout a clear-on-success the first bad session would haunt every later one until\nsomeone deleted the file by hand.\n\nUsage:\n  health-record.py <component> --fail "<what went wrong>"\n  health-record.py <component> --fail "<whole state>" --replace\n  health-record.py <component> --ok'
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

STATE = os.path.join(hp.state_dir(), "health")


def fingerprint(detail):
    'Identity of a FINDING, as opposed to identity of a sentence.\n\n    That is not a cosmetic problem. This banner is the fleet\'s loudest signal\n    and its entire value is that a reader believes the count, so a subsystem\n    that inflates its own findings is destroying the thing it exists to\n    provide.\n\n    So findings are matched on their first line with every run of digits\n    collapsed, and the NEWEST wording wins -- the stale "6 minutes ago" is\n    replaced rather than kept beside its own successor.'
    head = (detail or "").strip().splitlines()
    return re.sub(r"\d+", "#", head[0]).strip().lower() if head else ""


def main(argv):
    if len(argv) < 3:
        return 2
    component = argv[1]
    path = os.path.join(STATE, "%s.json" % component.replace("/", "-"))

    
    
    
    
    
    replace = "--replace" in argv[2:]
    args = [a for a in argv[2:] if a != "--replace"]
    if not args:
        return 2

    if args[0] == "--ok":
        try:
            os.remove(path)
        except OSError:
            pass
        return 0

    if args[0] != "--fail" or len(args) < 2:
        return 2

    os.makedirs(STATE, exist_ok=True)
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        rec = {}

    detail = args[1]
    
    failures = [] if replace else (rec.get("failures") or [])
    fp = fingerprint(detail)
    failures = [f for f in failures if fingerprint(f) != fp]
    failures.append(detail)

    rec.update({"component": component, "ok": False, "ts": int(time.time()),
                
                "since": rec.get("since") or int(time.time()),
                "failures": failures[-10:]})
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh, indent=2)
    os.replace(tmp, path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
