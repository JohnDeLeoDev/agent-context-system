#!/usr/bin/env python3

'PreToolUse: an agent may not mint its own consent grant.\n\nWhy this exists. guard-git-write\'s last check honors a single-use token at\n$XDG_STATE_HOME/agent-context/git-write-consent, minted only on the user\'s say-so\n(by approval-question, when user picks Approve on an approval question). The\nconsent script that mints it sits on disk where an agent can run it. Without this\nhook an agent could read the deny, run the script, get a valid token and retry the\nwrite, and git-write-consent.log would record a user authorization that no user\ngave.\n\nSecond grant. block-write-outside-home honors approvals in\n$XDG_STATE_HOME/agent-context/write-outside-home-consent, created on user\'s approval\n(write-outside-home-consent.py). The same reasoning applies and the same checks run\nfor both names. TOKENS maps each grant file name to its refusal text.\n\nThird grant: unlocking an acceptance test. test-lock.py locks a task\'s\ntests in $XDG_STATE_HOME/agent-context/test-locks/, and a lock comes off only on\nuser\'s approval, through test-lock-consent.py. Here the protected thing is the\nmanifest directory, and deleting a manifest is the weakening act, so `test-locks`\nis also in REMOVAL_WEAKENS. For the other two grants a delete is a revoke and stays\nallowed.\n\nWhat this can and cannot do. An agent with a shell could always write a grant file\nitself, so the consent scripts buy friction and an audit trail and no cryptographic\nauthority. This hook does not change that. A determined agent has other routes: a\npython one-liner, some tool this matcher never sees. What this supplies is the\nfriction: self-authorization stops being the obvious next step.\n\nBoth routes are blocked, because blocking only the script would be for show:\n  - running the consent script to mint a grant\n  - writing the grant file directly (redirect, tee, cp, mv, touch, Write/Edit)\n\nStill allowed:\n  --revoke  destroys a grant. A tightening; never block it.\n  --log     reads the audit trail. A guard must not hide the trail it protects.\n  --list    reads the active grants, or the active test locks.\n  --help    reads the script\'s own header.\n  Anything naming a .log file and not the grant itself.\n\nName matching. Matching literal text alone would let these spellings through:\n`X=git-write-consent.py; $X`, `eval "git-write""-consent.py"`\nand `$(echo git-write-consent.py)` as the command. So a name is looked for with\nquotes removed and with the command\'s own variable assignments substituted, a\n`$VAR` command word is expanded and re-read, and a command word built from a\nsubstitution whose text names a grant counts as running it. Variables the command\nnever assigns come from the environment and are left alone, which keeps\n`$PY x.py | grep git-write-consent` allowed.\n\nDynamic values. ANSI-C `$\'...\'` is decoded before anything else\nruns, so `$\'git-write-consent.py\'` and its hex or octal escapes read the same as\nthe plain name everywhere downstream. `read`, `printf -v`, `source`/`.` of a file\nthis hook cannot read, and `$(cat f)`/`` `cat f` `` cannot be resolved to a value\nahead of time. Those are denied conservatively: `dynamic_risk()`\nrefuses a command that sources an unread file, or reads a value and separately\nruns a shell interpreter or eval, while the command also names a consent-script\nfragment or the grants state directory. This is co-occurrence; dataflow is not\nproven. It does not confirm the dynamic value is what reaches the interpreter,\nso it can refuse a command where the two are unrelated. That trade favors a false\nrefusal over an unseen bypass. A read-only grep or rg naming a script still passes,\nsince it runs neither an interpreter nor eval.\n\nFail mode, which is split. This hook runs on every Bash call and\nevery file write, so failing closed on everything it cannot parse would brick\nsessions. So: open on an unparseable payload, except when the raw text names a\ngrant, where it fails closed.'
import json
import os
import re
import shlex
import sys

