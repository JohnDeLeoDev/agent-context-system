#!/usr/bin/env python3
"block-destructive-data-command: PreToolUse(Bash) refuses two destructive shapes.\n\n  1. `dd` with `of=` a block device (/dev/sd*, nvme*, mmcblk*, disk*, loop*, ...).\n  2. `drop table|database|schema`, `truncate`, or `delete from` with no WHERE, issued\n     through psql, sqlite3, mysql or mariadb, inline (-c, -e, an argument) or by heredoc.\n\nblock-blind-recursive-delete covers `rm -rf`, `find -delete` and `find -exec rm`.\nThis covers the rest of the destructive set. A command that only NAMES these words\n(grep, echo, a commit message, a heredoc feeding cat) passes: a command word counts\nonly where a shell would run it, and SQL is read only from a database client's own\ncommand. SQL in a file (`-f x.sql`, `< x.sql`) is not inspected.\n\nFails open on any error."
import json
import os
import re
import shlex
import sys

FAST = re.compile(r"(?<![\w.-])(dd|psql|sqlite3|mysql|mariadb)(?![\w-])")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
BLOCKDEV = re.compile(r"^/dev/(sd|hd|vd|xvd|nvme|mmcblk|disk|rdisk|loop|md|dm-|mapper/|sr|nbd)")
WRAPPERS = {"sudo", "env", "time", "nohup", "command", "exec", "nice", "doas", "xargs"}
VALUE_FLAGS = ("-u", "-g", "-I", "-n", "-P", "-L", "-d", "-E", "-s")
CLIENTS = {"psql", "sqlite3", "mysql", "mariadb"}
DROP = re.compile(r"\bdrop\s+(table|database|schema)\b", re.I)
TRUNCATE = re.compile(r"\btruncate\s+(table\s+)?\w", re.I)
DELETE = re.compile(r"\bdelete\s+from\b", re.I)
WHERE = re.compile(r"\bwhere\b", re.I)


def segments(cmd):
    'Shell command segments, quotes kept intact. A heredoc body stays with the\n    segment that opened it.'
    out, cur, quote, i, n, pending = [], [], None, 0, len(cmd), []

    def flush():
        text = "".join(cur).strip()
        if text:
            out.append(text)
        del cur[:]

    while i < n:
        c = cmd[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                cur.append(cmd[i + 1])
                i += 1
            elif c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            cur.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            cur.append(c + cmd[i + 1])
            i += 2
            continue
        m = HEREDOC.match(cmd, i)
        if m:
            pending.append(m.group(2))
            cur.append(m.group(0))
            i = m.end()
            continue
        if c == "\n" and pending:
            j = i + 1
            for delim in pending:
                while j < n:
                    end = cmd.find("\n", j)
                    line = cmd[j:] if end < 0 else cmd[j:end]
                    j = n if end < 0 else end + 1
                    if line.strip() == delim:
                        break
            cur.append(cmd[i:j])
            del pending[:]
            flush()
            i = j
            continue
        if c in ";&|\n()":
            piped = c == "|" and not (i + 1 < n and cmd[i + 1] == "|")
            flush()
            if piped and out:
                out[-1] = out[-1] + "\x00|"            
            i += 1
            continue
        cur.append(c)
        i += 1
    flush()
    return out


def command_word(words):
    '(index, basename) of the program a segment runs, after env assignments and\n    wrappers such as sudo. (None, "") when there is none.'
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_]\w*=", w):
            i += 1
        elif w in WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in VALUE_FLAGS else 1
        else:
            return i, os.path.basename(w)
    return None, ""


def bad_sql(text):
    if DROP.search(text) or TRUNCATE.search(text):
        return True
    for stmt in text.split(";"):
        if DELETE.search(stmt) and not WHERE.search(stmt):
            return True
    return False


def verdict(cmd):
    prev = ""
    for raw_seg in segments(cmd):
        piped_out = raw_seg.endswith("\x00|")
        seg = raw_seg[:-2] if piped_out else raw_seg
        fed = prev                                      
        prev = seg if piped_out else ""
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        idx, prog = command_word(words)
        if idx is None:
            continue
        if prog == "dd":
            for w in words[idx + 1:]:
                if w.startswith("of=") and (BLOCKDEV.match(w[3:]) or w[3:] == "{}"):
                    return ("dd writes to the block device %s. Every byte on it is overwritten."
                            % w[3:])
        elif prog in CLIENTS and (bad_sql(seg) or bad_sql(fed)):
            return ("%s is asked to drop a table, database or schema, truncate, or delete "
                    "with no WHERE clause. That removes data with no undo." % prog)
    return None


def main():
    raw = sys.stdin.read()
    if not FAST.search(raw):
        return 0
    try:
        data = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return 0
    args = data.get("tool_input")
    cmd = args.get("command") if isinstance(args, dict) else None
    if not isinstance(cmd, str) or not cmd:
        return 0
    why = verdict(cmd)
    if not why:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "BLOCKED by block-destructive-data-command: " + why + "\n\n"
            "Nothing was run. Show user the exact command and target and let him confirm. "
            "For SQL, add a WHERE clause that names the rows, or run a SELECT first and "
            "report what it returned.")}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
