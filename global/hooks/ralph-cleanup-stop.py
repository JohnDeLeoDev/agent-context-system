#!/usr/bin/env python3
"ralph-cleanup-stop: Stop hook (replacement for the ralph-loop plugin's stop-hook.sh).\n\nOwns a SEPARATE state file (.claude/ralph-cleanup.local.md) so it does not conflict with\nthe ralph-loop plugin's /ralph-loop state. Both can coexist.\n\nFixes vs. the plugin script:\n  - Matches ANY <promise>...</promise> in the last assistant text block, not just the\n    FIRST one. The plugin's regex was non-greedy from the start, so a prior in-text\n    mention of <promise>X</promise> (e.g. paraphrasing instructions) ate the match and\n    the genuine final <promise>DONE</promise> was never seen.\n  - Honors a `.claude/ralph-cleanup.cancel` sentinel file as a hard kill switch.\n    `touch .claude/ralph-cleanup.cancel` from anywhere terminates the loop on the next\n    Stop event, no matter what the assistant emits.\n  - Honors `active: false` in the state file frontmatter as a soft kill.\n  - Does NOT exit on `stop_hook_active` (self-driving loop): re-feeds continuously;\n    runaway protection is `max_iterations` + the kill switches.\n  - Per-line JSON parsing (no slurp) so one malformed transcript line can't fail the\n    whole extraction."
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

STATE_BASENAME = os.path.join(hp.CLAUDE_DIRNAME, "ralph-cleanup.local.md")
CANCEL_BASENAME = os.path.join(hp.CLAUDE_DIRNAME, "ralph-cleanup.cancel")


def posix_dirname(path):
    d = os.path.dirname(path)
    return d if d else "."


def resolve_state_root(cwd):
    'Walk up from cwd to the nearest directory holding the state file.'
    root = cwd
    while root != "/" and not os.path.isfile(os.path.join(root, STATE_BASENAME)):
        nxt = posix_dirname(root)
        if nxt == root:
            break
        root = nxt
    if not os.path.isfile(os.path.join(root, STATE_BASENAME)):
        root = cwd
    return root


def frontmatter_of(text):
    'Lines between the first two `---` lines only -- a later --- pair does not reopen it.'
    c = 0
    out = []
    for line in text.splitlines():
        if line == "---":
            c += 1
            if c == 2:
                break
            continue
        if c == 1:
            out.append(line)
    return "\n".join(out)


def field(frontmatter, name):
    "First `name: value` line, matching the shell's `grep '^name:' | sed 's/name: *//'`."
    prefix = name + ":"
    for line in frontmatter.splitlines():
        if line.startswith(prefix):
            return re.sub(r'^' + re.escape(prefix) + r' *', '', line)
    return ""


def unquote(value):
    m = re.match(r'^"(.*)"$', value)
    return m.group(1) if m else value


def extract_prompt_text(state_text):
    "Everything after the frontmatter's closing ---; a --- later in the body is\n    swallowed (counted, not printed), matching the awk it replaces."
    i = 0
    out = []
    for line in state_text.splitlines():
        if line == "---":
            i += 1
            continue
        if i >= 2:
            out.append(line)
    return "\n".join(out)


def last_assistant_text_block(transcript_path):
    "The last non-blank text block from the last 200 assistant lines. Bad JSON lines\n    are dropped, not fatal -- one malformed transcript line can't fail the extraction.\n    Returns (found_any_assistant_line, last_text)."
    assistant_lines = []
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"role":"assistant"' in line:
                assistant_lines.append(line)
    if not assistant_lines:
        return False, ""
    last = ""
    for line in assistant_lines[-200:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "") or ""
                if t.strip():
                    last = t
    return True, last


def promise_tags(text):
    for m in re.finditer(r'<promise>(.*?)</promise>', text, re.S):
        t = m.group(1).strip()
        t = re.sub(r'\s+', ' ', t)
        yield t