GIT_DENY = """Blocked: minting a git-write consent token is the user's action.

That token authorizes one git write that guard-git-write would otherwise refuse.
It is single-use and every use is logged. If an agent mints it, the log records
an authorization user never gave -- which is a worse outcome than the write that
was blocked, because it corrupts the one record that exists to answer "did he
approve this?"

What to do: ask user with one AskUserQuestion. Header "Approval", options
"Approve" (its description names the single operation and why) and "Deny", and the
question:

    Allow one git commit, push or merge in <repo top level> within the next <1-120> minutes? [approval:git-write:<repo top level>:<minutes>]

When he picks Approve, the approval-question hook mints the token. user never types
a command for this.

First, check that a token is needed. These are already allowed without one:
landing a completed worktree through the project's wt-finish.sh, a
bookkeeping-only commit or push, and the first commit in a repo with no commits
yet. guard-git-write's own refusal names the exemption that did not apply -- read
that before asking a human for anything.

Do not route around this by writing the token file some other way. What is
blocked is forging the authorization, not the shape of the command that does it.
"""

WRITE_DENY = """Blocked: approving writes outside the home directory is user's action.

A write-outside-home grant lets agents write under one directory outside $HOME
for a limited time, and every use is logged. If an agent creates it, the log
records an approval user never gave.

Ask user with one AskUserQuestion. Header "Approval", options "Approve" (its
description says why) and "Deny", and the question:

    Allow agent writes under <resolved dir outside home> for the next <1-240> minutes? [approval:write-outside-home:<resolved dir outside home>:<minutes>]

When he picks Approve, the approval-question hook creates the grant.

First check whether the write can stay inside home. <working dir>/.agents/tmp/ and
~/.local/state/agent-scratch/<session>/ need no approval.

Do not route around this by writing the grant file some other way.
"""

LOCK_DENY = """Blocked: unlocking an acceptance test is user's action.

A task's tests are locked once they are agreed and seen failing. Removing a lock,
or editing the lock manifest, accepts a change to a test the task agreed on. If an
agent does it, the unlock log records an approval user never gave.

If a locked test is wrong, tell user which assertion and why. In Claude Code,
ask with AskUserQuestion (header "Approval", options "Approve" and "Deny").
In Codex, use request_user_input_async ("Approve (Recommended)", "Deny").
In opencode, use the question tool: one call holding only this question, header
"Approval", options "Approve" and "Deny".
The exact question text for every harness is:

    Unlock the locked test <absolute path>? [approval:test-unlock:<absolute path>:0]

When he picks Approve, the corresponding approval hook removes the lock.
Reading the locks is allowed:

    python3 ~/.agent-context/global/scripts/test-lock.py status <checkout>

Do not route around this by writing the manifest some other way.
"""

MARK_DENY = """Blocked: approval marks are written by the approval-question hook, never by you.

PreToolUse leaves a mark for each approval question it let through, and PostToolUse
grants only when it finds one. A mark written by an agent would let an answer that
never came from user's dialog count as his. Ask the approval question and let the
hooks run.
"""

REPLY_DENY = """Blocked: answering an opencode question is user's action.

opencode shows a question to user as a form, and its server takes the answer on a
reply route. A command that calls that route answers for him. If the question is an
approval, the grant that follows records an approval user never gave.

Ask with the question tool and wait for his answer. Reading a form or a session
through the API is allowed; replying to one is not.
"""

TOKENS = {
    "approval-marks": MARK_DENY,
    "git-write-consent": GIT_DENY,
    "write-outside-home-consent": WRITE_DENY,
    "test-lock-consent": LOCK_DENY,
    "codex-test-unlock": LOCK_DENY,
    "opencode-approval": LOCK_DENY,
    "test-locks": LOCK_DENY,
}






REPLY_ROUTE = re.compile(r"/(?:form|question)/[^\s/]*/reply\b")








