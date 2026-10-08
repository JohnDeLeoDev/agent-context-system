#!/usr/bin/env python3
'terse-judge — the one copy of "did this message earn its length", plus telemetry.\n\nWhy it is a shared script. Two hooks need this judgment and they differ only in\ndelivery, which is the verification-claim-judge shape exactly:\n\n  * terse-output-check (UserPromptSubmit) advises the next turn. Right home for\n    advice, wrong home for a loop: a ralph iteration re-feeds through a Stop\n    hook\'s decision:block, which never raises UserPromptSubmit, so every\n    UserPromptSubmit guard on this fleet is dead inside a loop.\n  * terse-output-gate (Stop) blocks the current turn, and Stop is the one event\n    a loop cannot skip, because firing it is how the loop advances.\n\nWhy the telemetry lives here and not in a hook. Whether responses drift longer\nduring loops and longer sessions is a claim about a trend, and a count of how many\ntimes a hook spoke cannot confirm or deny it: that needs word counts, turn depth\nand a loop flag. read-width-nudge logs every Read, violation or not, for the same\nreason.\n\nSo every judged turn is recorded, including the quiet ones. A log of violations\nalone answers "how loud was the worst turn" and can never answer "is turn 40\nworse than turn 4", which is the actual question.\n\nEarned is deliberately generous, and stays as terse-output-check drew it:\na turn that asked a structured question, a turn in which a tool failed, or a turn\nwhose prompt asked for a report. A guard that fires on turns that deserved their\nlength teaches its reader to skim it.\n\nIn a loop the budget is tighter, because the rule is different there and already\nwritten down: ralph-loop-notes says an iteration\'s visible output is one line —\n`done:` / `blocked:` / `no-op:` and why — with everything else going to the\nloop\'s ledger. That doc is lazily loaded, so an iteration re-fed with a one-line\npointer may never have it in context at all. The number enforces what the prose\ncannot reach.'

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import transcript_records

BUDGET = 40                     
BUDGET_IN_LOOP = 25             

FENCE = re.compile(r"```.*?```", re.S)
CODE = re.compile(r"`[^`]*`")



ASKED_FOR_PROSE = re.compile(
    r"\b(report|assess|assessment|review|explain|explanation|describe|detail|"
    r"summar\w+|walk me through|write up|deep dive|full|thorough|why\b)",
    re.I)

TELEMETRY = os.environ.get("TERSE_TELEMETRY") or os.path.join(
    os.path.expanduser("~"), ".local", "state", "agent-context",
    "terse-telemetry.jsonl")

DEPTH_DIR = os.environ.get("TERSE_DEPTH_DIR") or os.path.join(
    os.path.expanduser("~"), ".local", "state", "agent-context", "terse-depth")

LOOP_STATE_FILES = ("ralph-loop.local.md", "ralph-cleanup.local.md")

WORDS_PY = os.environ.get("PLAIN_LANGUAGE_WORDS") or os.path.join(
    hp.scripts_dir(), "plain-language-words.py")

_WORDS = []


def words():
    'The canonical word lists, or None when they cannot be loaded.\n\n    Loaded from the same file plain-language-check evaluates, so the guard on\n    writes and the guard on chat can never disagree about what is banned.'
    if not _WORDS:
        mod = None
        path = WORDS_PY
        if not os.path.exists(path):
            path = os.path.join(os.path.expanduser("~"), ".agent-context",
                                "global", "scripts", "plain-language-words.py")
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("pl_words", path)
            if spec is None or spec.loader is None:
                raise ImportError("cannot load spec for pl_words from %s" % path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            mod = None
        _WORDS.append(mod)
    return _WORDS[0]


def banned(msg):
    'Hard-list words and em dashes in the text user reads.\n\n    Why this is here. plain-language-check gates writes: files, commit messages,\n    store bodies. A message to user matches none of its tools, so without this a\n    word blocked in a code comment would still reach him in chat.\n\n    Returns the distinct offenders, capped, so the caller can name them.'
    mod = words()
    if mod is None:
        return []
    text = visible(msg or "")
    found = []
    
    
    for pat in [mod.alt(mod.HARD)] + list(mod.HARD_PHRASE):
        try:
            found += [m.group(0) for m in re.finditer(pat, text, re.I)]
        except re.error:
            pass
    if mod.EM_DASH in text:
        found.append("em dash")
    out = []
    for hit in found:
        if hit.lower() not in [s.lower() for s in out]:
            out.append(hit)
    return out[:8]


def visible(msg):
    'Message with code removed. Fenced blocks and code spans are content.\n\n    A diff, a probe result or a command is the answer, not padding around it.\n    Tables are deliberately not stripped: a table of things user already knows is\n    the exact shape this exists to catch.'
    return CODE.sub(" ", FENCE.sub(" ", msg or ""))


def in_loop(cwd):
    'Is a ralph loop active at or above `cwd`?\n\n    Walks up the way PATCH C in ralph-patch-guard walks up, and for the same\n    reason: a loop that edits source spends its turns inside\n    .agents/worktrees/<name> (or legacy .claude/worktrees/<name>), where the relative path does not exist. A checker\n    that only looks at the cwd reports "no loop" for most of a loop\'s life.\n\n    `active: false` is honored. Anything unreadable counts as not in a loop,\n    because the only cost of getting this wrong is a slightly looser budget.'
    probe = os.path.abspath(cwd or ".")
    while probe and probe != "/":
        for name in LOOP_STATE_FILES:
            path = os.path.join(probe, hp.CLAUDE_DIRNAME, name)
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        head = fh.read(2048)
                except OSError:
                    return False
                if re.search(r"^active:\s*false\b", head, re.M):
                    return False
                return True
        parent = os.path.dirname(probe)
        if parent == probe:  
            break
        probe = parent
    return False


def depth(session_id, bump=False):
    'How many turns deep this session is.\n\n    Counted in a tiny ledger rather than by parsing the transcript. A transcript\n    grows without bound and every turn would re-walk all of it, which is the same\n    cost pattern the read guards exist to stop. Failure returns 0: an unknown\n    depth is reported as unknown, never as a violation.'
    if not session_id:
        return 0
    path = os.path.join(DEPTH_DIR, str(session_id))
    try:
        os.makedirs(DEPTH_DIR, exist_ok=True)
        n = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                n = int((fh.read() or "0").strip() or 0)
        if bump:
            n += 1
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(n))
        return n
    except (OSError, ValueError):
        return 0


