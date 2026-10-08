#!/usr/bin/env python3
'The shared shell-command parser for guards that decide on shell syntax.\n\nNone of these is fixable by a better regex. Reading the command once, in\norder, holding quote/heredoc/segment state, is the whole fix.\n\nSo this module is the one copy. A guard that decides on shell syntax imports\n`parse()` (python) or calls this file as a CLI (sh). Re-implementing any of\nit is a defect the `guard-parses-not-greps` invariant fails on.\n\nCLI\n    echo \'<hook payload json>\' | shell-command-scan.py\n    shell-command-scan.py --command \'cat x | grep y\' [--cwd /abs/dir]\n\n  Emits one JSON object on stdout:\n    {"command": str,            # after line-joining and heredoc stripping\n     "redirects": [{"dir": str, "text": str, "quoted": bool}, ...],\n     "segments":  [{"dir": str,\n                    "argv": [{"text": str, "quoted": bool}, ...]}, ...]}\n\n  Exit 0 with {"command": "", ...} when there is nothing to judge (no\n  command, unparseable payload). A guard must treat that as "cannot judge"\n  and fall through to allow -- never as "clean".\n\nWhat a segment is. The command split on `; | & ( ) newline`, each carrying the\ndirectory its relative paths resolve against (`cd X && ...` is followed).\nSegment awareness matters: `cat FILE` and\n`cat FILE | grep x` present the same first segment, and `git stash list` can\nbe told from `git stash push` by reading argv rather than the raw string.\n\nWhat `quoted` is for. A token that came out of quotes is data.\nA guard must not match its\ntrigger pattern against a token whose `quoted` is True, because prose that\nmentions a sleep loop or a deploy script is not one.\n\nObservations guarded: #224, #229, #237, #249, #254, #308, #312, #337, #342, #351, #353.'

import base64
import json
import os
import re
import sys

__all__ = [
    "Tok", "parse", "strip_heredocs", "sed_script", "scan", "blank_quoted",
    "payload_command", "write_targets", "collect_env", "resolve_candidates",
    "unescape_dquotes", "powershell_as_posix",
]


def strip_heredocs(s):
    "Drop heredoc BODIES. Their words are data, not arguments.\n\n    The opening line is KEPT, because that is where the redirect lives:\n    `cat > src/New.swift <<'EOF'` must still be seen as a write to\n    src/New.swift."
    out, lines, i = [], s.split("\n"), 0
    while i < len(lines):
        out.append(lines[i])
        term, j, ln = None, 0, lines[i]
        while True:
            j = ln.find("<<", j)
            if j == -1:
                break
            k = j + 2
            if k < len(ln) and ln[k] == "-":
                k += 1
            while k < len(ln) and ln[k] == " ":
                k += 1
            if k < len(ln) and ln[k] in "\"'":
                k += 1
            st = k
            while k < len(ln) and (ln[k].isalnum() or ln[k] == "_"):
                k += 1
            if k > st:
                term = ln[st:k]
            j = k
        if term:
            i += 1
            while i < len(lines) and lines[i].strip() != term:
                i += 1
        i += 1
    return "\n".join(out)


def sed_script(w):
    'Is this word a sed/perl script operand rather than a filename?\n\n    Recognises `s<d>...<d>...<d>` and `y<d>...<d>...<d>` where the delimiter is\n    a punctuation character not found in ordinary paths: `s|a|b|`, `s,a,b,`,\n    `s#a#b#`, `y!abc!xyz!`.\n\n    Slash-delimited expressions are deliberately not matched here, and that is\n    the whole reason this function is narrow. `s/a/b/` was never the problem:\n    an existence filter already rejects it, because its parent resolves to\n    <base>/s, which does not exist. Claiming `/` here would cost real accuracy\n    -- a genuine path like `s/x/y/z.txt` would then be read as an expression\n    and skipped, silently letting a write through. Excluding `.`, `_` and `-`\n    for the same reason keeps `s.txt` and `s-1.log` filenames.'
    if len(w) < 4 or w[0] not in "sy":
        return False
    d = w[1]
    if d.isalnum() or d in "/._-":
        return False
    return w.count(d) >= 3