RUN_STORE_TASK = "mcp__agent-context__run_store_task"
CONSENT_READ_ONLY_ARGS = {"--help", "-h", "--log", "-l", "--list"}
TEST_LOCK_REMOVAL_SUBCOMMANDS = frozenset()


def _consent_task_removes_lock(args):
    if not isinstance(args, list) or not args:
        return False
    first = args[0]
    return not (isinstance(first, str) and first in CONSENT_READ_ONLY_ARGS)


def _test_lock_task_removes_lock(args):
    return (isinstance(args, list) and bool(args) and isinstance(args[0], str)
            and args[0] in TEST_LOCK_REMOVAL_SUBCOMMANDS)



REMOVAL_WEAKENS = {"test-locks"}

INTERPRETER = re.compile(r"\b(?:python3?|perl|ruby|node)\b")

WRITES = re.compile(
    r"\bopen\s*\(|\.write\b|write_text|write_bytes|\bremove\s*\(|unlink|\brename\s*\(|"
    r"\breplace\s*\(|rmtree|copyfile|shutil\.|symlink|\.touch\s*\(|truncate|"
    r"json\.dump\s*\(|writeFile|appendFile|rmSync|renameSync|File\.write|File\.open")

RUNS = re.compile(r"subprocess|os\.system|os\.exec|popen|spawn|execSync|\bsystem\s*\(|`")





FRAGMENT = re.compile(r"consent|test-lock|approval-mark")
GRANTS_DIR = re.compile(r"state[/\\]agent-context\b")
INTERP_OR_EVAL = re.compile(r"\b(?:sh|bash|zsh|ksh|dash|python3?|eval)\b")

DYNAMIC_DENY = """Blocked: this command reads a value this hook cannot see (from `read`,
`printf -v`, or a file's own contents) and separately runs a shell interpreter or `eval`,
while naming a consent grant. Static analysis cannot rule out that it mints or writes the
grant, so it is refused.

Ask user with one AskUserQuestion instead. Header "Approval", options "Approve" and "Deny",
with the question shape for the grant this touches. See guard-git-write,
block-write-outside-home or block-locked-test-edit's own refusal for the exact wording.

If this command has nothing to do with a grant, drop the read/printf -v/file step, or the
interpreter/eval, or the shell here, and it will run without being refused.
"""


def deny(name):
    sys.stderr.write(TOKENS[name])
    sys.exit(2)


def deny_dynamic():
    sys.stderr.write(DYNAMIC_DENY)
    sys.exit(2)


def strip_heredoc_bodies(cmd):
    'Return `cmd` with heredoc bodies removed, command lines kept.\n\n    A heredoc body is content the shell hands to a program, and the shell does\n    not run it, so documentation that quotes the consent command is allowed.\n    The delimiter line is kept so the surrounding command stays intact.\n\n    A `<<<` herestring is not a heredoc: its argument is the rest of the same\n    line. The lookaround in the opener regex keeps `<<<` from matching as an\n    opener. Without it `<<< "git-write-consent.py"` reads as opener `<<` with\n    terminator `git`, since `-` ends a bare word match, and everything after it\n    is swallowed as a heredoc body, which hides a consent-script invocation on a\n    later line from every check below.'
    lines = cmd.split("\n")
    kept = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        opener = re.search(r"(?<!<)<<-?(?!<)\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", line)
        if not opener:
            continue
        term = opener.group(1)
        while i < len(lines) and lines[i].strip() != term:
            i += 1
        if i < len(lines):
            kept.append(lines[i])
            i += 1
    return "\n".join(kept)


OPERATORS = set(";&|()\n")
REDIRECTS = set("<>")

RESERVED = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "time"}

PREFIXES = {"env", "exec", "nohup", "sudo", "command", "builtin", "nice", "timeout",
            "xargs", "caffeinate"}
