#!/usr/bin/env python3

'PreToolUse: keep written prose plain and American.\n\nuser wants terse, simple, easy-to-read language and American spelling in\nnarratives, descriptors and naming. Rules and word lists:\nget_doc("plain-language.md").\n\nWhat it scans: prose only. Code comments, markdown, commit messages, store doc\nand memory bodies. It never scans identifiers, so a descriptive function name is\nsafe: cutting length in a name makes code harder to read.\n\nInline code spans and fenced blocks are stripped before scanning, so quoting a\nreal British-spelled API or a banned word as an example is always allowed.\n\nHard list and British spelling in new text -> deny.\nLong sentences and hits already in the file -> warn, never block.\nBlocking on a pre-existing hit elsewhere in a legacy file would deadlock edits\nto that file, so those only ever warn.'
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

GIT_COMMIT_RE = re.compile(r"(^|[;&|\s])git\s+(-\S+\s+)*commit\b", re.MULTILINE)






SHELL_WRITE_RE = re.compile(r"(>>?\s*[^&|\s]|<<-?\s*[A-Za-z_'\"]|(^|[;&|\s])tee\b)",
                             re.MULTILINE)

MACHINE_GENERATED_GLOBS = (
    "*/.git/*", "*.lock", "*package-lock.json", "*.min.js", "*.svg",
    "*/node_modules/*", "*/Pods/*", "*/vendor/*",
)
PROSE_EXT_GLOBS = ("*.md", "*.markdown", "*.mdx", "*.txt", "*.rst")

CODE_KINDS = ("hook", "script")

LEADING_COMMENT_RE = re.compile(
    r"^[ \t]*(//+|#+|/\*+|\*|<!--|--|;;|%)[ \t]*(.*)$"
)
TRAILING_SLASHSLASH_RE = re.compile(r"^.*[ \t]//[ \t](.*)$")
TRAILING_HASH_RE = re.compile(r"^.*[ \t]#[ \t](.*)$")
FENCE_RE = re.compile(r"^\s*```")
CODE_SPAN_RE = re.compile(r"`[^`]*`")


WIKILINK_RE = re.compile(r"\[\[([^\]|]*)(\|([^\]]*))?\]\]")


def _get(tool_input, *names):
    for name in names:
        val = tool_input.get(name)
        if val is not None:
            return val
    return None


def _join_edits(tool_input, *keys):
    edits = tool_input.get("edits")
    out = []
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            for key in keys:
                val = edit.get(key)
                if val is not None:
                    out.append(val)
    return out


def _join_nonnull(*values):
    parts = [v for v in values if v is not None]
    return "\n".join(str(p) for p in parts)


def determine_fields(tool, tool_input):
    '(file_path, new_text, all_prose) for this call, or (None, None, None) to skip.'
    if tool in ("Write", "write"):
        fp = _get(tool_input, "file_path", "filePath", "path") or ""
        new = _get(tool_input, "content", "file_text", "text") or ""
        return fp, new, 0
    if tool in ("Edit", "edit", "MultiEdit", "multiedit", "patch"):
        fp = _get(tool_input, "file_path", "filePath", "path") or ""
        parts = [tool_input.get("new_string"), tool_input.get("newString"),
                  tool_input.get("new_str")]
        parts += _join_edits(tool_input, "new_string", "newString", "new_str", "newText")
        new = _join_nonnull(*parts)
        return fp, new, 0
    if tool == "NotebookEdit":
        fp = tool_input.get("notebook_path") or ""
        new = tool_input.get("new_source") or ""
        return fp, new, 0
    if tool in ("Bash", "bash", "shell"):
        cmd = tool_input.get("command") or ""
        if GIT_COMMIT_RE.search(cmd):
            return "<commit message>", cmd, 1
        if SHELL_WRITE_RE.search(cmd):
            return "<shell write>", cmd, 1
        return None, None, None
    if tool.endswith("upsert_doc"):
        fp = tool_input.get("path") or ""
        new = _join_nonnull(tool_input.get("body"), tool_input.get("title"))
        return fp, new, 1
    if tool.endswith("upsert_memory"):
        fp = tool_input.get("slug") or ""
        new = _join_nonnull(tool_input.get("body"), tool_input.get("description"))
        return fp, new, 1
    if tool.endswith("edit_body"):
        fp = tool_input.get("key") or ""
        new = tool_input.get("new_string") or ""
        return fp, new, 0 if tool_input.get("kind") in CODE_KINDS else 1
    
    
    
    if tool.endswith("upsert_skill") or tool.endswith("upsert_command") \
            or tool.endswith("upsert_agent_definition"):
        fp = tool_input.get("name") or ""
        new = _join_nonnull(tool_input.get("body"), tool_input.get("description"))
        return fp, new, 1
    if tool.endswith("upsert_instruction"):
        fp = tool_input.get("title") or ""
        new = _join_nonnull(tool_input.get("body"), tool_input.get("title"))
        return fp, new, 1
    
    
    
    
    
    if tool.endswith("upsert_hook") or tool.endswith("upsert_script"):
        fp = tool_input.get("name") or ""
        parts = []
        desc = tool_input.get("description")
        if desc is not None:
            parts.append("# " + str(desc))
        if tool_input.get("script_body") is not None:
            parts.append(tool_input.get("script_body"))
        new = "\n".join(parts)
        return fp, new, 0
    
    
    if tool.endswith("bulk_edit"):
        new = _join_nonnull(*_join_edits_replacements(tool_input))
        return "<bulk_edit>", new, 1
    
    
    if tool.endswith("add_audit_observation") or tool.endswith("update_audit_observation"):
        new = _join_nonnull(tool_input.get("observation"), tool_input.get("evidence"),
                             tool_input.get("note"))
        return "<audit observation>", new, 1
    
    
    
    
    
    if tool.endswith("resolve_audit_observation"):
        new = tool_input.get("resolution_note") or ""
        return "<audit observation>", new, 1
    return None, None, None


