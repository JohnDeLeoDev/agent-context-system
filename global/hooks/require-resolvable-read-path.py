#!/usr/bin/env python3

'require-resolvable-read-path -- turn a prompt for user into a correction for the agent.\n\n`permissions.deny` outranks `bypassPermissions`: a deny rule can never be waived, so when\nClaude Code analyzes a shell command that reads files and cannot statically resolve what it\nwould read, it refuses to decide alone and floors the decision at a permission prompt --\n"only you can approve running it anyway". The prompt appears on an ordinary text search:\n\n    grep on \'.agents/maintainability/COVERAGE.md\' after a cd would search a directory that\n    cannot be determined here, and a Read() deny rule is configured; only you can approve\n    running it anyway.\n\nAny Read() deny rule causes this. The trigger is a relative path:\nafter a `cd` inside the same command the harness cannot know the working directory, so a relative\noperand is unresolvable and it asks. An absolute operand is always resolvable and never prompts.\n\nSo this is not a security control -- it changes nothing about what may be read. It moves the\ninterruption off user and onto the agent. It corrects what it can and refuses the rest:\na relative operand becomes `cwd/operand`, `$HOME`\nand `$PWD` become their values, and the call runs with the rewritten command (PreToolUse\n`updatedInput`), with a note in context saying what changed so the agent writes it right\nnext time. What it cannot know it refuses: a variable nobody\nassigned, a command substitution, or a relative path after a `cd` whose target is not\nliteral (a single `cd` to a literal directory is resolved). The prompts are no\nmisconfiguration to fix with an allow rule (`Grep`/`Glob` are withheld\nso symbol questions go to the LSP), so the fix is a command the\nharness can clear.\n\nA rewrite is still a firing. eval-run and hook-firing-report count `REWROTE by` beside\n`BLOCKED by`, so the eval keeps measuring whether the agent wrote the path right itself;\nonly user\'s interruption is gone.\n\nScope is narrow -- `grep` and `rg` only. `cat`/`head`/`tail`/`sed -n` are\nrefused outright by `block-shell-file-read`, and a `grep` reading stdin (`ps | grep claude`)\nreads no file at all and must never be touched.\n\nOnly read positions are rewritten: the same spelling as an\nargument of ln/cp/mv/rm/mkdir/touch/install/rsync/tee, a `>`/`>>` target, a cd target or\nan argument of `git -C <dir>` is left as written. See WRITE_COMMANDS.\n\nFails open on any parse trouble: a guard that misfires on quoting is worse than no guard.'
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

READERS = {"grep", "rg", "egrep", "fgrep", "zgrep"}


TAKES_VALUE = {
    "-e", "-f", "-m", "-A", "-B", "-C", "-d", "--regexp", "--file", "--max-count",
    "--after-context", "--before-context", "--context", "--directories", "--devices",
    "--binary-files", "--color", "--colour", "--label", "--include", "--exclude",
    "--exclude-dir", "--exclude-from", "-g", "-t", "-T", "--glob", "--type",
    "--type-not", "--threads", "-j",
}

SPLIT = {"|", "||", "&&", ";", "&", "\n"}










SPLIT_CHARS = set(";|&()")






REDIRECTS = {">", ">>", "<", "<<", "<<<", ">|", "&>", "&>>", ">&"}


