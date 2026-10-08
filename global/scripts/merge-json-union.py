#!/usr/bin/env python3
'git merge driver: union two JSON arrays of objects keyed by "id".\n\nUsed for churny aggregate files (e.g. global/audit-observations.json) that\nmultiple machines/agents append to concurrently — git can\'t auto-merge them,\nwhich used to abort the whole sync. Unioning by id keeps every record.\n\nInvoked by git as:  merge-json-union.py %O %A %B %P\n  %O ancestor, %A ours (ALSO the output file), %B theirs, %P path.\nExit 0 = resolved (result written to %A); non-zero = give up (git reports conflict).'
import json
import sys

_, base, ours_path, theirs_path, path = sys.argv[:5]
try:
    ours = json.load(open(ours_path, encoding="utf-8"))
    theirs = json.load(open(theirs_path, encoding="utf-8"))
    if not (isinstance(ours, list) and isinstance(theirs, list)):
        sys.exit(1)
    by = {}
    
    for rec in theirs + ours:
        i = rec.get("id")
        if i is None:
            sys.exit(1)  
        if i not in by:
            by[i] = rec
        elif rec.get("status") == "resolved" and by[i].get("status") != "resolved":
            by[i] = rec
    merged = [by[k] for k in sorted(by)]
    with open(ours_path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")
    sys.exit(0)
except Exception:
    sys.exit(1)
