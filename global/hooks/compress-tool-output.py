#!/usr/bin/env python3
"PostToolUse(*): shorten a large, repetitive tool result before the model reads it.\n\nWhat it leaves alone. File reads and edits, whose exact text the next edit depends on;\nget_session_context, which a session must read whole (policy); a failed call; a result under\ntool_output_compress.MIN_BYTES; and a result that would not shrink by enough. Then it\nprints nothing and the result stands.\n\nContract. The replacement is hookSpecificOutput.updatedToolOutput in the shape of the\ntool_response it was given, because Claude Code discards a rewrite that does not match the\ntool's output shape (checked on 2.1.292): a string, an object with a `stdout` string\n(Bash; pi and the other harness adapters send this), or a list of content blocks whose\ntext blocks carry `text` (MCP tools). Any other shape is left alone. Each harness adapter\nturns updatedToolOutput into its own replacement call.\n\nCopilot CLI runs this file itself, with no adapter. Its payload carries the result as\ntool_result.text_result_for_llm (toolResult.textResultForLlm under a camelCase event), and\nthe answer it reads is a top-level modifiedResult (GitHub's hooks reference; not seen on a\nlive copilot).\n\nCost. It runs on every tool call. A payload shorter than MIN_BYTES exits before it is\nparsed.\n\nRecord. Each replacement appends one line to <state-dir>/tool-output/savings.jsonl\n(time, session, tool, bytes before and after), which is what answers whether the hook\nearns its place. Session directories older than KEEP_DAYS are removed when a new one is\nmade.\n\nIt never blocks, and on any fault it prints nothing: the original result stands."
import hashlib
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

KEEP_DAYS = 7
SAVINGS_MAX_BYTES = 4 * 1024 * 1024

EXACT_TOOLS = re.compile(r"^(?:read|edit|write|multiedit|notebookedit|notebookread|"
                         r"apply_patch|patch|view|str_replace_editor)$", re.I)
WHOLE_TOOLS = re.compile(r"get_session_context$")


def skipped(tool):
    return bool(EXACT_TOOLS.match(tool) or WHOLE_TOOLS.search(tool))


def output_dir():
    import harness_paths as hp
    return os.path.join(hp.state_dir(), "tool-output")


def sweep(root, now):
    'Remove session directories nothing has written to for KEEP_DAYS, and halve the\n    savings record once it passes SAVINGS_MAX_BYTES.'
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path) \
                    and now - os.path.getmtime(path) > KEEP_DAYS * 86400:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue
    record = os.path.join(root, "savings.jsonl")
    try:
        if os.path.getsize(record) > SAVINGS_MAX_BYTES:
            with open(record, encoding="utf-8") as fh:
                lines = fh.readlines()
            with open(record, "w", encoding="utf-8") as fh:
                fh.writelines(lines[len(lines) // 2:])
    except OSError:
        pass


def save_original(text, session, call_id):
    'The path the original was written to, or None when it could not be saved.'
    root = output_dir()
    ref = re.sub(r"[^\w.-]", "_", session)[:36] or "no-session"
    folder = os.path.join(root, ref)
    name = re.sub(r"[^\w.-]", "_", call_id)[:64] \
        or hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    path = os.path.join(folder, name + ".txt")
    try:
        if not os.path.isdir(folder):
            os.makedirs(folder, mode=0o700, exist_ok=True)
            sweep(root, time.time())
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
        os.chmod(path, 0o600)
    except OSError:
        return None
    return path


def record(session, tool, before, after):
    try:
        with open(os.path.join(output_dir(), "savings.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "session": session[:8], "tool": tool,
                                 "before": before, "after": after}) + "\n")
    except OSError:
        pass


def shorten(text, session, call_id, tool, tally):
    'The replacement for one text, or None to keep it. No file is written for a text\n    compress() declines.'
    import tool_output_compress as toc
    got = toc.compress(text)
    if got is None:
        return None
    path = save_original(text, session, call_id + ("-%d" % len(tally) if tally else ""))
    if path is None:
        return None             
    head = ("%s %d of %d bytes shown; %d lines dropped at the markers below. Full original: "
            "%s . Read that file only when a dropped line matters.]"
            % (toc.MARK, len(got.text.encode("utf-8", "replace")), got.original_bytes,
               got.dropped_lines, path))
    out = head + "\n" + got.text
    tally.append((got.original_bytes, len(out.encode("utf-8", "replace"))))
    return out


def rewrite(response, shorten_text):
    '`response` with its large texts replaced, in the same shape; None when nothing\n    changed or the shape is not one this hook knows.'
    if isinstance(response, str):
        return shorten_text(response)
    if isinstance(response, dict) and isinstance(response.get("stdout"), str):
        new = shorten_text(response["stdout"])
        return None if new is None else dict(response, stdout=new)
    if isinstance(response, list):
        changed, blocks = False, []
        for block in response:
            if isinstance(block, dict) and block.get("type") == "text" \
                    and isinstance(block.get("text"), str):
                new = shorten_text(block["text"])
                if new is not None:
                    block, changed = dict(block, text=new), True
            blocks.append(block)
        return blocks if changed else None
    return None


def main():
    raw = sys.stdin.read()
    import tool_output_compress as toc
    if len(raw) < toc.MIN_BYTES:
        return 0
    data = json.loads(raw)
    tool = str(data.get("tool_name") or data.get("toolName") or "")
    if not tool or skipped(tool):
        return 0
    if str(data.get("hook_event_name") or "PostToolUse").lower() != "posttooluse":
        return 0
    session = str(data.get("session_id") or data.get("sessionId") or "")
    call_id = str(data.get("tool_use_id") or "")
    tally = []

    def shorten_text(text):
        return shorten(text, session, call_id, tool, tally)

    copilot = data.get("tool_result") or data.get("toolResult")
    if "tool_response" not in data and isinstance(copilot, dict):
        kind = copilot.get("result_type") or copilot.get("resultType")
        text = copilot.get("text_result_for_llm") or copilot.get("textResultForLlm")
        new = shorten_text(text) if kind == "success" and isinstance(text, str) else None
        answer = {"modifiedResult": {"resultType": "success", "textResultForLlm": new}}
    else:
        new = rewrite(data.get("tool_response"), shorten_text)
        answer = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                         "updatedToolOutput": new}}
    if new is None:
        return 0
    record(session, tool, sum(b for b, _ in tally), sum(a for _, a in tally))
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        
        sys.exit(0)
