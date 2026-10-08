#!/usr/bin/env python3

'PreToolUse(Bash): block unbounded sleep-polling loops -- `while true; do ...\nsleep N; done` and `until <cond>; do sleep N; done` -- while leaving every\nbounded wait alone.\n\nWhy. An unbounded sleep/poll loop can run to the harness\'s 600-second cap and\nreturn nothing. The recurring shape is a poll for something that already\nannounces itself:\n\n    until [ "$(git log --oneline -1 ...)" != "dd46bbe" ]; do sleep 20; done\n\nBackground tasks notify the agent when they finish, so polling for one is never\nnecessary; Monitor takes an until-condition and does the waiting without\nburning a Bash call.\n\nA gate people route around is worse than no gate, so this errs hard toward\nprecision: it blocks only when it can see both that a loop sleeps and that\nnothing bounds its iteration count. Anything it cannot parse confidently is\nallowed through.\n\nQuoted text is data: writing a note about a poll loop\nmust not trip this hook. Quoted spans are blanked before scanning for loop\nsyntax via the shared shell-command-scan.blank_quoted, unless a quoted string\nis handed to something that executes it (`bash -c`, `sh -c`, `eval`, or piped\ninto a shell). There the quotes are the program, and a runaway loop can live\ninside them, as in `echo \'while true; do sleep 1; done\' | sh`.\n\nA heredoc body is only shell if a shell eats it. `python3 - <<\'PY\' ... PY` is\nnot a shell consumer, so its body is stripped before scanning -- a Python\n`while i < n:` near a string containing `; do ... done` otherwise matches a\nloop header. `bash <<EOF` and `... | sh` execute the body and stay in scope.\nAuthoring a script that happens to contain a poll loop (`cat > w.sh <<\'EOF\' ...\nEOF`) is a file write, not an execution, and is exempt.\n\nA `for` loop is never flagged: it iterates a fixed list and is bounded by\nconstruction. A `timeout`-wrapped command has a hard cap whatever it does\ninside. A bound in the loop condition (numeric comparison, a SECONDS deadline,\na `read`) or in the body (a deadline/counter test paired with `break`/`exit`,\nor an incremented variable the condition tests) both count.'
import importlib.util
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

EXECUTOR = re.compile(
    r'\b(?:ba|z|k)?sh\s+(?:-\w+\s+)*-c\b'
    r'|\beval\b'
    r'|\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b')

SHELL_HEREDOC = re.compile(
    r'(?:^|[;&|(]\s*)(?:sudo\s+)?(?:ba|z|k)?sh\b[^\n]*<<'
    r'|\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b')

HEADER = re.compile(r'\b(while|until)\b(.*?)(?:;|\n)\s*do\b', re.S)

BOUNDED_COND = re.compile(
    r'-lt\b|-le\b|-gt\b|-ge\b'          
    r'|\(\(.*?[<>].*?\)\)'              
    r'|\bSECONDS\b'                     
    r'|\bread\b', re.S)                 

INCREMENT = re.compile(
    r'(?:\(\(\s*|\blet\s+|\$\(\(\s*)?\b([A-Za-z_]\w*)\s*(?:\+\+|=\s*\$?\(?\(?\s*\1\s*\+|\+=)')

BODY_BOUND = re.compile(r'-lt\b|-le\b|-gt\b|-ge\b|\(\(.*?[<>].*?\)\)|\bSECONDS\b', re.S)

DENY_MESSAGE = """BLOCKED by ~/.agent-context/global/hooks/block-sleep-poll.py: unbounded sleep loop.
  %s
Do one of:
  1. Work you started: run it with run_in_background: true; you are notified when it ends.
  2. External condition: use the Monitor tool with an until-condition.
  3. Bounded retry: `for i in $(seq 1 10); do <cmd> && break; sleep 5; done` or `timeout 120 <cmd>`.
"""


def _load_scanner():
    path = os.environ.get("SHELL_COMMAND_SCAN") or os.path.join(
        hp.scripts_dir(), "shell-command-scan.py")
    spec = importlib.util.spec_from_file_location("scs", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bounded(cond, body):
    if BOUNDED_COND.search(cond):
        return True
    if re.search(r'\b(?:break|exit)\b', body) and BODY_BOUND.search(body):
        return True
    for m in INCREMENT.finditer(body):
        
        if re.search(r'[$\b]' + re.escape(m.group(1)) + r'\b', cond):
            return True
    return False


def find_block(cmd):
    "The offending 'while/until <cond>' text, or None."
    try:
        scs = _load_scanner()
    except Exception:
        
        
        
        
        return None

    if EXECUTOR.search(cmd):
        scan = cmd
    else:
        base = cmd if SHELL_HEREDOC.search(cmd) else scs.strip_heredocs(cmd)
        scan = scs.blank_quoted(base)

    if not re.search(r'\bsleep\b', scan):
        return None

    
    
    if "<<" in cmd and not re.search(r'\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b', cmd) \
            and re.match(r'^\s*(?:cat|tee)\b', cmd) and re.search(r'>\s*\S', cmd):
        return None

    
    if re.search(r'(^|[;&|(]\s*)(?:sudo\s+)?g?timeout\s+\S', cmd):
        return None

    for m in HEADER.finditer(scan):
        cond = m.group(2)
        
        
        
        
        tail = scan[m.end():]
        ends = [mm.end() for mm in re.finditer(r'\bdone\b', tail)]
        body = tail[:ends[-1]] if ends else tail
        if not re.search(r'\bsleep\b', body):
            continue                      
        if bounded(cond, body):
            continue
        return " ".join((m.group(1) + " " + cond.strip()).split())[:120]
    return None


def main():
    raw = sys.stdin.read()

    
    if "sleep" not in raw:
        return 0

    try:
        data = json.loads(raw)
        args = data.get("tool_input") or data.get("tool_args") or data.get("params") or {}
        cmd = (args.get("command") if isinstance(args, dict) else "") or data.get("command") or ""
    except Exception:
        return 0
    if not cmd:
        return 0

    verdict = find_block(cmd)
    if not verdict:
        return 0

    sys.stderr.write(DENY_MESSAGE % verdict)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        sys.exit(0)