def _join_edits_replacements(tool_input):
    edits = tool_input.get("edits")
    out = []
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            replacements = edit.get("replacements")
            if not isinstance(replacements, list):
                continue
            code = edit.get("kind") in CODE_KINDS
            for repl in replacements:
                if isinstance(repl, list) and len(repl) > 1:
                    out.append(prose_extract(str(repl[1]), 0) if code else repl[1])
    return out


def prose_extract(text, all_prose):
    if all_prose:
        return text
    out = []
    for line in text.split("\n"):
        buf = line
        m = LEADING_COMMENT_RE.match(buf)
        if m:
            buf = m.group(2)
            out.append(buf)
        m2 = TRAILING_SLASHSLASH_RE.match(buf)
        if m2:
            buf = m2.group(1)
            out.append(buf)
        m3 = TRAILING_HASH_RE.match(buf)
        if m3:
            buf = m3.group(1)
            out.append(buf)
    return "\n".join(out)


def strip_wikilink_targets(text):
    'Drop the `[[target]]` identifier, keep an optional `|label` for scanning.'
    return WIKILINK_RE.sub(lambda m: m.group(3) or "", text)


def strip_code(text):
    out_lines = []
    fenced = False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out_lines.append(line)
    joined = strip_wikilink_targets("\n".join(out_lines))
    return CODE_SPAN_RE.sub("", joined)


def matches_any(path, globs):
    return any(fnmatch.fnmatch(path, g) for g in globs)


def hits(text, pattern, limit=6):
    'Case-insensitive `N:match` lines, deduplicated by match content, first `limit`.'
    if not pattern:
        return ""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return ""
    results = []
    for i, line in enumerate(text.split("\n"), start=1):
        for m in rx.finditer(line):
            if m.group(0) == "":
                continue
            results.append("%d:%s" % (i, m.group(0)))

    def key(s):
        idx = s.find(":")
        return s[idx + 1:]

    seen = set()
    uniq = []
    for s in sorted(results, key=key):
        k = key(s)
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return "\n".join(uniq[:limit])


def hits_ordered(text, pattern, limit=3):
    'Case-insensitive `N:match` lines, in original order, no dedup, first `limit`.'
    if not pattern:
        return ""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return ""
    results = []
    for i, line in enumerate(text.split("\n"), start=1):
        for m in rx.finditer(line):
            if m.group(0) == "":
                continue
            results.append("%d:%s" % (i, m.group(0)))
            if len(results) >= limit:
                return "\n".join(results)
    return "\n".join(results)


