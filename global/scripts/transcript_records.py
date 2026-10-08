"One parse of a Claude Code transcript per process, shared by every hook that reads it.\n\nThe Stop chain read the same transcript about six times per turn end (context overhaul,\nPhase 1 item 6): verification-claim-judge twice, locked-test-drift-gate twice,\nrequire-structured-questions and block-stop-with-open-work once each. At 27 ms a parse\non a 4.3 MB transcript that was ~160 ms per turn, growing with the session.\nhook-dispatch.py runs every Python guard in one process, so a module-level cache keyed\non (path, size, mtime) serves them all from one read, and a transcript that grew since\nis read again.\n\nrecords() is tolerant: a malformed line, a line that is not a JSON object, or an\nundecodable byte never stops the read (the ralph-cleanup-stop lesson: one bad line must\nnot decide a whole turn's verdict). A missing or unreadable path gives an empty tuple.\nThe tuple is shared: callers must not rely on mutating what it holds."
import json
import os

PARSES = 0          
_CACHE = {}         
_RECENT = {}        
BLOCK = 1 << 20     


def is_human_prompt(rec):
    'is human prompt.'
    if rec.get("type") != "user" or rec.get("isMeta"):
        return False
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    if rec.get("promptSource") == "system":
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in content)
    return True


def _parse(raw):
    try:
        rec = json.loads(raw.decode("utf-8", "replace"), strict=False)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def recent(path, prompts=2):
    'The records from the `prompts`-th last answered human prompt to the end, in order,\n    as a tuple; every record when the transcript holds fewer. Tolerant as records() is.'
    global PARSES
    if not path:
        return ()
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return ()
    key = (st.st_size, st.st_mtime_ns, prompts)
    hit = _RECENT.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    full = _CACHE.get(path)
    if full is not None and full[0] == key[:2]:
        
        newest_first, _ = _window(reversed(full[1]), prompts)
    else:
        try:
            source = _from_end(path, st.st_size)
            newest_first, whole = _window(source, prompts)
            
            whole = whole or next(source, None) is None
        except OSError:
            return ()
        PARSES += 1
        if whole:
            
            
            _CACHE.clear()
            _CACHE[path] = (key[:2], tuple(reversed(newest_first)))
    result = tuple(reversed(newest_first))
    _RECENT.clear()
    _RECENT[path] = (key, result)
    return result


def _from_end(path, size):
    "The file's records, newest first, read back from the end BLOCK bytes at a time."
    pending = b""
    with open(path, "rb") as fh:
        pos = size
        while pos > 0:
            step = min(BLOCK, pos)
            pos -= step
            fh.seek(pos)
            lines = (fh.read(step) + pending).split(b"\n")
            
            
            pending = lines.pop(0) if pos > 0 else b""
            for raw in reversed(lines):
                rec = _parse(raw)
                if rec is not None:
                    yield rec


def _window(newest_first, prompts):
    "(records, newest first, down to the `prompts`-th last answered human prompt;\n    True when the source ran out before that prompt).\n\n    Only an ANSWERED prompt counts: a slash command's records (`/exit` and its output)\n    read as human prompts with nothing after them, and counting those left the window\n    short of the turn a reader wants. Each prompt needs its own reply, so the next one\n    back is judged on the records between it and the one already counted."
    out, seen, answered = [], 0, False
    for rec in newest_first:
        out.append(rec)
        if rec.get("type") == "assistant":
            answered = True
        elif answered and is_human_prompt(rec):
            seen, answered = seen + 1, False
            if seen >= prompts:
                return out, False
    return out, True


def records(path):
    'Every JSON-object line of the transcript at `path`, in order, as a tuple.'
    global PARSES
    if not path:
        return ()
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return ()
    key = (st.st_size, st.st_mtime_ns)
    hit = _CACHE.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line, strict=False)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return ()
    PARSES += 1
    result = tuple(out)
    _CACHE.clear()          
    _CACHE[path] = (key, result)
    return result
