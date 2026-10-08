#!/usr/bin/env python3

'PreToolUse(Bash): stop Bash being used as a file reader.\n\nWhy. A Bash call whose only job is to look at a file with cat/sed/grep costs\ncontext for an answer worse than Read\'s (no line-accurate anchoring, no staleness\ncheck, no diagnostics, no hook coverage, no undo). An instruction ("use the\npurpose-built tool") loses because the harness\'s bypass-permissions mode injects a\nper-turn note preferring cat/grep/sed over Read/Edit/Write, arriving after any\ninstruction and winning on recency. A hook can beat that; prose cannot.\n\nThe deny/warn split is the whole design:\n  DENY  file reads (cat, head, tail, more, less, nl, sed -n). Read always covers\n        the replacement, so there is always a legal move.\n  WARN  searches (grep, rg, ag). The Grep tool is not guaranteed to be in a\n        session\'s roster, so denying a search could leave no way to search at\n        all, and a hook that can deadlock a session gets switched off. Searches\n        get one line of advice and proceed.\n\nPer-line, then per-segment. Every line of a multi-line payload is judged on its\nown (a whole-payload bail on any newline would make "cat file\\n" a complete bypass).\nA line containing a pipe, redirect, chain, substitution or shell keyword is\nskipped as composing. The per-line pass alone would deny `cat FILE` and pass\n`cat FILE | head`, `cat FILE && echo done` and `cat FILE; ls`. The segment pass\n(shell-command-scan) adds separator awareness: a pipe into a non-reader is\ncomposition (allow); a pipe into another reader, or `&&`/`;` appended to an\notherwise-complete read, is still just looking (deny). Command substitution is\nout of scope:\n`sed -n "$(grep -n PAT f | cut -d: -f1),+12p" f` computes its anchor with the\nshell, which Read cannot do, and denying it is the false positive this guard\ncannot afford.\n\nConservative. `cat x | jq`, `grep -c foo f > out`, `find . -exec\ngrep ...` all pass untouched. Only a lone simple command with an existing\nfile operand is denied. A heredoc anywhere bails the entire payload: its body is\narbitrary text that must never be parsed as commands. Anything unparseable is\nallowed -- this hook runs on every Bash call, and a false deny costs more than a\nmissed one.'
import importlib.util
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

READERS = {"cat", "head", "tail", "more", "less", "nl"}
SEARCHERS = {"grep", "egrep", "fgrep", "rg", "ag"}

KEYWORDS = {"for", "while", "until", "if", "then", "else", "elif", "fi", "do",
            "done", "case", "esac", "function", "select", "time", "{", "}", "(",
            "!", "local", "export", "return", "break", "continue"}
COMPOSING = ("|", "&&", "||", ";", ">", "<", "`", "$(")




VALUE_FLAGS = {
    "sed":   {"-e", "-f", "--expression", "--file"},
    "grep":  {"-e", "-f", "-m", "-A", "-B", "-C", "--regexp", "--file"},
    "egrep": {"-e", "-f", "-m", "-A", "-B", "-C"},
    "fgrep": {"-e", "-f", "-m", "-A", "-B", "-C"},
    "rg":    {"-e", "-A", "-B", "-C", "-m", "-g", "--glob", "-t", "-T"},
    "ag":    {"-A", "-B", "-C"},
    "head":  {"-n", "-c", "--lines", "--bytes"},
    "tail":  {"-n", "-c", "--lines", "--bytes"},
}




FAST_PATH = re.compile(
    r"(^|[^A-Za-z0-9_./-])(cat|head|tail|sed|grep|rg|ag|more|less|nl)"
    r"(?=[^A-Za-z0-9_./-]|$)")

WARN_MESSAGE = (
    "Searched %s with a shell command. Prefer the `Grep` tool if present; "
    "for a symbol question use the LSP.")