def read_turn(transcript_path):
    '(asked, failed, user_text, assistant_text) for the turn just finished.\n\n    Per-line parsing: one malformed transcript line must not decide the verdict\n    for a whole turn. That is the ralph-cleanup-stop lesson, and both callers are\n    allowed to be wrong only in the permissive direction.\n\n    The window closes at the last prompt that has assistant output after\n    it. Whether the incoming prompt is already on disk when a UserPromptSubmit\n    hook runs is a harness detail that differs by version, and assuming either way\n    yields an empty window — which reads as "nothing to say" and silences the\n    caller permanently.'
    asked = failed = False
    user_text = ""
    said = []
    if not transcript_path:
        return asked, failed, user_text, ""
    
    
    records = transcript_records.recent(transcript_path)

    def is_prompt(rec):
        if (rec.get("type") or rec.get("role")) != "user":
            return False
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                           for b in content)
        return bool(content)

    start = None
    for idx in range(len(records) - 1, -1, -1):
        if is_prompt(records[idx]) and any(
                (r.get("type") or r.get("role")) == "assistant"
                for r in records[idx + 1:]):
            start = idx
            break
    if start is None:
        return asked, failed, user_text, ""

    content = (records[start].get("message") or {}).get("content")
    if isinstance(content, str):
        user_text = content
    elif isinstance(content, list):
        user_text = " ".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")

    for rec in records[start + 1:]:
        role = rec.get("type") or rec.get("role")
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            if role == "assistant":
                said.append(content)
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result" and b.get("is_error"):
                failed = True
            elif b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                asked = True
            elif b.get("type") == "text" and role == "assistant":
                said.append(b.get("text") or "")
    return asked, failed, user_text, "\n".join(said)


def judge(payload, event, bump_depth=False):
    'The verdict for one finished turn. Never raises; never blocks by itself.\n\n    Returns a dict the caller acts on:\n      words / budget / over  — the measurement\n      earned                 — why the length was allowed, or None\n      in_loop / depth        — the two axes the telemetry tracks'
    transcript = payload.get("transcript_path") or ""
    session = payload.get("session_id") or ""
    asked, failed, user_text, said = read_turn(transcript)

    
    
    
    msg = payload.get("last_assistant_message") or said

    loop = in_loop(payload.get("cwd") or os.getcwd())
    budget = BUDGET_IN_LOOP if loop else BUDGET
    words_n = len(visible(msg).split())

    earned = None
    if asked:
        earned = "asked a structured question"
    elif failed:
        earned = "a tool failed in this turn"
    elif ASKED_FOR_PROSE.search(user_text or ""):
        earned = "the prompt asked for a report"

    verdict = {
        "words": words_n,
        "budget": budget,
        "over": words_n > budget and earned is None,
        "banned": banned(msg),
        "earned": earned,
        "in_loop": loop,
        "depth": depth(session, bump=bump_depth),
        "empty": not (msg or "").strip(),
    }
    record(payload, event, verdict)
    return verdict


def record(payload, event, verdict):
    "Append one line of telemetry. Failure here is silent and costs nothing.\n\n    Telemetry that can break the guard it measures is worse than no telemetry,\n    so every error path here is a pass. The transcript's size in bytes rides\n    along as the cheapest available proxy for how much context the session is\n    carrying — the payload exposes no token count, and one stat is affordable\n    where re-walking the transcript is not."
    try:
        transcript = payload.get("transcript_path") or ""
        try:
            size = os.path.getsize(transcript) if transcript else 0
        except OSError:
            size = 0
        os.makedirs(os.path.dirname(TELEMETRY), exist_ok=True)
        with open(TELEMETRY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "event": event,
                "session": payload.get("session_id") or "",
                "words": verdict["words"],
                "budget": verdict["budget"],
                "over": verdict["over"],
                "earned": verdict["earned"],
                "in_loop": verdict["in_loop"],
                "depth": verdict["depth"],
                "transcript_bytes": size,
            }, separators=(",", ":")) + "\n")
    except Exception:
        pass