SHELLS = {"sh", "bash", "zsh", "ksh", "dash"}
PYTHON = re.compile(r"^python[0-9.]*$")
PY_VALUE_FLAGS = {"-X", "-W", "-Q"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SAFE_SUBCOMMANDS = {"--revoke", "-r", "--log", "-l", "--list", "--help", "-h"}


SUBST = "$()"
VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
ASSIGNED = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|)]*)")


_ANSI_C_ESCAPES = {
    "\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
    "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "?": "?",
}
ANSI_C_QUOTE = re.compile(r"\$'((?:[^'\\]|\\.)*)'")


def _decode_ansi_c_body(body):
    "One ANSI-C `$'...'` body, decoded the way bash decodes it: a recognized\n    backslash escape becomes the character it names. An unrecognized one is left\n    exactly as written."
    out = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nc = body[i + 1]
        if nc in _ANSI_C_ESCAPES:
            out.append(_ANSI_C_ESCAPES[nc])
            i += 2
        elif nc == "x":
            m = re.match(r"[0-9A-Fa-f]{1,2}", body[i + 2:i + 4])
            if m:
                out.append(chr(int(m.group(), 16)))
                i += 2 + len(m.group())
            else:
                out.append(c)
                i += 1
        elif nc in "01234567":
            
            m = re.match(r"[0-7]{1,3}", body[i + 1:i + 4])
            assert m is not None
            out.append(chr(int(m.group(), 8) & 0xFF))
            i += 1 + len(m.group())
        elif nc in "uU":
            width = 4 if nc == "u" else 8
            m = re.match(r"[0-9A-Fa-f]{1,%d}" % width, body[i + 2:i + 2 + width])
            if m:
                out.append(chr(int(m.group(), 16)))
                i += 2 + len(m.group())
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def decode_ansi_c(text):
    "`text` with every ANSI-C `$'...'` literal replaced by the string it decodes\n    to, the same text a running shell would see. Applied once, at the top of the\n    dispatch, so a script name spelled inside one (`$'git-write-consent.py'`, hex\n    or octal escapes included) reads like the plain name everywhere downstream."
    return ANSI_C_QUOTE.sub(lambda m: _decode_ansi_c_body(m.group(1)), text)


def dequote(text):
    '`text` with quote characters and backslashes removed, so a name split across\n    adjacent quotes ("git-write""-consent.py") reads as the one word the shell joins.'
    return re.sub(r"[\"'\\]", "", text)


def expansions(text, env):
    'Every spelling of `text` with the variables in `env` substituted, `text`\n    itself excluded. A variable absent from `env` comes from the environment, not\n    from this command, and stays as written.'
    out = [text]
    for _ in range(6):
        grown = []
        for w in out:
            m = next((m for m in VAR.finditer(w) if m.group(1) in env), None)
            if not m:
                grown.append(w)
                continue
            grown.extend(w[:m.start()] + v + w[m.end():] for v in env[m.group(1)])
        if grown == out:
            break
        out = grown[:16]
    return [w for w in out if w != text]


def mentions(text, name, env=None):
    'True when `text` names `name` once quotes are removed and its variables,\n    those it assigns itself plus `env`, are substituted.'
    flat = dequote(text)
    scope = {k: list(v) for k, v in (env or {}).items()}
    for m in ASSIGNED.finditer(flat):
        scope.setdefault(m.group(1), []).append(m.group(2))
    return any(name in t for t in [flat] + expansions(flat, scope))


def _balanced(text, i, close):
    'Index just past the `close` that ends the substitution opened before `i`.'
    depth = 1
    quote = ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = ""
        elif c == "\\":
            i += 1
        elif c in "'\"" and close == ")":
            quote = c
        elif close == "`" and c == "`":
            return i + 1
        elif close == ")" and c == "(":
            depth += 1
        elif close == ")" and c == ")":
            depth -= 1
            if not depth:
                return i + 1
        i += 1
    return i