def load_words():
    'Shell-variable assignments from plain-language-words.py --sh, parsed.'
    home = os.environ.get("HOME") or os.path.expanduser("~")
    words_py = os.environ.get("PLAIN_LANGUAGE_WORDS") \
        or os.path.join(hp.scripts_dir(home), "plain-language-words.py")
    if not os.path.isfile(words_py):
        words_py = os.path.join(home, ".agent-context", "global", "scripts",
                                 "plain-language-words.py")
    try:
        proc = subprocess.run([sys.executable, words_py, "--sh"], capture_output=True,
                               text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    if not out.strip():
        return None
    words = {}
    for line in out.splitlines():
        name, sep, rest = line.partition("=")
        if not sep:
            continue
        try:
            parts = shlex.split(rest)
        except ValueError:
            parts = []
        words[name] = parts[0] if parts else ""
    return words


def join_ignore_empty(*parts):
    return "\n".join(p for p in parts if p)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  

    tool = data.get("tool_name") or ""
    if not tool:
        return 0

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    fp, new, all_prose = determine_fields(tool, tool_input)
    if new is None:
        return 0
    if not new:
        return 0

    fp = fp or ""

    
    if "plain-language" in fp or "plain_language" in fp:
        return 0
    if "PLAIN-LANGUAGE-EXEMPT" in new:
        return 0

    
    if matches_any(fp, MACHINE_GENERATED_GLOBS):
        return 0

    if matches_any(fp, PROSE_EXT_GLOBS):
        all_prose = 1

    
    text = strip_code(prose_extract(new, all_prose))
    if not text:
        return 0

    
    words = load_words()
    if not words:
        return 0

    hard = words.get("HARD", "")
    hard_phrase = words.get("HARD_PHRASE", "")
    em_dash = words.get("EM_DASH", "")
    brit_ise = words.get("BRIT_ISE", "")
    brit_analyse = words.get("BRIT_ANALYSE", "")
    brit_our = words.get("BRIT_OUR", "")
    brit_re = words.get("BRIT_RE", "")
    brit_misc = words.get("BRIT_MISC", "")

    h1 = hits(text, hard)
    h2 = hits_ordered(text, hard_phrase, 3)
    b1 = hits(text, brit_ise)
    b2 = hits(text, brit_analyse)
    b3 = hits(text, brit_our)
    b4 = hits(text, brit_re)
    b5 = hits(text, brit_misc)
    jargon = join_ignore_empty(h1, h2)
    british = join_ignore_empty(b1, b2, b3, b4, b5)

    dash = ""
    if em_dash:
        dash_lines = []
        for i, line in enumerate(text.split("\n"), start=1):
            if em_dash in line:
                dash_lines.append("%d:%s" % (i, line))
                if len(dash_lines) >= 6:
                    break
        dash = "\n".join(dash_lines)

    if jargon or british or dash:
        reason = "BLOCKED by plain-language-check on `%s`." % (fp or "<text>")
        if jargon:
            reason += "\n\nJargon (hard list):\n" + jargon
        if british:
            reason += "\n\nBritish spelling, use American:\n" + british
        if dash:
            reason += "\n\nEm dash, use a period, comma or colon:\n" + dash
        reason += (
            "\n\nRewrite the prose and retry. Prefer the plain verb: use, show, handle.\n"
            "Identifiers are not scanned; do not shorten a name to pass this.\n"
            "A correct word (a British-spelled API, a quoted error) goes in backticks.\n"
            "get_doc(\"plain-language.md\")"
        )
        sys.stderr.write(reason + "\n")
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
        return 2

    
    warn = ""

    long_count = sum(1 for part in re.split(r"[.!?]", text) if len(part.split()) > 30)
    if long_count > 0:
        piece = "%d sentence(s) over 30 words - split them" % long_count
        warn = (warn + " | " + piece) if warn else piece

    
    if tool not in ("Bash", "bash") and os.path.isfile(fp):
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                old_content = fh.read()
        except OSError:
            old_content = None
        if old_content is not None:
            old_text = strip_code(prose_extract(old_content, all_prose))
            combined = "|".join(p for p in (hard, brit_ise, brit_our, brit_misc) if p)
            old_count = 0
            if combined:
                try:
                    rx = re.compile(combined, re.IGNORECASE)
                    old_count = sum(1 for line in old_text.split("\n") if rx.search(line))
                except re.error:
                    old_count = 0
            if old_count > 0:
                piece = ("%d pre-existing plain-language hit(s) elsewhere in this "
                          "file - clean them up while you are here" % old_count)
                warn = (warn + " | " + piece) if warn else piece

    if warn:
        json.dump({"systemMessage": "plain-language: " + warn}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("plain-language-check crashed and failed open: %r\n" % (exc,))
        sys.exit(0)