class Tok(object):
    __slots__ = ("text", "quoted")

    def __init__(self, text, quoted):
        self.text = text
        self.quoted = quoted

    def __repr__(self):
        return "Tok(%r, quoted=%r)" % (self.text, self.quoted)


def scan(s, base):
    'One left-to-right pass over the command, tracking quote state.\n\n    None of these is fixable by a better regex, because in each one the same\n    character means different things depending on quote and heredoc state,\n    which is exactly the state a regex does not carry. Reading the command\n    once, in order, holding that state, is the whole fix.\n\n    Returns (redirects, segments). A segment is (dir, [Tok, ...], sep) where\n    `dir` is the directory that segment\'s relative paths resolve against and\n    `sep` is the operator that follows it ("|", "||", "&&", "&", ";", "\\n",\n    "(", ")", or "" at the end).'
    redirects, segments = [], []
    cur, curdir = [], [base]
    pending = [False]        
    
    
    T, Q, S = [[]], [False], [False]     

    def endtok():
        if not S[0]:
            return
        tok = Tok("".join(T[0]), Q[0])
        T[0], Q[0], S[0] = [], False, False
        if pending[0]:
            pending[0] = False
            redirects.append((curdir[0], tok))
        else:
            cur.append(tok)

    def apply_cd():
        "`cd X && ...` moves where later relative paths point.\n\n        Without this, `cd <scratchpad> && python3 gen.py > out.json` resolved\n        out.json against the SESSION's cwd and blocked a write to a directory\n        the policy exempts. An undecidable target (a variable, a glob, a\n        directory that is not there) deliberately leaves the directory\n        unchanged, so the fallback is the conservative behavior rather than a\n        guess."
        if not cur or cur[0].text != "cd":
            return
        words = [t for t in cur[1:] if not t.text.startswith("-")]
        if len(words) != 1:
            return
        p = words[0].text
        if not p or "$" in p or "*" in p:
            return
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.join(curdir[0], p)
        p = os.path.normpath(p)
        if os.path.isdir(p):
            curdir[0] = p

    def endseg(sep=""):
        'Close the current segment, recording the operator that ended it.\n\n        The separator matters. Without it a guard can\n        see that `cat f` and `grep x` are two segments but not whether the\n        first feeds the second. The distinction:\n        `cat FILE | jq .` is the shell composing (allow), while\n        `cat FILE && echo done` is a lone read with two characters appended to\n        slip past a guard (deny). Both are "two segments" and they are not the\n        same act.\n\n        `sep` is the operator FOLLOWING this segment: one of "|", "||", "&&",\n        "&", ";", "\\n", "(", ")", or "" for the last segment.'
        endtok()
        if cur:
            segments.append((curdir[0], list(cur), sep))
            apply_cd()
            del cur[:]

    i, n = 0, len(s)
    while i < n:
        c = s[i]

        if c == "\\" and i + 1 < n:
            T[0].append(s[i + 1]); S[0] = True; i += 2; continue

        if c == "'":
            j = s.find("'", i + 1)
            j = n if j == -1 else j
            T[0].append(s[i + 1:j]); Q[0] = True; S[0] = True
            i = j + 1; continue

        if c == '"':
            j, buf = i + 1, []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j + 1]); j += 2; continue
                buf.append(s[j]); j += 1
            T[0].append("".join(buf)); Q[0] = True; S[0] = True
            i = j + 1; continue

        if c in " \t":
            endtok(); i += 1; continue

        if c == "#" and not S[0]:
            
            
            
            
            while i < n and s[i] != "\n":
                i += 1
            continue

        if c == "<":
            
            
            
            endtok()
            i += 1
            while i < n and s[i] in "<-":
                i += 1
            while i < n and s[i] in " \t":
                i += 1
            if i < n and s[i] in "\"'":
                q = s[i]; j = s.find(q, i + 1); i = (n if j == -1 else j) + 1
            else:
                while i < n and s[i] not in " \t;|&()<>\n":
                    i += 1
            continue

        if c == ">" or (c == "&" and i + 1 < n and s[i + 1] == ">"):
            
            
            
            if S[0] and not Q[0] and "".join(T[0]).isdigit():
                T[0], Q[0], S[0] = [], False, False
            else:
                endtok()
            if c == "&":
                i += 1
            i += 1
            if i < n and s[i] == ">":
                i += 1
            while i < n and s[i] in " \t":
                i += 1
            if i < n and s[i] == "&":
                
                i += 1
                while i < n and (s[i].isdigit() or s[i] == "-"):
                    i += 1
                continue
            if i < n and s[i] == "|":       
                i += 1
                while i < n and s[i] in " \t":
                    i += 1
            pending[0] = True
            continue

        if c in ";\n|&()":
            
            
            
            
            
            op = c
            if c in "|&" and i + 1 < n and s[i + 1] == c:
                op = c + c
                endseg(op)
                i += 2
                continue
            endseg(op)
            i += 1
            continue

        T[0].append(c); S[0] = True; i += 1

    endseg()
    return redirects, segments


