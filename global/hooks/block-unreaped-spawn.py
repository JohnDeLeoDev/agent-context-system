#!/usr/bin/env python3

'PreToolUse(Bash): block a backgrounded process whose only cleanup is a\n`kill` on the success path -- no `trap`, no `timeout`. That kill is not a stop;\nit is a hope.\n\nWhy. The shape this catches, here with a CPU load generator that has no\ndeadline and no stop flag:\n\n    stress_bounded &\n    STRESS_PID=$!\n    swift test --filter WithTimeoutTests 2>&1 | tail -40\n    kill -9 "$STRESS_PID"\n\nThe kill is reachable only if `swift test` returns. If the test hangs or the\ncall is cut off, the background job keeps running and nothing reaps it. A\nsubagent inherits no global instructions, so a written reaping rule cannot\nreach it. A hook can.\n\nA gate people route around is worse than no gate, so this is kept narrow: a\nbackground job, a `kill` present (proving the author knew reaping was needed),\nand no `trap` and no `timeout` making that reaping unconditional. It does not\npolice every `cmd &`; that would be constant noise on legitimate work.\n\nRecall is partial. A background job with no cleanup attempt at all passes\nhere; human review is the backstop for that.\n\nQuoted text is data: a note about an unreaped spawn must not be refused\nbecause it quotes the command it reports. Quoted spans are blanked before\nlooking for shell syntax, unless a quoted string is handed to something that\nexecutes it (`bash -c`, `sh -c`, `eval`), where the quotes are the program and\nan offender can live inside them.\n\nEvery `&` that is not a background operator is neutralized first, so the rest\nof the hook can treat a surviving `&` as backgrounding: `&&` (logical and),\n`2>&1` / `>&2` / `>&` (fd duplication), `<&` (input fd dup), `|&` (pipe incl.\nstderr), `&>` (redirect both). Misreading one of these as a spawn is how a\nguard becomes noise and gets disabled.'
import json
import re
import sys

EXECUTOR = re.compile(r'\b(?:ba|z|k)?sh\s+(?:-\w+\s+)*-c\b|\beval\b')

DENY_MESSAGE = """BLOCKED by ~/.agent-context/global/hooks/block-unreaped-spawn.py: background job reaped only by a success-path `kill`.
  %s &
Give it its own deadline, one of:
  1. `<cmd> & PID=$!; trap 'kill "$PID" 2>/dev/null' EXIT INT TERM; <work>`
  2. `timeout 300 <cmd> &`
  3. Build the deadline into the spawned program itself.
"""


def blank_quoted(s):
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            out.append("  ")
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            j = n - 1 if j == -1 else j
            out.append(" " * (j - i + 1))
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if (s[j] == "\\" and j + 1 < n) else 1
            j = min(j, n - 1)
            out.append(" " * (j - i + 1))
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def find_unreaped(cmd):
    scan = cmd if EXECUTOR.search(cmd) else blank_quoted(cmd)

    
    
    if "<<" in cmd and not re.search(r'\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b', cmd) \
            and re.match(r'^\s*(?:cat|tee)\b', cmd) and re.search(r'>\s*\S', cmd):
        return None

    neutral = scan
    neutral = neutral.replace("&&", "\x00\x00")
    neutral = re.sub(r'\d?>&', lambda m: "\x00" * len(m.group(0)), neutral)
    neutral = neutral.replace("<&", "\x00\x00")
    neutral = neutral.replace("|&", "\x00\x00")
    neutral = neutral.replace("&>", "\x00\x00")

    if "&" not in neutral:
        return None          

    
    
    if re.search(r'\btrap\b', scan):
        return None

    
    
    if not re.search(r'\b(?:p)?kill\b', scan):
        return None

    
    
    
    unbounded = []
    for m in re.finditer(r'&', neutral):
        seg = neutral[:m.start()]
        cut = max(seg.rfind(";"), seg.rfind("\n"), seg.rfind("&"),
                  seg.rfind("|"), seg.rfind("("))
        seg = seg[cut + 1:]
        if re.search(r'\b(?:sudo\s+)?g?timeout\s+\S', seg):
            continue
        job = " ".join(seg.split())
        if job:
            unbounded.append(job)

    if not unbounded:
        return None
    return unbounded[0][:120]


def main():
    raw = sys.stdin.read()

    
    if "&" not in raw:
        return 0

    try:
        data = json.loads(raw)
        args = data.get("tool_input") or data.get("tool_args") or data.get("params") or {}
        cmd = (args.get("command") if isinstance(args, dict) else "") or data.get("command") or ""
    except Exception:
        return 0
    if not cmd:
        return 0

    job = find_unreaped(cmd)
    if not job:
        return 0

    sys.stderr.write(DENY_MESSAGE % job)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        
        sys.exit(0)