def segments(command):
    'Split a compound command into argv lists, one per invocation.\n\n    Newlines are split first and never handed to shlex: `whitespace_split` treats a\n    newline as ordinary whitespace, so `cd /p\\ngrep -n foo docs/a.md` lexed whole comes\n    back as one argv starting with `cd`, the `grep` disappears into the middle of it and\n    the guard sees nothing.\n\n    Uses shell-command-scan, the shared parser. shlex with punctuation_chars=True\n    emits `2`, `>` and `/dev/null` as ordinary tokens, so an operand scan over its\n    output would count a file descriptor and a redirect operator as files. The\n    shared scanner never emits a redirect target as argv, because it consumes the\n    operator and its target while it reads. The REDIRECTS filtering below is kept\n    only for the fallback path.\n\n    Falls back to the shlex lexer if the shared parser cannot be loaded. This guard\n    prevents a permission prompt and no damage; going silent would hand user the\n    interruptions it exists to stop.'
    try:
        import importlib.util
        scan_path = os.environ.get("SHELL_COMMAND_SCAN") or os.path.join(
            hp.scripts_dir(), "shell-command-scan.py")
        spec = importlib.util.spec_from_file_location("scs", scan_path)
        if spec is None or spec.loader is None:
            raise ImportError(scan_path)
        scs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scs)
        _redirects, segs = scs.parse(command, os.getcwd())
        return [[t.text for t in toks] for _d, toks, _sep in segs]
    except Exception:
        pass

    out = []
    for line in command.splitlines():
        if not line.strip():
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        current = []
        for token in lexer:
            if token in SPLIT or (token and set(token) <= SPLIT_CHARS):
                if current:
                    out.append(current)
                current = []
            else:
                current.append(token)
        if current:
            out.append(current)
    return out


ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
VAR = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def literal_assignments(parsed):
    '{NAME: value} for every NAME assigned a literal absolute path in this command.\n\n    In `S=/Users/user/.agent-context` followed by `grep ... "$S/global/..."` the path\n    is absolute, assigned one token earlier, and statically knowable. Refusing it\n    would tell the agent to re-issue with an absolute path it already wrote, which\n    leaves no way forward.\n\n    A name assigned more than once resolves to nothing: the guard would have to\n    track order to know which value reaches the operand, and guessing wrong here\n    hands user the prompt this hook exists to prevent. Only a literal `/` or `~`\n    value counts -- a command substitution is not statically knowable, which is the\n    whole premise of the check.'
    seen, banned = {}, set()
    for argv in parsed:
        for token in argv:
            m = ASSIGN.match(token)
            if not m:
                continue
            name, value = m.group(1), m.group(2)
            if name in seen:
                banned.add(name)
            
            
            ref = VAR.match(value)
            if ref and ref.group(1) in seen and ref.group(1) not in banned:
                base = seen[ref.group(1)]
                if base.startswith("/") or base.startswith("~"):
                    value = base + value[ref.end():]
            seen[name] = value
    return {n: v for n, v in seen.items()
            if n not in banned and (v.startswith("/") or v.startswith("~"))}


def strip_assignments(argv):
    'Drop leading `NAME=value` prefixes so the real command is argv[0].\n\n    `S=/x grep pat $S/y` puts the assignment in argv[0]. Without this the reader test\n    would see `S=/x`, match nothing, and pass the whole call unchecked.'
    i = 0
    while i < len(argv) and ASSIGN.match(argv[i]):
        i += 1
    return argv[i:]


def resolve(operand, variables):
    'The operand with a leading $VAR replaced by its literal value, or unchanged.'
    m = VAR.match(operand)
    if not m:
        return operand
    value = variables.get(m.group(1))
    if value is None:
        return operand
    return value + operand[m.end():]


def pattern_tokens(argv):
    'The pattern(s) of a grep/rg call: every `-e`/`--regexp` value, else the first\n    positional token. A path rewrite must never touch one.'
    rest, out, skip, after_ddash, positional = argv[1:], [], None, False, []
    for token in rest:
        if skip is not None:
            if skip in ("-e", "--regexp"):
                out.append(token)
            skip = None
            continue
        if not after_ddash and token == "--":
            after_ddash = True
            continue
        if not after_ddash and token.startswith("-") and token != "-":
            if token in TAKES_VALUE:
                skip = token
            elif token.startswith("--regexp="):
                out.append(token.split("=", 1)[1])
            continue
        positional.append(token)
    if out or "-f" in rest or "--file" in rest:
        return out
    return positional[:1]



FIND_VALUE_OPTS = {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename",
                   "-regex", "-iregex", "-lname", "-ilname"}


def option_values(argv):
    'Every option value in one invocation: the token after a value-taking option, and\n    the part after `=` in a glued `--opt=value`. A path rewrite must never touch one: it\n    is a glob, a name or a type, not a file. In `rg -g a.log pat a.log` the glob must\n    stay as written.'
    name = argv[0].rsplit("/", 1)[-1] if argv else ""
    takes = TAKES_VALUE if name in READERS else (FIND_VALUE_OPTS if name == "find" else set())
    out = set()
    for i, token in enumerate(argv[1:], start=1):
        if token.startswith("--") and "=" in token:
            out.add(token.split("=", 1)[1])
        elif token in takes and i + 1 < len(argv):
            out.add(argv[i + 1])
    return out


CD_WORD = re.compile(r"(?:^|(?<=[\s;&|(]))(?:cd|pushd)\s")










WRITE_COMMANDS = {"ln", "cp", "mv", "rm", "mkdir", "touch", "install", "rsync", "tee",
                  "truncate", "rmdir", "unlink", "chmod", "chown", "dd"}

_SEP = set(";|&()\n")