def shell_tokens(text):
    'Split shell text into ("word", w) / ("op", o) / ("redir", r) tokens, plus the\n    bodies of every $(...) and `...` it holds.\n\n    Quotes are honored, so `|` inside \'(a|b)\' is part of one word, not a pipe.\n    A substitution is returned separately because its body is a command in its\n    own right, even inside double quotes.'
    tokens, subs = [], []
    word, in_word = [], False
    i, n = 0, len(text)

    def end_word():
        if in_word:
            tokens.append(("word", "".join(word)))
        word.clear()
        return False

    while i < n:
        c = text[i]
        if c in " \t\r":
            in_word = end_word()
            i += 1
        elif c == "\\":
            if i + 1 < n and text[i + 1] != "\n":
                word.append(text[i + 1])
                in_word = True
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            word.append(text[i + 1:j])
            in_word = True
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    word.append(text[i + 1])
                    i += 2
                elif text.startswith("$(", i) or text[i] == "`":
                    close = ")" if text[i] == "$" else "`"
                    start = i + (2 if close == ")" else 1)
                    i = _balanced(text, start, close)
                    subs.append(text[start:i - 1])
                    word.append(SUBST)
                else:
                    word.append(text[i])
                    i += 1
            in_word = True
            i += 1
        elif text.startswith("$(", i) or c == "`":
            close = ")" if c == "$" else "`"
            start = i + (2 if close == ")" else 1)
            i = _balanced(text, start, close)
            subs.append(text[start:i - 1])
            word.append(SUBST)
            in_word = True
        elif c == "#" and not in_word:
            while i < n and text[i] != "\n":
                i += 1
        elif c in REDIRECTS or (c == "&" and text.startswith("&>", i)):
            if in_word and "".join(word).isdigit():
                word.clear()
                in_word = False
            in_word = end_word()
            j = i + 1
            while j < n and text[j] in "<>&|-":
                j += 1
            tokens.append(("redir", text[i:j]))
            i = j
        elif c in OPERATORS:
            in_word = end_word()
            tokens.append(("op", c))
            i += 1
        else:
            word.append(c)
            in_word = True
            i += 1
    end_word()
    return tokens, subs


