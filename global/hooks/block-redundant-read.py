#!/usr/bin/env python3
'PreToolUse(Read): refuse a read that would add NOTHING to context.\n\nWHY THIS DENIES WHERE ITS PREDECESSOR ONLY WARNED. A subagent runs under the\nPARENT\'s session_id, so a session-keyed ledger could not tell "the orchestrator\nre-reading what it has" from "a fresh worker context reading for the first\ntime" -- denying would have broken delegation. `agent_id` is the discriminator\nthat removed that blocker (present only on subagent calls; absent for the\norchestrator, established by probe rather than inference). If a future harness\nstops sending it, this hook fails toward DENYING subagents, not exempting them.\n\nTHERE IS ALWAYS A LEGAL MOVE: the first two bounded reads of a file are never\ntouched (3rd-4th nudge, 5th refused); a whole-file read is always available\nonce; a file whose mtime+size changed is never a duplicate; the ledger is\ndropped on compaction.\n\nIDENTITY IS THE PATH RELATIVE TO THE CHECKOUT, NOT THE ABSOLUTE PATH. Under the\nworktree mandate the same source file is read at .agents/worktrees/<name>/<path> (or legacy .claude/worktrees/<name>/<path>)\nand at <path> in the main checkout; an absolute-path key would treat those as\nunrelated and neither ledger would ever trip. The worktree segment is\nnormalized out before keying -- but the per-absolute-path SIGNATURE is kept\nseparate, because a worktree copy has its own mtime and a shared signature\nwould read "changed" on every hop and reset the count, defeating the\nnormalization.\n\nA HARD CEILING ON UNBOUNDED READS (>=1200 lines) lives HERE, in PreToolUse,\nrather than in read-width-nudge\'s PostToolUse: by the time a PostToolUse hook\nruns, the read is already paid for, so a deny there costs the full context and\nshows an error on top of it.'
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

STATE_ROOT = os.path.expanduser("~/.local/state/agent-context/read-ledger")
TELEMETRY_LOG = os.path.expanduser("~/.local/state/agent-context/read-telemetry.jsonl")
EXEMPT_EXT = (".ipynb", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".svg", ".ico")
WHOLE_LINE_CEILING = 1200
RETIRE_AGE_SECONDS = 7 * 86400

CEILING_MESSAGE = """Blocked: %(file)s is %(lines)d lines and this read has no offset/limit. A file that
size does not belong in context whole -- it would be re-paid as a cache-read on
every later request of this session.

Locate first, then read the region: the LSP (definition/references/hover) or Grep
for the anchor, then Read("%(file)s", offset=..., limit=...). The first two bounded
reads of a file are always free. For breadth across several files, send it to
worker-explore, which reads in its own context and returns the conclusion.
"""

BOUNDED_DENY_MESSAGE = """Blocked: this is read #%(n)d of %(file)s in one session, and the file has not changed.
Every earlier region is still in the context above -- re-reading re-pays the
cache-write and then a cache-read on every remaining request of the session.

Scroll up and use what you already have. If you need the WHOLE file, ask for it
once without offset/limit. If the file has changed, this counter resets
on its next mtime/size change, and the whole ledger is dropped on compaction.
Sending breadth to worker-explore keeps its reads out of this context.
"""

WHOLE_DENY_MESSAGE = """Blocked: you already read %(file)s whole in this session (%(lines)s lines) and it has
NOT changed since -- same mtime and size. Those lines are already in your context
above; this call would add nothing and would then be cache-read on every remaining
request of the session.

Scroll up and use what you already have.

If you need it again, a BOUNDED read is the move: Read("%(file)s",
offset=..., limit=...). That is also the right call if you only need one region,
which is usually the case. Bounded reads have their own budget -- the first two are
free, then you get a nudge, and the fifth is refused.
"""


def _posix_cksum(data):
    'The POSIX `cksum` CRC (CRC-32/CKSUM: poly 0x04C11DB7, non-reflected,\n    init 0, xorout 0xFFFFFFFF, followed by the byte length fed in little-endian\n    order). Reimplemented rather than shelling out, since this hook has no\n    other reason to spawn a process; only used as a stable ledger key, so exact\n    parity with the shell `cksum` binary is not required for correctness.'
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