def segment_spans(command):
    '[(start, end)] of each simple command in the raw text, quote-aware.\n\n    Only used to decide which occurrence of an operand sits in which command; the\n    argv meaning still comes from shell-command-scan. A span this gets wrong can\n    only leave an occurrence unrewritten (see occurrence_is_read), never rewrite a\n    write target: a hit whose command it cannot name is left alone.'
    spans, start, i, quote, n = [], 0, 0, None, len(command)
    while i < n:
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = None
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if quote == '"':
            if c == '"':
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c in _SEP:
            if c == "&" and ((i > 0 and command[i - 1] in "<>")
                             or (i + 1 < n and command[i + 1] == ">")):
                i += 1
                continue
            spans.append((start, i))
            start = i + 1
        i += 1
    spans.append((start, n))
    return spans


def command_words(text):
    'The words of one simple command, leading `NAME=value` prefixes dropped, or None.'
    try:
        words = shlex.split(text, posix=True)
    except ValueError:
        return None
    while words and ASSIGN.match(words[0]):
        words = words[1:]
    return words


WRAPPERS = {"sudo", "env", "command", "nice", "nohup", "time"}


def occurrence_is_read(command, spans, pos):
    'May the occurrence of an operand starting at `pos` be rewritten?\n\n    False for a write target: a `>`/`>>` target, any argument of a\n    WRITE_COMMANDS command, a cd/pushd target, any argument of `git -C <dir>`\n    (it resolves against <dir>), and anything whose command cannot be named.'
    span = next(((s, e) for s, e in spans if s <= pos < e), None)
    if span is None:
        return False
    if re.search(r">\s*$", command[span[0]:pos]):
        return False                      
    words = command_words(command[span[0]:span[1]])
    if not words:
        return False
    while len(words) > 1 and words[0].rsplit("/", 1)[-1] in WRAPPERS:
        words = words[1:]
        while len(words) > 1 and (words[0].startswith("-") or ASSIGN.match(words[0])):
            words = words[1:]
    name = words[0].rsplit("/", 1)[-1]
    if name in WRITE_COMMANDS or name in ("cd", "pushd"):
        return False
    if name == "git" and any(w.startswith("-C") for w in words[1:]):
        return False
    return True


def rewrite(command, rewrites, patterns=()):
    '`command` with every whole-word occurrence of each rewritten operand replaced,\n    or None when a replacement would be a guess.\n\n    `rewrites` holds (operand, absolute, resolved_against_cd, reader). The operands\n    arrive un-quoted from the parser while the command is raw text, so the substitution\n    is textual, on whole words only (not glued to a path, a name or a `$`).\n\n    An operand may occur more than once: the same relative path read and then written\n    (`grep -qx x f || echo x >> f`), or read by two commands. Every occurrence resolves\n    against the same directory, so replacing all of them changes nothing the shell does.\n    Two shapes refuse:\n\n    - the operand is also a pattern or an option value (`grep -rn src src`,\n      `rg -g a.log pat a.log`, `find . -name a.log`): rewriting it would search or glob\n      for the absolute path instead of reading it;\n    - the operand was resolved against the one `cd` target and an occurrence sits before\n      that cd, where the other directory applies. The last textual cd is used, so a `cd`\n      quoted inside a later string can only cause a refusal, never a wrong rewrite.\n\n    An occurrence in a write position is never replaced:\n    see WRITE_COMMANDS and occurrence_is_read. The read occurrences still are, so\n    `grep -qx x f || echo x >> f` becomes `grep -qx x /abs/f || echo x >> f`, which the\n    shell resolves identically. The second return value is True when a write-position\n    occurrence was left as written.'
    first = {}
    for old, new, after_cd, _reader in rewrites:
        first.setdefault(old, (new, after_cd))
    kept_write = False
    for old, (new, after_cd) in first.items():
        if old in patterns:
            return None, False
        pat = re.compile(r"(?<![\w/.\-$@{}])" + re.escape(old) + r"(?![\w/.\-])")
        hits = list(pat.finditer(command))
        if not hits:
            return None, False
        if after_cd:
            cds = [m.start() for m in CD_WORD.finditer(command)]
            if not cds or hits[0].start() < cds[-1]:
                return None, False
        spans = segment_spans(command)
        reads = [m for m in hits if occurrence_is_read(command, spans, m.start())]
        if not reads:
            return None, False
        kept_write = kept_write or len(reads) < len(hits)
        
        for m in reversed(reads):
            command = command[:m.start()] + new + command[m.end():]
    return command, kept_write