def runs_script(text, name, depth=0, env=None, sub_named=False):
    "True when shell `text` runs the consent script for `name` as a command.\n\n    Naming the script as an operand (grep, diff, ls, a quoted pattern) inspects it\n    and forges nothing. A word counts only in command position: first in a\n    command, after an operator, behind env assignments, a prefix such as env or\n    xargs, or an interpreter (sh, bash, python3, source, .), inside $(...) or\n    backticks, or as the text of `sh -c` or `eval`. Its read-only and revoking\n    subcommands stay allowed.\n\n    `env` holds the variables assigned so far, by this text or the text that holds\n    it. A command word that uses one is re-read with each value substituted. A\n    command word built from a substitution counts when the substitution's text\n    names the script (`sub_named`), since what it prints cannot be known here."
    if depth > 6:
        return False
    names = {name, name + ".sh", name + ".py"}
    tokens, subs = shell_tokens(text)
    env = {k: list(v) for k, v in (env or {}).items()}
    for k, (kind, tok) in enumerate(tokens):
        if kind != "word":
            continue
        if ASSIGNMENT.match(tok):
            var, value = tok.split("=", 1)
            env.setdefault(var, []).append(value)
        elif tok == "for" and k + 2 < len(tokens) and tokens[k + 2] == ("word", "in"):
            for item_kind, item in tokens[k + 3:]:
                if item_kind != "word":
                    break
                env.setdefault(tokens[k + 1][1], []).append(item)
    sub_named = sub_named or any(mentions(s, name, env) for s in subs)
    if any(runs_script(s, name, depth + 1, env, sub_named) for s in subs):
        return True

    def unsafe_after(k):
        nxt = tokens[k + 1] if k + 1 < len(tokens) else ("op", "")
        return not (nxt[0] == "word" and nxt[1] in SAFE_SUBCOMMANDS)

    def rest(k, quote):
        words = []
        for kind, tok in tokens[k + 1:]:
            if kind == "op":
                break
            words.append(shlex.quote(tok) if quote and kind == "word" else tok)
        return " ".join(words)

    state = "cmd"
    skip_next = False
    for k, (kind, tok) in enumerate(tokens):
        if kind == "op":
            state, skip_next = "cmd", False
            continue
        if kind == "redir":
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        base = os.path.basename(tok)
        if state == "shc":
            if runs_script(tok, name, depth + 1, env, sub_named):
                return True
            state = "arg"
            continue
        if state == "prefix":
            if tok.startswith("-") or ASSIGNMENT.match(tok) or tok[:1].isdigit():
                continue
            state = "cmd"
        if state in ("shell", "python", "source"):
            if tok.startswith("-") and state != "source":
                if state == "shell" and not tok.startswith("--") and "c" in tok:
                    state = "shc"
                elif state == "python" and tok in ("-c", "-m"):
                    state = "arg"
                elif state == "python" and tok in PY_VALUE_FLAGS:
                    skip_next = True
                continue
            state = "script"
        if state == "cmd":
            if ASSIGNMENT.match(tok) or tok in RESERVED:
                continue
            
            if any(runs_script(c + " " + rest(k, True), name, depth + 1, env, sub_named)
                   for c in expansions(tok, env)):
                return True
            if base in PREFIXES:
                state = "prefix"
                continue
            if base in SHELLS:
                state = "shell"
                continue
            if PYTHON.match(base):
                state = "python"
                continue
            if base in ("source", "."):
                state = "source"
                continue
            if base == "eval":
                
                if runs_script(rest(k, False), name, depth + 1, env, sub_named):
                    return True
                state = "arg"
                continue
            state = "script"
        if state == "script":
            spelled = [base] + [os.path.basename((c.split() or [""])[0])
                                for c in expansions(tok, env)]
            built = SUBST in tok and sub_named
            if (built or any(s in names for s in spelled)) and unsafe_after(k):
                return True
            state = "arg"
    return False


def bash_forges(cmd, name):
    'True when `cmd` runs the consent script for `name` or writes its grant file.'
    if not mentions(cmd, name):
        return False
    
    if not mentions(cmd.replace(name + ".log", ""), name):
        return False
    scan = strip_heredoc_bodies(cmd)
    if runs_script(scan, name):
        return True
    
    
    readings = (scan, dequote(scan))
    
    
    
    operand = r"[\w./~$-]*" + re.escape(name) + r"(?![\w.-])"
    writes = [
        re.compile(r"(?:>>?|\|\s*tee\b(?:\s+-\S+)*)\s*" + operand),
        re.compile(r"\b(?:tee|cp|mv|install|touch|ln|dd|sed\s+-i\S*)\b[^;&|]*?" + operand),
    ]
    if name in REMOVAL_WEAKENS:
        writes.append(re.compile(r"\b(?:rm|unlink|trash|shred)\b[^;&|]*?" + operand))
    if any(w.search(text) for w in writes for text in readings):
        return True
    
    
    
    
    for line in "\n".join(readings).split("\n"):
        interp = INTERPRETER.search(line)
        if not interp:
            continue
        code = line[interp.end():]
        script = re.escape(name) + r"\.(?:sh|py)\b"
        if not re.search(operand, code) and not re.search(script, code):
            continue
        if re.search(script, code) and RUNS.search(code):
            return True
        if WRITES.search(code):
            return True
    return False


def _sources_unknown_file(tokens):
    "True when a simple command's word is `source` or `.` -- interpreting a file\n    whose contents this hook cannot read."
    state = "cmd"
    for kind, tok in tokens:
        if kind == "op":
            state = "cmd"
            continue
        if kind == "redir":
            continue
        if state != "cmd":
            continue
        if ASSIGNMENT.match(tok) or tok in RESERVED:
            continue
        if os.path.basename(tok) in ("source", "."):
            return True
        state = "arg"
    return False


