#!/usr/bin/env python3
'tool_output_compress: shorten a large tool result without losing what it says.\n\nThe compress-tool-output hook calls compress() on the text of a tool result before the\nmodel reads it. The transforms are line-based and need no parser, so they hold for any\nshell output:\n\n  1. A directory prefix shared by most lines is stated once and cut from each line.\n  2. A line repeated in a row is printed once with its count.\n  3. A line identical to one already shown twice is dropped. A log line counts as\n     identical when only its leading timestamp differs.\n  4. A run of lines that differ only in their numbers keeps its first three and last two.\n\nEvery dropped line is counted in a bracketed marker at the place it stood, so the result\nnever reads as complete when it is not. A line that names an error, a warning or a\nfailure is never dropped by rule 4: its numbers are the content.\n\ncompress() is pure: no file, no clock, no environment. It returns None when the text is\nshort, already compressed, or would not shrink by enough to pay for the marker. The caller\nkeeps the original and tells the agent where it is.'
import os
import re
from dataclasses import dataclass

MIN_BYTES = 2048          
MIN_SAVED_BYTES = 1024    
MIN_SAVED_SHARE = 0.30
MIN_PREFIX = 20           
PREFIX_SHARE = 0.60       
SEEN_KEEP = 2             
MIN_DEDUPE_LEN = 24       
RUN_MIN = 8               
RUN_HEAD, RUN_TAIL = 3, 2

MARK = "[compressed:"     

ALREADY_CUT = ("<persisted-output>", "...output truncated...", "[MCP text output truncated",
               "Warning: truncated output (original token count")

_STAMP = re.compile(
    r"^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*"
    r"|^\[?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\]?\s+")
_NUMBER = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-f]{8,}\b|\d+")
_PROBLEM = re.compile(r"error|warn|fail|fatal|panic|exception|traceback|denied|refused|"
                      r"cannot|not found|undefined|unresolved|mismatch|conflict", re.I)
_LOCATED = re.compile(r"^[^\s:]+:\d+[:-]")   


@dataclass
class Compressed:
    text: str             
    original_bytes: int
    dropped_lines: int
    prefix: str           


def _size(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


def shared_prefix(lines: list[str]) -> str:
    "The directory, ending in a slash, that most lines start with; else ''."
    solid = [ln for ln in lines if ln.strip()]
    rooted = [ln for ln in solid if ln.startswith("/")]
    if len(rooted) < 4 or len(rooted) < PREFIX_SHARE * len(solid):
        return ""
    prefix = os.path.commonprefix(rooted)
    prefix = prefix[:prefix.rfind("/") + 1]
    return prefix if len(prefix) >= MIN_PREFIX else ""


def _form(line: str) -> str:
    'The line with its numbers masked: two lines of one form differ only in numbers.'
    return _NUMBER.sub("#", line)


def _mark(count: int, what: str) -> str:
    return f"{MARK} {count} {what}]"


def _collapse_repeats(lines: list[str]) -> tuple[list[str], int]:
    'Rule 2: a line repeated in a row, printed once with its count.'
    out, dropped, at = [], 0, 0
    while at < len(lines):
        end = at
        while end + 1 < len(lines) and lines[end + 1] == lines[at]:
            end += 1
        extra = end - at
        out.append(lines[at])
        if extra >= 2 and lines[at].strip():
            out.append(_mark(extra, "more lines identical to the line above"))
            dropped += extra
        else:
            out.extend(lines[at + 1:end + 1])
        at = end + 1
    return out, dropped


def _drop_seen(lines: list[str]) -> tuple[list[str], int]:
    'Rule 3: a line already shown SEEN_KEEP times is dropped, with one marker per gap.'
    out, dropped, shown, gap = [], 0, {}, 0
    for line in lines:
        key = _STAMP.sub("", line)
        stamped = key != line
        if len(key) < MIN_DEDUPE_LEN or line.startswith(MARK):
            key = None
        if key is not None and shown.get(key, 0) >= SEEN_KEEP:
            gap += 1
            continue
        if gap:
            out.append(_mark(gap, "lines identical to lines shown earlier"
                             + (", apart from their timestamps" if stamped else "")))
            dropped, gap = dropped + gap, 0
        if key is not None:
            shown[key] = shown.get(key, 0) + 1
        out.append(line)
    if gap:
        out.append(_mark(gap, "lines identical to lines shown earlier"))
        dropped += gap
    return out, dropped


def _thin_runs(lines: list[str]) -> tuple[list[str], int]:
    'Rule 4: a long run of lines of one form keeps its head and tail.'
    out, dropped, at = [], 0, 0
    while at < len(lines):
        line = lines[at]
        if (len(line) < MIN_DEDUPE_LEN or line.startswith(MARK) or _PROBLEM.search(line)
                or _LOCATED.match(line)):
            out.append(line)
            at += 1
            continue
        form, end = _form(line), at
        while (end + 1 < len(lines) and _form(lines[end + 1]) == form
               and not _PROBLEM.search(lines[end + 1])):
            end += 1
        length = end - at + 1
        if length >= RUN_MIN:
            cut = length - RUN_HEAD - RUN_TAIL
            out.extend(lines[at:at + RUN_HEAD])
            out.append(_mark(cut, "lines of the same form, differing only in numbers"))
            out.extend(lines[end - RUN_TAIL + 1:end + 1])
            dropped += cut
        else:
            out.extend(lines[at:end + 1])
        at = end + 1
    return out, dropped


def compress(text: str, min_bytes: int = MIN_BYTES) -> Compressed | None:
    'The shortened text, or None when `text` is short, already cut, or would not shrink\n    by MIN_SAVED_BYTES and MIN_SAVED_SHARE.'
    if not isinstance(text, str) or len(text) < min_bytes:
        return None
    if MARK in text or any(stub in text[:400] or stub in text[-400:] for stub in ALREADY_CUT):
        return None
    original = _size(text)
    if original < min_bytes:
        return None
    lines = text.split("\n")
    prefix = shared_prefix(lines)
    if prefix:
        lines = [ln[len(prefix):] if ln.startswith(prefix) else ln for ln in lines]
    lines, repeats = _collapse_repeats(lines)
    lines, seen = _drop_seen(lines)
    lines, runs = _thin_runs(lines)
    body = "\n".join(lines)
    if prefix:
        body = f"{MARK} paths below are relative to {prefix}]\n{body}"
    saved = original - _size(body)
    if saved < MIN_SAVED_BYTES or saved < MIN_SAVED_SHARE * original:
        return None
    return Compressed(text=body, original_bytes=original,
                      dropped_lines=repeats + seen + runs, prefix=prefix)
