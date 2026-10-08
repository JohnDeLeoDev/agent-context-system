#!/usr/bin/env python3

'PostToolUse(Bash): a search whose output was truncated cannot support an\nabsence claim.\n\nWhy. When a search is piped into `head`, the truncated result can be read as the\nwhole search space, and "this does not exist" gets stated on the strength of it.\nA truncated view and an empty result look identical once the output scrolls past.\nSo this blocks nothing (truncating a search is usually right); it only refuses to\nlet the truncation go unnoticed at the moment it happens.\n\nFires only when both halves are present: a search command, and a truncating\npipe. A plain `head` on a file is somebody reading a file and is\nblock-shell-file-read\'s business, not this one\'s.\n\nOne pipeline at a time. Matching the whole string would warn on\n`ls ... | head -40; find ...`: the head truncates a listing, and\nthe find in the next command is not truncated. A search is\ntruncated only when the truncating pipe comes after it in the same pipeline.\n\nA command that also counts (`| wc -l`) has already taken the remedy this hook\nwould prescribe; warning anyway is the noise that gets a warner skimmed.\n\nSay it in full once per session, then one line. The full paragraph is ~120\ntokens; a fresh session gets it again, everything after the first firing gets\none line. Fails open (full text) on any bookkeeping error.\n\nDelivery is additionalContext. A PostToolUse hook that exits 0 and\nwrites to stderr is talking to a log file and never reaches the model.'
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp



SEARCH = re.compile(r"(?<![\w-])(git\s+grep|grep|rg|ripgrep|ag|find)(?![\w-])")

TRUNCATE = re.compile(r"\|\s*(head|tail)(?![\w-])|\|\s*sed\s+-n")

FULL_MESSAGE = (
    "warn-truncated-search: this %s was piped into head/tail, so the output is the "
    "first few matches, not the whole search space.\n"
    "Fine for looking around, but not evidence of absence.\n"
    "Before claiming something is missing, re-run without head/tail or append "
    "`| wc -l`, and state the absence only from that.\n"
)

BRIEF_MESSAGE = (
    "warn-truncated-search: this %s was piped into head/tail again, "
    "not evidence of absence. Re-run without the pipe or with `| wc -l` before "
    "claiming something is missing.")


def main():
    raw = sys.stdin.read()

    
    if not any(w in raw for w in ("head", "tail", "sed")):
        return 0

    try:
        data = json.loads(raw)
        args = data.get("tool_input") or data.get("tool_args") or data.get("params") or {}
        cmd = (args.get("command") if isinstance(args, dict) else "") or data.get("command") or ""
    except Exception:
        return 0                     
    if not cmd:
        return 0

    
    bare = re.sub(r"'[^']*'", "''", cmd)
    bare = re.sub(r'"[^"]*"', '""', bare)

    hit = None
    for pipeline in re.split(r"\n|;|&&|\|\|", bare):
        s, t = SEARCH.search(pipeline), TRUNCATE.search(pipeline)
        if s and t and s.start() < t.start():
            hit = s
            break
    if not hit:
        return 0

    if re.search(r"\|\s*wc(?![\w-])", bare):
        return 0

    tool = " ".join(hit.group(1).split())

    session = (data.get("session_id") or "").strip()
    brief = False
    if session:
        try:
            d = os.path.join(hp.state_dir(), "nudges")
            os.makedirs(d, exist_ok=True)
            stamp = os.path.join(d, "truncated-search-%s" % re.sub(r"[^\w.-]", "_", session))
            brief = os.path.exists(stamp)
            if not brief:
                with open(stamp, "w") as fh:
                    fh.write("1\n")
        except OSError:
            brief = False

    if brief:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": BRIEF_MESSAGE % tool}}))
        return 0

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": FULL_MESSAGE % tool}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        sys.exit(0)