def _reads_dynamic_value(tokens):
    'True when a simple command is `read ...` or `printf -v ...` -- a value this\n    hook cannot know until the command actually runs.'
    state = "cmd"
    for kind, tok in tokens:
        if kind == "op":
            state = "cmd"
            continue
        if kind == "redir":
            continue
        if state == "cmd":
            if ASSIGNMENT.match(tok) or tok in RESERVED:
                continue
            base = os.path.basename(tok)
            if base == "read":
                return True
            if base == "printf":
                state = "printf"
                continue
            state = "arg"
            continue
        if state == "printf":
            if tok == "-v":
                return True
            if tok.startswith("-"):
                continue
            state = "arg"
    return False


def _cats_a_file(sub):
    "True when a command-substitution body starts a simple command with `cat` --\n    its output is a file's contents, unknown here."
    return bool(re.search(r"(?:^|[;&|\n]|&&|\|\|)\s*cat\b", sub))


def dynamic_risk(cmd):
    "Conservative fallback for what static analysis cannot resolve: a value from\n    `read`, `printf -v`, or a file's own contents, used toward a shell interpreter\n    or `eval`, while the command also names a consent grant by fragment or by its\n    state directory. Exact dataflow is not traced -- the two only have to appear in\n    the same command."
    scan = strip_heredoc_bodies(cmd)
    readings = (scan, dequote(scan))
    if not any(FRAGMENT.search(t) or GRANTS_DIR.search(t) for t in readings):
        return False
    tokens, subs = shell_tokens(scan)
    if _sources_unknown_file(tokens):
        return True
    if not any(INTERP_OR_EVAL.search(t) for t in readings):
        return False
    if _reads_dynamic_value(tokens):
        return True
    return any(_cats_a_file(s) for s in subs)


def calls_reply_route(cmd):
    'True when shell `cmd` names an opencode reply route outside a heredoc body,\n    with quotes removed and its own variable assignments substituted.'
    flat = dequote(strip_heredoc_bodies(cmd))
    scope = {}
    for m in ASSIGNED.finditer(flat):
        scope.setdefault(m.group(1), []).append(m.group(2))
    return any(REPLY_ROUTE.search(t) for t in [flat] + expansions(flat, scope))


raw = sys.stdin.read()




flat = dequote(raw)
named = [n for n in TOKENS if n in raw or n in flat]
if not named and '"Bash"' not in raw and RUN_STORE_TASK not in raw:
    sys.exit(0)

payload = None
try:
    payload = json.loads(raw)
except Exception:
    pass
if not isinstance(payload, dict):
    if named:
        deny(named[0])  
    sys.exit(0)

tool = payload.get("tool_name") or ""
ti = payload.get("tool_input") or {}
if not isinstance(ti, dict):
    ti = {}

if tool == "Bash":
    cmd = ti.get("command") or ""
    if isinstance(cmd, str):
        cmd = decode_ansi_c(cmd)
        for n in TOKENS:
            if bash_forges(cmd, n):
                deny(n)
        if dynamic_risk(cmd):
            deny_dynamic()
        if calls_reply_route(cmd):
            sys.stderr.write(REPLY_DENY)
            sys.exit(2)
    sys.exit(0)

if tool == RUN_STORE_TASK:
    task = ti.get("task")
    args = ti.get("args")
    if task == "test-lock-consent" and _consent_task_removes_lock(args):
        deny("test-lock-consent")
    if task == "test-lock" and _test_lock_task_removes_lock(args):
        deny("test-locks")
    sys.exit(0)





path = (ti.get("file_path") or ti.get("filePath")
        or ti.get("notebook_path") or ti.get("notebookPath") or "")
if path:
    for part in path.replace("\\", "/").split("/"):
        if part in TOKENS:
            deny(part)

sys.exit(0)