def main():
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        data = {}

    cwd = data.get("cwd") or os.getcwd()
    root = resolve_state_root(cwd)
    state_file = os.path.join(root, STATE_BASENAME)
    cancel_sentinel = os.path.join(root, CANCEL_BASENAME)

    if not os.path.isfile(state_file):
        return 0

    if os.path.isfile(cancel_sentinel):
        for p in (state_file, cancel_sentinel):
            try:
                os.remove(p)
            except OSError:
                pass
        sys.stderr.write("ralph-cleanup: cancel sentinel detected -- loop terminated.\n")
        return 0

    with open(state_file, encoding="utf-8", errors="replace") as fh:
        state_text = fh.read()
    frontmatter = frontmatter_of(state_text)

    state_session = field(frontmatter, "session_id")
    hook_session = data.get("session_id") or ""
    if state_session and state_session != hook_session:
        return 0

    active = unquote(field(frontmatter, "active"))
    if active == "false":
        try:
            os.remove(state_file)
        except OSError:
            pass
        sys.stderr.write("ralph-cleanup: active=false -- loop terminated.\n")
        return 0

    iteration_s = field(frontmatter, "iteration")
    max_iterations_s = field(frontmatter, "max_iterations")
    completion_promise = unquote(field(frontmatter, "completion_promise"))

    if not re.match(r'^[0-9]+$', iteration_s) or not re.match(r'^[0-9]+$', max_iterations_s):
        sys.stderr.write(
            "ralph-cleanup: state file corrupted (iteration='%s', max='%s') -- "
            "terminating.\n" % (iteration_s, max_iterations_s))
        try:
            os.remove(state_file)
        except OSError:
            pass
        return 0
    iteration = int(iteration_s)
    max_iterations = int(max_iterations_s)

    if max_iterations > 0 and iteration >= max_iterations:
        sys.stderr.write(
            "ralph-cleanup: max iterations (%d) reached.\n" % max_iterations)
        try:
            os.remove(state_file)
        except OSError:
            pass
        return 0

    transcript_value = data.get("transcript_path")
    transcript_path = "null" if transcript_value is None else str(transcript_value)
    if not os.path.isfile(transcript_path):
        sys.stderr.write(
            "ralph-cleanup: transcript not found (%s) -- terminating.\n" % transcript_path)
        try:
            os.remove(state_file)
        except OSError:
            pass
        return 0

    try:
        found_assistant, last_output = last_assistant_text_block(transcript_path)
    except OSError:
        sys.stderr.write(
            "ralph-cleanup: transcript not found (%s) -- terminating.\n" % transcript_path)
        try:
            os.remove(state_file)
        except OSError:
            pass
        return 0
    if not found_assistant:
        sys.stderr.write(
            "ralph-cleanup: no assistant messages in transcript -- terminating.\n")
        try:
            os.remove(state_file)
        except OSError:
            pass
        return 0

    if completion_promise and completion_promise != "null" and last_output:
        if any(tag == completion_promise for tag in promise_tags(last_output)):
            sys.stderr.write(
                "ralph-cleanup: detected <promise>%s</promise> -- loop terminated.\n"
                % completion_promise)
            try:
                os.remove(state_file)
            except OSError:
                pass
            return 0

    next_iteration = iteration + 1

    prompt_text = extract_prompt_text(state_text)
    if not prompt_text.strip():
        sys.stderr.write("ralph-cleanup: empty prompt body -- terminating.\n")
        try:
            os.remove(state_file)
        except OSError:
            pass
        return 0

    
    
    new_state_text = re.sub(r'^iteration: .*$', 'iteration: %d' % next_iteration,
                             state_text, flags=re.M)
    tmp_path = state_file + ".tmp.%d" % os.getpid()
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_state_text)
    os.replace(tmp_path, state_file)

    if completion_promise and completion_promise != "null":
        system_msg = (
            "ralph-cleanup iter %d | Soft stop: emit the %s sentinel in "
            "<promise>...</promise> tags (must be TRUE). Hard stop: `touch %s` or set "
            "`active: false` in %s."
            % (next_iteration, completion_promise, cancel_sentinel, state_file))
    else:
        system_msg = (
            "ralph-cleanup iter %d | Hard stop: `touch %s` or set `active: false` "
            "in %s." % (next_iteration, cancel_sentinel, state_file))

    full_every_s = field(frontmatter, "full_prompt_every")
    full_every = int(full_every_s) if re.match(r'^[0-9]+$', full_every_s) else 0

    state_abs = state_file if state_file.startswith("/") else os.path.join(os.getcwd(), state_file)

    send_full = full_every == 1 or (full_every > 1 and (next_iteration - 1) % full_every == 0)

    if send_full:
        refeed = prompt_text
    else:
        refeed = "Ralph %d, continue, do not restart. %s" % (next_iteration, state_abs)

    print(json.dumps({
        "decision": "block",
        "reason": refeed,
        "systemMessage": system_msg,
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                  
        sys.exit(0)