DENY_MESSAGE = """Blocked: this command only reads %(target)s.
Use Read("%(target)s"), with offset/limit for a region.
Piping into jq, a search, or a redirect is allowed; a pipe into head, tail, less, more, nl or sed -n is still a read.
"""


def _load_scanner():
    'The shared shell-command parser. Returns None on any failure -- fail\n    open, because this guard redirects a habit and prevents no damage, so a\n    missing parser must not stop a session from reading files at all.'
    path = os.environ.get("SHELL_COMMAND_SCAN") or os.path.join(
        hp.scripts_dir(), "shell-command-scan.py")
    try:
        spec = importlib.util.spec_from_file_location("scs", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def judge_parts(parts):
    '(kind, path) if this argv is a bare file read/search, else None.'
    if not parts:
        return None
    prog = os.path.basename(parts[0])
    if prog in KEYWORDS:
        return None
    args = parts[1:]
    if prog not in READERS and prog not in SEARCHERS and prog != "sed":
        return None
    
    if prog == "tail" and any(a in ("-f", "-F", "--follow") for a in args):
        return None
    
    if prog == "sed" and any(a == "-i" or a.startswith("-i") for a in args):
        return None

    takes_value = VALUE_FLAGS.get(prog, set())
    operands, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            if a in takes_value:
                skip = True
            continue
        operands.append(a)

    
    
    
    if prog == "sed" or prog in SEARCHERS:
        by_flag = any(a.startswith("-e") or a.startswith("-f") or
                      a in ("--expression", "--file", "--regexp") for a in args)
        if not by_flag:
            operands = operands[1:]

    paths = [p for p in operands if os.path.exists(os.path.expanduser(p))]
    if not paths:
        return None          
    return ("WARN" if prog in SEARCHERS else "DENY", paths[0])


def verdict_for(line):
    '(kind, path) if this one line is a bare file read/search, else None.'
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    for tok in COMPOSING:
        if tok in line:
            return None
    try:
        parts = shlex.split(line)
    except Exception:
        return None
    return judge_parts(parts)


def find_verdict(cmd, cwd, scs):
    
    
    best = None
    for line in cmd.splitlines():
        v = verdict_for(line)
        if v is None:
            continue
        if v[0] == "DENY":
            best = v
            break
        if best is None:
            best = v

    
    
    
    
    
    if best is None and scs is not None and "$(" not in cmd and "`" not in cmd:
        try:
            redirects, segments = scs.parse(cmd, cwd)
        except Exception:
            redirects, segments = [], []

        if not redirects:
            progs = []
            for _d, toks, sep in segments:
                parts = [t.text for t in toks]
                prog = os.path.basename(parts[0]) if parts else ""
                progs.append((prog, parts, sep))

            for idx, (prog, parts, sep) in enumerate(progs):
                if prog in KEYWORDS:
                    
                    best = None
                    break
                v = judge_parts(parts)
                if v is None:
                    continue
                if sep == "|":
                    nxt = progs[idx + 1][0] if idx + 1 < len(progs) else ""
                    
                    
                    if nxt not in READERS and nxt != "sed":
                        continue
                if v[0] == "DENY":
                    best = v
                    break
                if best is None:
                    best = v

    return best


def main():
    raw = sys.stdin.read()

    
    
    if not FAST_PATH.search(raw):
        return 0

    try:
        payload = json.loads(raw)
        cmd = (payload.get("tool_input") or {}).get("command", "")
        cwd = payload.get("cwd") or os.getcwd()
    except Exception:
        return 0
    if not isinstance(cmd, str) or not cmd.strip():
        return 0

    
    if "<<" in cmd:
        return 0

    scs = _load_scanner()
    best = find_verdict(cmd, cwd, scs)
    if best is None:
        return 0

    kind, target = best
    if kind == "WARN":
        
        
        print(json.dumps({"systemMessage": WARN_MESSAGE % target}))
        return 0

    sys.stderr.write(DENY_MESSAGE % {"target": target})
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        sys.exit(0)