def file_sig(path):
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return "%d %d" % (int(st.st_mtime), st.st_size)


def normalize_worktree_path(f):
    marker = next((mark for mark in hp.worktree_marks() if mark in f), None)
    i = f.find(marker) if marker else -1
    if i == -1:
        return f
    rest = f[i + len(marker):]
    j = rest.find("/")
    if j == -1:
        return f
    return f[:i] + "/" + rest[j + 1:]


def _read_int(path, default=0):
    try:
        with open(path) as fh:
            return int(fh.read().strip() or default)
    except (OSError, ValueError):
        return default


def _read_text(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write_text(path, text):
    try:
        with open(path, "w") as fh:
            fh.write(text)
    except OSError:
        pass


def _count_lines(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().count(b"\n")
    except OSError:
        return 0


def _append_jsonl(path, record):
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _retire_old_ledgers(root):
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


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    bounded = 1 if (off not in (None, "") or lim not in (None, "")) else 0

    if not f or not os.path.isfile(f):
        return 0
    if f.endswith(EXEMPT_EXT):
        return 0

    sig = file_sig(f)
    if not sig:
        return 0

    
    
    if bounded == 0:
        whole_lines = _count_lines(f)
        if whole_lines >= WHOLE_LINE_CEILING:
            sys.stderr.write(CEILING_MESSAGE % {"file": f, "lines": whole_lines})
            return 2

    state = os.path.join(STATE_ROOT, sid + (("/agent-" + agent) if agent else ""))
    try:
        os.makedirs(state, exist_ok=True)
    except OSError:
        return 0

    norm = normalize_worktree_path(f)
    key = _posix_cksum(norm.encode("utf-8", "surrogateescape"))
    fsig_key = _cksum_path(f)
    entry = os.path.join(state, str(key))

    
    
    if bounded == 1:
        bentry = os.path.join(state, "b_%d" % key)
        n = _read_int(bentry, 0)
        
        bsigf = "%s.sig.%d" % (bentry, fsig_key)
        if os.path.exists(bsigf):
            prev = _read_text(bsigf)
            if prev != sig:
                n = 0
        _write_text(bsigf, sig + "\n")
        n += 1
        _write_text(bentry, str(n) + "\n")
        if n <= 2:
            return 0

        lines = _count_lines(f)
        _append_jsonl(TELEMETRY_LOG, {
            "ts": _now_iso(), "sid": sid, "agent": agent, "file": f,
            "lines": lines, "bounded": 1, "dup": 1, "nth": n, "denied": n > 4})

        if n <= 4:
            print(json.dumps({"systemMessage": (
                "Read #%d of %s this session. Earlier regions of this file are still in "
                "your context above -- scroll up before asking for another. Repeat reads "
                "carry 65%% of this fleet's Read bill, and the 5th will be refused."
            ) % (n, f)}))
            return 0

        sys.stderr.write(BOUNDED_DENY_MESSAGE % {"n": n, "file": f})
        return 2

    
    
    
    psigf = "%s.sig.%d" % (entry, fsig_key)

    if not os.path.exists(entry):
        
        _write_text(entry, "")
        _write_text(psigf, sig + "\n")
        try:
            count = len([p for p in os.listdir(state) if os.path.isfile(os.path.join(state, p))])
        except OSError:
            count = 0
        if count <= 2:
            _retire_old_ledgers(STATE_ROOT)
        return 0

    changed = False
    if os.path.exists(psigf):
        prev = _read_text(psigf)
        changed = prev != sig
    _write_text(psigf, sig + "\n")

    if changed:
        
        return 0

    lines = _count_lines(f)
    _append_jsonl(TELEMETRY_LOG, {
        "ts": _now_iso(), "sid": sid, "agent": agent, "file": f,
        "lines": lines, "dup": 1, "denied": 1})

    sys.stderr.write(WHOLE_DENY_MESSAGE % {"file": f, "lines": lines})
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        sys.exit(0)