def blank_quoted(s):
    'Blank the contents of quoted spans, preserving length.\n\n    The second view, and why the module has two. `parse()` returns argv, which\n    is what a guard needs to ask "which command is this, and what are its\n    operands" (block-git-stash, block-shell-file-read, block-deploy). But a\n    guard reasoning about shell structure -- `while ... ; do ... sleep ...\n    done` -- cannot use argv at all, because segmentation splits on `;` and\n    `|` and destroys the very construct it is looking for.\n\n    Such a guard needs the raw text with quoted spans neutralized, so its\n    regexes still see loop syntax while prose inside quotes is invisible. That\n    is this function. It exists here, not in a guard, so there is one\n    implementation of quote state in the fleet: a second copy is how the two drift.\n\n    Length is preserved on purpose -- callers index into the result and report\n    offsets against the original command.\n\n    Note for callers: this deliberately does not strip heredoc bodies. A guard\n    that also wants those gone composes the two: blank_quoted(strip_heredocs(s)).\n    Keeping them separate matters because block-sleep-poll must still see a\n    heredoc opener to decide whether the body is being written to a file or\n    executed.'
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


def unescape_dquotes(s):
    '`s` with backslash-escaping UNDONE inside DOUBLE-quoted spans only, the\n    way a running shell reads `\\"`, `\\\\`, `\\$`, `` \\` `` there. A single\n    quote takes no escaping in a shell (a backslash inside one is literal) and\n    is copied through untouched; text outside any quote is also copied\n    through untouched, backslashes and all.\n\n    Why this exists: a guard that regexes\n    an interpreter one-liner for a literal argument -- `open("...")` inside\n    `python3 -c "..."` -- has to match the text the interpreter receives, not\n    the shell-escaped source. `python3 -c "open(\\"/opt/x\\",\\"w\\")"` is\n    valid bash: the `\\"` pairs are bash\'s own escaping of the double quotes\n    python3\'s argument needs, and after the shell strips them python3 sees\n    `open("/opt/x","w")`. A regex run on the raw source sees `\\"` where it\n    wants a bare `"` right after `open(` and never matches. Regexing this\n    function\'s output matches what python3 gets.\n\n    This is a flat string transform, not a tokenizer -- length is not\n    preserved and there is no notion of "the Nth argv word" here, unlike\n    blank_quoted. It exists for a caller that needs to regex JOINED text (an\n    interpreter one-liner\'s whole body, or a heredoc\'s), not argv.'
    out = []
    i, n = 0, len(s)
    in_squote = in_dquote = False
    while i < n:
        c = s[i]
        if in_squote:
            out.append(c)
            in_squote = c != "'"
            i += 1
            continue
        if in_dquote:
            if c == "\\" and i + 1 < n and s[i + 1] in ('"', "\\", "$", "`"):
                out.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                in_dquote = False
            out.append(c)
            i += 1
            continue
        if c == "'":
            in_squote = True
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_dquote = True
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(c)
            out.append(s[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse(command, cwd=None):
    'The library entry point. Returns (redirects, segments).\n\n    `command` is the raw string from the hook payload; `cwd` the session\'s\n    working directory, used as the base for relative paths.\n\n    Line continuations are joined first. A trailing backslash-newline is one\n    command line, but the scanner treats a newline as a separator and would\n    read the dangling backslash as an argument: `ln -sf <src> \\` + newline +\n    `<dest>` would block cwd + "/\\".'
    if not command:
        return [], []
    base = cwd or os.getcwd()
    command = command.replace("\\\n", " ")
    return scan(strip_heredocs(command), base)


def payload_command(d):
    'Pull (command, cwd) out of a hook payload, tolerating every shape.\n\n    Returns ("", cwd) when there is nothing to judge. A caller must read that\n    as "cannot judge" and allow -- never as "clean".'
    if not isinstance(d, dict):
        return "", os.getcwd()
    ti = d.get("tool_input") or d.get("tool_args") or d.get("params") or {}
    if not isinstance(ti, dict):
        ti = {}
    cmd = ti.get("command") or ti.get("cmd") or ""
    cwd = d.get("cwd") or os.getcwd()
    return (cmd if isinstance(cmd, str) else ""), cwd


WRITERS_ALL_ARGS = {"tee"}
WRITERS_INPLACE = {"sed", "perl", "ruby", "gsed"}
WRITERS_LAST_ARG = {"cp", "mv", "install", "rsync", "ln"}
WRITERS_TRUNC = {"truncate", "shred"}









ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
VAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
VAR_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")


def collect_env(segments):
    'var -> every literal value a `VAR=value` word or a `for VAR in a b c`\n    item gives it, anywhere in the command. Order-independent and safe to\n    over-collect: a caller only ADDS candidate targets from this, so an extra\n    binding costs an unnecessary check, never a missed one. A value that\n    itself holds an unresolved `$OTHER` is kept as-is -- one substitution\n    pass here, not a recursive resolver -- so a target built from it still\n    shows the remaining `$OTHER` to resolve_candidates, which judges what it\n    can and leaves the rest alone.'
    env = {}
    for _d, toks, _sep in segments:
        words = [t.text for t in toks]
        for w in words:
            if ASSIGNMENT.match(w):
                var, val = w.split("=", 1)
                env.setdefault(var, []).append(val)
        if len(words) > 2 and words[0] == "for" and words[2] == "in":
            for item in words[3:]:
                env.setdefault(words[1], []).append(item)
    return env


def expand_literal(text, env, limit=16):
    "Every way `text` reads once its variables resolve as far as they can.\n\n    `${VAR:-default}` resolves first, from the hook process's own environment\n    (HOME, TMPDIR, CLAUDE_CODE_TMPDIR, or any other var a caller's process has\n    set) when it is set and non-empty, else the literal default -- the same\n    rule a running shell applies to `:-`. Bare `$VAR` / `${VAR}` are then\n    substituted from `env` (local assignments and for-loop items) to a fixed\n    point; a variable in neither is left exactly as written, `$NAME` or\n    `${NAME}`, so a caller can tell what is still unknown from what resolved."
    def sub_default(m):
        val = os.environ.get(m.group(1))
        return val if val else m.group(2)
    text = VAR_DEFAULT.sub(sub_default, text)

    out = [text]
    for _ in range(6):
        grown = []
        for w in out:
            m = next((m for m in VAR_REF.finditer(w) if m.group(1) in env), None)
            if not m:
                grown.append(w)
                continue
            grown.extend(w[:m.start()] + v + w[m.end():] for v in env[m.group(1)])
        if grown == out:
            break
        out = grown[:limit]
    return out


def resolve_candidates(base, raw_text, env):
    'Targets worth judging for `raw_text`, which may hold shell variables.\n\n    A candidate that fully resolves (no `$` left after expand_literal) is a\n    real path, normalized and made absolute against `base` exactly as a plain\n    literal would be. One that still holds an unresolved variable is judged\n    by the LITERAL PREFIX before that `$`, and only when the prefix is itself\n    absolute or starts with `~` -- `/tmp/err_$mid` proves outside-home from\n    `/tmp/err_` alone, no matter what `$mid` turns out to be, while a bare\n    `$mid` or a relative `err_$mid` proves nothing about where the finished\n    path lands and is left out. That residual gap is real and documented at\n    the top of block-write-outside-home.py: a target built from a value this\n    module cannot see ahead of time, such as `$(...)` command substitution,\n    stays unresolvable and is allowed.'
    out = []
    for cand in expand_literal(raw_text, env):
        cand = cand.strip()
        if not cand or cand.startswith("&") or cand.startswith("/dev/"):
            continue
        i = cand.find("$")
        if i == -1:
            t = os.path.expanduser(cand)
            if not os.path.isabs(t):
                t = os.path.join(base, t)
            out.append(os.path.normpath(t))
            continue
        prefix = cand[:i]
        if prefix.startswith("/"):
            out.append(os.path.normpath(prefix))
        elif prefix.startswith("~"):
            out.append(os.path.normpath(os.path.expanduser(prefix)))
        
        
    return out


def write_targets(command, cwd=None):
    "Absolute paths a shell command writes, as far as its argv shows.\n\n    Shared so every write guard reads the same targets (require-worktree-edit-bash\n    and block-write-outside-home).\n    Covers redirects, `dd of=`, tee, in-place sed/perl/ruby, the destination of\n    cp/mv/install/rsync/ln, truncate and shred. A write made inside an\n    interpreter or a script file is invisible.\n\n    A target holding a shell variable is judged, not skipped: resolve_candidates\n    substitutes what collect_env can resolve (assignments earlier in the same\n    command, `for x in a b` items, `${VAR:-default}` from this process's own\n    environment) and, for what is left, the literal prefix ahead of the first\n    remaining `$` when that prefix alone already proves outside home. What it\n    genuinely cannot resolve -- a value from `$(...)`, a variable this command\n    never assigns -- is left unjudged; see block-write-outside-home.py's\n    header for that residual gap.\n\n    Ambiguous argv words must LOOK like a file: they exist, or their parent\n    directory does. A sed expression such as `s/a/b/` fails both. Redirect and\n    of= targets skip that test, because their position already proves a path."
    base = cwd or os.getcwd()
    redirects, segments = parse(command, base)
    env = collect_env(segments)

    def resolve(d, t):
        t = t.strip()
        if not t or t.startswith("&") or t.startswith("/dev/"):
            return []
        if "$" not in t:
            e = os.path.expanduser(t)
            if not os.path.isabs(e):
                e = os.path.join(d, e)
            return [os.path.normpath(e)]
        return resolve_candidates(d, t, env)

    targets, maybe = [], []
    for d, tok in redirects:
        targets += resolve(d, tok.text)

    for seg in segments:
        d, argv = seg[0], seg[1]
        for t in argv:
            if t.text.startswith("of="):
                targets += resolve(d, t.text[3:])
        k = 0
        while k < len(argv) and ("=" in argv[k].text and not argv[k].text.startswith("-")):
            k += 1
        for pre in ("sudo", "command", "nohup", "env", "time", "xargs"):
            if k < len(argv) and os.path.basename(argv[k].text) == pre:
                k += 1
        if k >= len(argv):
            continue
        name = os.path.basename(argv[k].text)
        rest = argv[k + 1:]
        words = [t.text for t in rest
                 if t.text and not t.text.startswith("-")
                 and not (not t.quoted and ("*" in t.text or "?" in t.text))]
        opts = [t.text for t in rest if t.text.startswith("-")]
        if name in WRITERS_ALL_ARGS:
            maybe += [(d, w) for w in words]
        elif name in WRITERS_INPLACE:
            if any(o.startswith("-i") or o == "--in-place" for o in opts):
                maybe += [(d, w) for w in words if not sed_script(w)]
        elif name in WRITERS_LAST_ARG:
            if len(words) >= 2:
                maybe.append((d, words[-1]))
        elif name in WRITERS_TRUNC:
            maybe += [(d, w) for w in words]

    for d, w in maybe:
        for p in resolve(d, w):
            if os.path.exists(p) or os.path.isdir(os.path.dirname(p)):
                targets.append(p)
    return sorted(set(targets))















_PS_SEPARATORS = (";", "\n", "|", "&&", "||", "{", "}", "(", ")")
_PS_REMOVE = {"remove-item", "ri", "rm", "rmdir", "del", "erase", "rd"}
_PS_WRITE = {"set-content", "sc", "add-content", "ac", "out-file", "tee-object", "tee",
             "new-item", "ni"}
_PS_COPY = {"copy-item", "cpi", "copy", "cp"}
_PS_MOVE = {"move-item", "mi", "move", "mv"}
_PS_CD = {"set-location", "sl", "cd", "chdir", "push-location", "pushd"}

_CMD_SWITCH = re.compile(r"^/[A-Za-z]$")

_PS_NESTING = 4

_POSIX_SPECIAL = re.compile(r"[\s;&|()<>'\"`\\!{}\[\]*?#~]")


def _ps_words(command):
    '[(kind, text, quoted)]: kind "word", "sep" or "redir" (text is the operator).'
    out, s, i, n = [], command, 0, len(command)
    buf, quoted, started = [], False, False

    def end():
        nonlocal buf, quoted, started
        if started:
            out.append(("word", "".join(buf), quoted))
        buf, quoted, started = [], False, False

    while i < n:
        c = s[i]
        if c == "`" and i + 1 < n:                       
            if s[i + 1] != "\n":
                buf.append(s[i + 1])
                started = True
            i += 2
            continue
        if c == "'":
            j, part = i + 1, []
            while j < n:
                if s[j] == "'" and j + 1 < n and s[j + 1] == "'":
                    part.append("'"); j += 2; continue
                if s[j] == "'":
                    break
                part.append(s[j]); j += 1
            buf.append("".join(part)); quoted = started = True
            i = j + 1
            continue
        if c == '"':
            j, part = i + 1, []
            while j < n:
                if s[j] == "`" and j + 1 < n:
                    part.append(s[j + 1]); j += 2; continue
                if s[j] == '"' and j + 1 < n and s[j + 1] == '"':
                    part.append('"'); j += 2; continue
                if s[j] == '"':
                    break
                part.append(s[j]); j += 1
            buf.append("".join(part)); quoted = started = True
            i = j + 1
            continue
        if c == "<" and s.startswith("<#", i):          
            j = s.find("#>", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == "#" and not started:
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c in " \t\r":
            end(); i += 1; continue
        if c in "0123456789*" and not started and i + 1 < n and s[i + 1] == ">":
            end()
            j = i + 2
            if j < n and s[j] == ">":
                j += 1
            if j < n and s[j] == "&":                    
                i = j + 2
                continue
            out.append(("redir", ">", False)); i = j
            continue
        if c == ">":
            end()
            i += 2 if s.startswith(">>", i) else 1
            out.append(("redir", ">", False))
            continue
        sep = next((op for op in ("&&", "||") if s.startswith(op, i)), None) or (
            c if c in ";\n|{}()" else None)
        if sep:
            end(); out.append(("sep", sep, False)); i += len(sep)
            continue
        if c == "&" and not started:                     
            end(); i += 1
            continue
        buf.append(c); started = True; i += 1
    end()
    return out


def _ps_split_args(words, value_params):
    '(named {param: [values]}, positional [values], switches [stems]) of one statement.'
    named, positional, switches, k = {}, [], [], 0
    while k < len(words):
        text, quoted = words[k]
        if not quoted and text.startswith("-") and len(text) > 1:
            stem, _, inline = text[1:].partition(":")
            name = next((p for p in value_params if p.startswith(stem.lower())), None)
            if name is not None:
                if inline:
                    named.setdefault(name, []).append((inline, quoted))
                elif k + 1 < len(words):
                    named.setdefault(name, []).append(words[k + 1])
                    k += 1
            else:
                switches.append(stem.lower())
            k += 1
            continue
        positional.append((text, quoted))
        k += 1
    return named, positional, switches


def _ps_values(pairs):
    "Split PowerShell's comma lists: `Remove-Item a,b` names two paths."
    out = []
    for text, quoted in pairs:
        out += [(part, quoted) for part in (text.split(",") if not quoted else [text]) if part]
    return out


def _ps_statement(words):
    "One PowerShell statement's words, as the POSIX words that do the same to files."
    if not words:
        return words
    head = words[0][0].lower().rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    head = head[:-4] if head.endswith(".exe") else head
    rest = words[1:]
    if head in _PS_REMOVE:
        
        
        
        cmd_switches = [w[0].lower() for w in rest if not w[1] and _CMD_SWITCH.match(w[0])]
        rest = [w for w in rest if w[1] or not _CMD_SWITCH.match(w[0])]
        _, _, switches = _ps_split_args(rest, ())
        targets = _ps_values([w for w in rest if w[1] or not w[0].startswith("-")])
        recurse = "/s" in cmd_switches or any("recurse".startswith(sw) for sw in switches if sw)
        return [("rm", False)] + ([("-rf", False)] if recurse else []) + targets
    if head in _PS_WRITE:
        named, positional, switches = _ps_split_args(rest, ("path", "literalpath", "filepath",
                                                            "value", "itemtype", "name",
                                                            "encoding", "variable"))
        targets = _ps_values(named.get("path", []) + named.get("literalpath", [])
                             + named.get("filepath", []) + positional[:1])
        kind = (named.get("itemtype") or [("", False)])[0][0].lower()
        if head in ("new-item", "ni") and kind and "directory".startswith(kind):
            return [("mkdir", False)] + targets
        return [("tee", False)] + targets
    if head in _PS_COPY or head in _PS_MOVE:
        named, positional, switches = _ps_split_args(rest, ("path", "literalpath",
                                                            "destination", "filter",
                                                            "include", "exclude"))
        sources = _ps_values(named.get("path", []) + named.get("literalpath", [])) or positional[:1]
        dest = named.get("destination", []) or positional[1:2] or positional[len(sources):][:1]
        return [("cp" if head in _PS_COPY else "mv", False)] + sources + dest
    if head in _PS_CD:
        named, positional, _ = _ps_split_args(rest, ("path", "literalpath"))
        return [("cd", False)] + (named.get("path") or named.get("literalpath") or positional)[:1]
    
    
    
    
    program, quoted = words[0]
    if quoted and not ("/" in program or "\\" in program or program.lower().endswith(".exe")):
        return words
    return [(head, False)] + rest


def _ps_nested(words):
    'The command string a statement hands to another shell, or None: `iex "..."`,\n    `Invoke-Expression`, `pwsh -c "..."` / `powershell -Command` (and -EncodedCommand), and\n    `cmd /c ...`. Without this a destructive git command inside one reached no guard.'
    if not words:
        return None
    head = words[0][0].lower().rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    head = head[:-4] if head.endswith(".exe") else head
    rest = words[1:]
    if head in ("iex", "invoke-expression"):
        named, positional, _ = _ps_split_args(rest, ("command",))
        found = named.get("command") or positional
        return " ".join(t for t, _ in found) if found else None
    if head in ("pwsh", "powershell"):
        for k, (text, quoted) in enumerate(rest):
            if quoted or not text.startswith("-"):
                continue
            stem = text[1:].lower()
            encoded = stem in ("e", "ec") or (len(stem) >= 2 and "encodedcommand".startswith(stem))
            if encoded and k + 1 < len(rest):
                try:
                    return base64.b64decode(rest[k + 1][0]).decode("utf-16-le")
                except (ValueError, UnicodeDecodeError):
                    return None
            if stem and "command".startswith(stem):
                return " ".join(t for t, _ in rest[k + 1:]) or None
        return None
    if head == "cmd":
        for k, (text, quoted) in enumerate(rest):
            if not quoted and text.lower() in ("/c", "/k"):
                return " ".join(t for t, _ in rest[k + 1:]) or None
    return None


def _posix_word(text, quoted):
    "A word written for scan(): forward slashes for backslashes, PowerShell's\n    `$env:NAME` as `$NAME`, and quotes kept on what was quoted (a quoted word is data)."
    text = re.sub(r"\$\{?env:([A-Za-z_][A-Za-z0-9_]*)\}?", r"${\1}", text.replace("\\", "/"))
    if quoted or _POSIX_SPECIAL.search(text) or not text:
        if "$" in text:
            return '"%s"' % re.sub(r'(["`\\])', r"\\\1", text)
        return "'%s'" % text.replace("'", "'\\''")
    return text


def powershell_as_posix(command, _depth=0):
    'The POSIX shell command a guard should judge for a PowerShell command (see above).'
    statements, words, out = [], [], []
    for kind, text, quoted in _ps_words(command or ""):
        if kind == "sep":
            statements.append((words, text))
            words = []
        else:
            words.append((kind, text, quoted))
    statements.append((words, ""))
    for words, sep in statements:
        plain, redirects = [], []
        k = 0
        while k < len(words):
            kind, text, quoted = words[k]
            if kind == "redir":
                if k + 1 < len(words) and words[k + 1][0] == "word":
                    redirects.append(words[k + 1][1:])
                    k += 1
            else:
                plain.append((text, quoted))
            k += 1
        nested = _ps_nested(plain) if _depth < _PS_NESTING else None
        if nested is not None:
            parts = [powershell_as_posix(nested, _depth + 1)]
        else:
            parts = [_posix_word(t, q) for t, q in _ps_statement(plain)]
        parts += ["> " + _posix_word(t, q) for t, q in redirects]
        if parts:
            out.append(" ".join(parts))
        if sep:
            out.append({"{": ";", "}": ";", "\n": ";"}.get(sep, sep))
    return " ".join(out).strip()


def main(argv):
    cmd, cwd = "", None
    i = 1
    while i < len(argv):
        if argv[i] == "--command" and i + 1 < len(argv):
            cmd = argv[i + 1]; i += 2; continue
        if argv[i] == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]; i += 2; continue
        i += 1

    if not cmd:
        try:
            d = json.load(sys.stdin)
        except Exception:
            d = None
        cmd, payload_cwd = payload_command(d)
        cwd = cwd or payload_cwd

    joined = cmd.replace("\\\n", " ") if cmd else ""
    redirects, segments = parse(cmd, cwd)
    out = {
        "command": strip_heredocs(joined) if joined else "",
        "redirects": [
            {"dir": d, "text": t.text, "quoted": t.quoted} for d, t in redirects
        ],
        "segments": [
            {"dir": d,
             "sep": sep,
             "argv": [{"text": t.text, "quoted": t.quoted} for t in toks]}
            for d, toks, sep in segments
        ],
    }
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        
        
        json.dump({"command": "", "redirects": [], "segments": []}, sys.stdout)
        sys.stdout.write("\n")
        sys.exit(0)
