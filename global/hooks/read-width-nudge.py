#!/usr/bin/env python3
'PostToolUse(Read): (1) LOG every read, (2) nudge toward NARROW reads.\n\nWHY. Read is ~77% of this fleet\'s amortized context cost. A read is not paid\nonce: it is cache-WRITTEN, then cache-READ again on every subsequent request\nin the session, so a 900-line file read at turn 3 is still being paid for at\nturn 60.\n\nLOGGING happens before any early exit, including the bounded reads that are\nthe outcome wanted, so "is this working?" stays a computable question (bounded\nshare over time, whether a nudged file gets read bounded next, whole-file\nreads by line count).\n\nDeliberately a NUDGE, never a block: a legitimate whole-file read exists (a\nfile about to be rewritten, a config that must be seen entire), and a guard\nthat fires on honest work gets switched off. Warns at most once per file and\nat most 10 times per session, then goes quiet.\n\nIts sibling block-redundant-read covers the OTHER half: reading the same file\nTWICE. Width and duplication are separate failures, so neither threshold has\nto compromise for the other.\n\nATTRIBUTING A READ TO A WORKER (`agent_id` present) rather than the\norchestrator is the whole question behind "should discovery move to\nworker-explore?". `session_id` is NOT the discriminator: a subagent\'s read\narrives under the PARENT\'s session_id.\n\nTHE LOG GROUPS PATHS THE SAME WAY THE GUARD DOES. block-redundant-read\ncollapses `/.agents/worktrees/<name>/` and legacy `/.claude/worktrees/<name>/` before metering, because the same\nsource file is read at both the worktree path and the main-checkout path\nunder the worktree mandate. This telemetry does the same, recording the\nworktree name (`wt`) separately so "which branch was this read from" stays\nanswerable without fragmenting every per-file total.'
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

EXEMPT_EXT = (".ipynb", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".svg", ".ico")
LOG_ROTATE_BYTES = 20 * 1024 * 1024
WIDTH_THRESHOLD = 300
WIDTH_STATE_ROOT = os.path.expanduser("~/.local/state/agent-context/read-width")
LOG_DIR = os.path.expanduser("~/.local/state/agent-context")
LOG_PATH = os.path.join(LOG_DIR, "read-telemetry.jsonl")
RETIRE_AGE_SECONDS = 7 * 86400
SESSION_WARN_BUDGET = 10


def _posix_cksum(data):
    'POSIX `cksum` CRC (CRC-32/CKSUM), used only as a stable per-path key.'
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    length = len(data)
    while length:
        b = length & 0xFF
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        length >>= 8
    return crc ^ 0xFFFFFFFF


def _cksum_path(p):
    return _posix_cksum(p.encode("utf-8", "surrogateescape"))


def normalize_worktree_path(f):
    "(normalized path, worktree name or ''). See block-redundant-read's\n    identical normalization -- the same file read at\n    .agents/worktrees/<name>/<path> (or legacy .claude/worktrees/) and at <path> in the main checkout must\n    group together."
    marker = next((mark for mark in ("/" + scope + "/worktrees/" for scope in hp.HARNESS_DIRNAMES) if mark in f), None)
    i = f.find(marker) if marker else -1
    if i == -1:
        return f, ""
    rest = f[i + len(marker):]
    j = rest.find("/")
    if j == -1:
        return f, ""
    wt = rest[:j]
    return f[:i] + "/" + rest[j + 1:], wt


def _count_lines(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().count(b"\n")
    except OSError:
        return 0


def _file_mtime(path):
    try:
        return int(os.stat(path).st_mtime)
    except OSError:
        return 0


def _retire_old(root):
    try:
        cutoff = datetime.datetime.now().timestamp() - RETIRE_AGE_SECONDS
        for name in os.listdir(root):
            p = os.path.join(root, name)
            try:
                if os.path.isdir(p) and os.stat(p).st_mtime < cutoff:
                    import shutil
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        ti = {}
    f = ti.get("file_path") or ""
    off = ti.get("offset")
    lim = ti.get("limit")
    agent = payload.get("agent_id") or ""
    sid = payload.get("session_id")
    if not sid:
        sid = "nosession"

    if not f or not os.path.isfile(f):
        return 0
    if f.endswith(EXEMPT_EXT):
        return 0

    bounded = 1 if (off not in (None, "") or lim not in (None, "")) else 0
    lines = _count_lines(f)
    sub = 1 if agent else 0

    norm, wt = normalize_worktree_path(f)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        pass
    
    try:
        if os.path.getsize(LOG_PATH) > LOG_ROTATE_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass

    mtime = _file_mtime(f)
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sid": sid, "agent": agent, "file": norm, "wt": wt, "lines": lines,
                "bounded": bounded, "sub": sub, "mtime": mtime}) + "\n")
    except OSError:
        pass

    
    if bounded:
        return 0

    if lines < WIDTH_THRESHOLD:
        return 0

    state = os.path.join(WIDTH_STATE_ROOT, sid)
    try:
        os.makedirs(state, exist_ok=True)
    except OSError:
        return 0

    key = str(_cksum_path(f))
    marker = os.path.join(state, key)
    if os.path.exists(marker):
        return 0                                  

    try:
        count = len([p for p in os.listdir(state) if os.path.isfile(os.path.join(state, p))])
    except OSError:
        count = 0
    if count >= SESSION_WARN_BUDGET:
        return 0                                  
    if count == 0:
        _retire_old(WIDTH_STATE_ROOT)

    try:
        with open(marker, "w"):
            pass
    except OSError:
        pass

    print(json.dumps({"systemMessage": (
        "Read %s whole -- %d lines, no offset/limit. Read is ~77%% of this fleet's "
        "amortized context cost, and a read is re-paid on every later request in the "
        "session, not just this one. For ONE region: LSP or Grep for the anchor, then "
        "Read with offset+limit. For breadth across many files: delegate to "
        "worker-explore, which returns the conclusion. If you "
        "need this file entire, carry on -- this will not ask twice."
    ) % (f, lines)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        sys.exit(0)