def file_operands(argv):
    'The path operands of a grep/rg call: everything after the pattern that is not an option.'
    rest = argv[1:]
    
    
    
    cleaned, i = [], 0
    while i < len(rest):
        token = rest[i]
        
        
        stripped = token.lstrip("0123456789")
        if stripped in REDIRECTS and (stripped != token or token in REDIRECTS):
            i += 2                      
            continue
        if (token.isdigit() and i + 1 < len(rest)
                and rest[i + 1].lstrip("0123456789") in REDIRECTS):
            i += 3                      
            continue
        cleaned.append(token)
        i += 1
    rest = cleaned

    operands, skip, after_ddash = [], False, False
    seen_pattern = "-e" in rest or "--regexp" in rest or "-f" in rest or "--file" in rest
    for token in rest:
        if skip:
            skip = False
            continue
        if not after_ddash and token == "--":
            after_ddash = True
            continue
        if not after_ddash and token.startswith("-") and token != "-":
            if token in TAKES_VALUE:
                skip = True
            continue
        if not seen_pattern:
            seen_pattern = True
            continue
        operands.append(token)
    return operands


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        return 0

    try:
        parsed = segments(command)
    except Exception:
        return 0

    variables = literal_assignments(parsed)
    
    
    
    cwd = str(payload.get("cwd") or os.getcwd() or "").strip()
    cds = [i for i, a in enumerate(parsed) if strip_assignments(a)[:1] in (["cd"], ["pushd"])]
    has_cd = bool(cds)
    env = {"HOME": os.path.expanduser("~")}
    if cwd.startswith("/") and not has_cd:
        env["PWD"] = cwd

    
    
    
    
    
    
    
    cd_at, cd_dir = None, None
    if len(cds) == 1:
        cd_argv = strip_assignments(parsed[cds[0]])
        if len(cd_argv) == 2:
            target = resolve(resolve(cd_argv[1], variables), env)
            if target == "~" or target.startswith("~/"):
                target = env["HOME"] + target[1:]
            if (target.startswith("/") and "$" not in target and "`" not in target
                    and not any(c in target for c in "*?[")):
                cd_at, cd_dir = cds[0], os.path.normpath(target)

    offenders, from_variable, rewrites, patterns = [], [], [], set()
    for idx, argv in enumerate(parsed):
        argv = strip_assignments(argv)
        if not argv:
            continue
        name = argv[0].rsplit("/", 1)[-1]
        patterns.update(option_values(argv))
        if name not in READERS:
            continue
        try:
            operands = file_operands(argv)
        except Exception:
            return 0
        patterns.update(pattern_tokens(argv))
        for operand in operands:
            resolved = resolve(operand, variables)
            if resolved.startswith("/") or resolved.startswith("~"):
                continue                        
            via_env = resolve(operand, env)
            if via_env.startswith("/"):
                rewrites.append((operand, via_env, False, name))
                continue
            
            
            base = cwd if not has_cd else (
                cd_dir if cd_at is not None and idx > cd_at and cd_dir is not None else "")
            if "$" not in operand and "`" not in operand and base.startswith("/"):
                rewrites.append((operand, os.path.normpath(os.path.join(base, operand)), has_cd, name))
                continue
            offenders.append((name, operand))
            if VAR.match(operand):
                from_variable.append(operand)

    if not offenders and rewrites:
        fixed, kept_write = rewrite(command, rewrites, patterns)
        if fixed is not None and fixed != command:
            updated = dict(payload.get("tool_input") or {})
            updated["command"] = fixed
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated,
                "additionalContext": (
                    "REWROTE by require-resolvable-read-path: "
                    + "; ".join(f"`{a}` -> `{b}`" for a, b, _c, _n in rewrites[:4])
                    + ". Ran with the absolute path; write read paths absolute next time."
                    + (" The same spelling as a write target (ln/cp/mv/rm/mkdir/touch/"
                       "install/rsync/tee, a > or >> target) was left as written."
                       if kept_write else "")),
            }}))
            return 0
        
        
        offenders = [(n, a) for a, _b, _c, n in rewrites]

    if not offenders:
        return 0

    listed = ", ".join(f"`{n}` on `{p}`" for n, p in offenders[:4])
    
    
    
    if from_variable:
        print(
            "BLOCKED by require-resolvable-read-path: " + listed + ": path from a variable "
            "this command does not assign literally, so the harness would prompt user.\n"
            "Write the path out in full, or assign it literally in the same command:\n"
            "  S=/Users/you/proj; grep -rn 'pattern' \"$S/src\"\n"
            "A command substitution value is never resolvable.",
            file=sys.stderr,
        )
        return 2
    print(
        "BLOCKED by require-resolvable-read-path: " + listed + ": relative read path, "
        "so the harness would prompt user.\n"
        "Re-issue with an absolute path and no `cd`:\n"
        "  grep -rn 'pattern' --include=*.cs /Users/you/proj\n"
        "Do not add a permission allow rule; `Grep`/`Glob` are withheld so symbol questions go to the LSP.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
