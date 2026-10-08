#!/usr/bin/env python3
'locked-test-drift-gate: a turn may not end while a locked test differs from its lock.\n\nCANNOT TRAP A SESSION. It never blocks on stop_hook_active, and it blocks once per\ndistinct drift: the same files with the same content as at the last block let the\nturn end. Every failure path allows.'

import datetime
import hashlib
import importlib.util
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp
import store_task
import transcript_records

SCRIPTS = hp.scripts_dir()
LOCK_TOOL = os.environ.get("TEST_LOCK_TOOL") or os.path.join(SCRIPTS, "test-lock.py")
JUDGE = (os.environ.get("VERIFICATION_CLAIM_JUDGE")
         or os.path.join(SCRIPTS, "verification-claim-judge.py"))
STATE_DIR = os.environ.get("LOCKED_TEST_GATE_STATE_DIR") or os.path.join(
    hp.state_dir(), "locked-test-drift-gate")

REASON = """This turn cannot end: locked acceptance tests differ from their locks.

%s

The tests were agreed before implementation. Edit each file back to its locked
content, then make the code pass. `git diff -- <file>` shows the change when the
locked version is committed. Check with:

    python3 ~/.agent-context/global/scripts/test-lock.py status <checkout>

If a test is wrong, say which assertion and why, then ask with one AskUserQuestion:
header "Approval", options "Approve" and "Deny", and the question:

    Unlock the locked test <absolute path>? [approval:test-unlock:<absolute path>:0]

The approval-question hook removes the lock when user picks Approve.
"""


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tool_strings(records):
    'Every string argument of every tool call in these records.'
    found = []
    for rec in records:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                continue
            inp = blk.get("input")
            if isinstance(inp, dict):
                found += [v for v in inp.values() if isinstance(v, str)]
    return found


def tool_records(transcript):
    'Every record in the whole transcript that carries a tool call.'
    found = []
    for rec in transcript_records.records(transcript):
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
            found.append(rec)
    return found


def transcript_mentions(transcript, text):
    'True when `text` appears anywhere in the transcript file: a byte search, a cheap\n    superset of it appearing in a tool argument. True when the file cannot be read, so a\n    doubt costs the daemon call, never a missed drift.'
    try:
        with open(transcript, "rb") as fh:
            return os.fsencode(text) in fh.read()
    except (OSError, TypeError, ValueError):
        return True


def epoch(stamp):
    'Seconds since the epoch for a transcript timestamp, or None.'
    if not isinstance(stamp, str) or not stamp:
        return None
    text = stamp[:-1] if stamp.endswith("Z") else stamp
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            moment = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return moment.replace(tzinfo=datetime.timezone.utc).timestamp()
    return None


def works_in(root, cwd, turn_named):
    if cwd == root or cwd.startswith(root + os.sep):
        return True
    if root.startswith(cwd + os.sep) and os.path.isdir(os.path.join(cwd, ".git")):
        return True
    return any(root in value for value in turn_named)


_DRIFT_LINE = re.compile(r"^(\S+)\s+(.*)$")


def _live_drift(store):
    ' live drift.'
    try:
        result = store_task.run("test-lock", ["status", store])
    except (store_task.store_mcp.StoreUnreachable, store_task.store_mcp.ToolError):
        return []
    if not isinstance(result, dict):
        return []
    out = []
    for line in (result.get("stdout") or "").splitlines():
        m = _DRIFT_LINE.match(line)
        if m and m.group(1) in ("changed", "missing"):
            out.append((m.group(2), m.group(1)))
    return out


def made_here(path, works_here, named, start):
    'Did this session make this drift? See SCOPE in the module docstring.'
    if any(path in value for value in named):
        return True
    if not works_here:
        return False
    if start is None:
        return True
    try:
        return os.stat(path).st_mtime >= start - 2
    except OSError:
        return True


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return 0
    session = str(payload.get("session_id") or "")
    if not session:
        return 0
    tl = load_module(LOCK_TOOL, "test_lock")
    local_docs = tl.local_manifests() if hasattr(tl, "local_manifests") else tl.manifests()
    store = tl.store_root() if hasattr(tl, "store_root") else None
    cwd = os.path.realpath(payload.get("cwd") or os.getcwd())
    transcript = payload.get("transcript_path")
    has_transcript = bool(transcript) and os.path.exists(transcript)
    turn = load_module(JUDGE, "vcj").previous_turn(transcript) if has_transcript else []
    stamps = [t for t in (epoch(r.get("timestamp")) for r in turn) if t is not None]
    start = min(stamps) if stamps else None
    turn_named = tool_strings(turn)
    
    
    named_once = []

    def named():
        if not named_once:
            named_once.append(tool_strings(tool_records(transcript)) if has_transcript else [])
        return named_once[0]

    drifted = []
    if store:
        
        
        here = works_in(store, cwd, turn_named)
        
        
        touched = here or (has_transcript and transcript_mentions(transcript, store))
        for rel, why in _live_drift(store) if touched else ():
            path = os.path.join(store, rel)
            if made_here(path, here, named(), start):
                drifted.append((path, why))
    for doc in local_docs:
        root = doc["root"]
        here = works_in(root, cwd, turn_named)
        for rel, why in tl.drift(doc):
            path = os.path.join(root, rel)
            if made_here(path, here, named(), start):
                drifted.append((path, why))
    if not drifted:
        return 0

    signature = hashlib.sha256(json.dumps(
        [[path, why, tl.file_sha256(path)] for path, why in drifted]).encode()).hexdigest()
    ledger = os.path.join(STATE_DIR, session)
    try:
        with open(ledger, encoding="utf-8") as fh:
            if fh.read().strip() == signature:
                return 0
    except OSError:
        pass
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(ledger, "w", encoding="utf-8") as fh:
            fh.write(signature)
    except OSError:
        return 0

    listing = "\n".join("  %-8s %s" % (why, path) for path, why in drifted)
    json.dump({"decision": "block", "reason": REASON % listing}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("locked-test-drift-gate: hook failed, allowing: %r\n" % (exc,))
        sys.exit(0)
