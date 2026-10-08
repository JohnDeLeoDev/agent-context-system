#!/usr/bin/env python3
"require-investigation-before-edit: PreToolUse(Edit|MultiEdit) evidence gate.\n\nDeny the first edit to a source file when the session transcript holds no earlier\nsearch naming that file. The denial lists the facts to gather. A retry passes, so the\ngate costs one round trip per file, and a file that was searched first costs nothing.\n\nEvidence is a Grep, Glob, LSP or `mcp__*-lsp__*` call, or a Bash `grep`/`rg`/`find`,\nwhose input names the file's basename or stem. A Read is not evidence: Edit already\nrequires one.\n\nA subagent (payload carries `agent_id`) skips the evidence check, because its calls may\nnot appear in the parent transcript. It gets a per-(agent_id, file) deny-once ledger.\n\nEvery failure path allows: malformed payload, unreadable transcript, unwritable ledger."
import json
import os
import re
import sys

GATED = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".swift", ".kt", ".kts", ".java",
         ".cs", ".go", ".rs", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".m",
         ".mm", ".sh"}
FULL_DENIALS = 3
BASH_SEARCH = re.compile(r"(^|[\s;&|(])(grep|egrep|fgrep|rg|find)(\s|$)")
TEST_NAME = re.compile(r"(^test[_-]|[_-]test\.|\.test\.|\.spec\.)")
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}

FULL_TEXT = (
    "Before editing %(name)s, gather these facts, then retry the edit:\n"
    "  - importers or references: who imports or calls anything in this file "
    "(Grep the module name, or LSP references)\n"
    "  - affected public symbols: which exported functions, classes or constants "
    "this edit changes\n"
    "  - data schema: if the file reads or writes data, read real records from more than one session and "
    "check its shape\n"
    "  - the current instruction: quote the user's request this edit serves\n"
    "%(instruction)s"
    "The retry is allowed. A search naming %(name)s first skips this check "
    "(denial %(n)d).\n"
    "%(batch)s"
)


BATCH_TEXT = ("Only this edit was refused. Other edits to %s sent in the same message may "
              "already be applied: re-read the file before retrying this one.")


def allow():
    sys.exit(0)


def deny(text):
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": "deny",
                                      "permissionDecisionReason": text}}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)


def exempt(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in GATED:
        return True
    parts = path.replace("\\", "/").split("/")
    if TEST_DIRS & set(parts[:-1]) or TEST_NAME.search(parts[-1]):
        return True
    home = os.path.expanduser("~")
    if (path.startswith(("/tmp/", "/private/tmp/", "/var/folders/"))
            or path.startswith(os.path.join(home, ".cache") + "/")
            or "/.agents/tmp/" in path):
        return True
    cur = os.path.dirname(path)
    while cur and cur != os.path.dirname(cur):
        if os.path.exists(os.path.join(cur, ".git")):
            return False
        cur = os.path.dirname(cur)
    return True                                     


def records(transcript):
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def message_blocks(rec):
    msg = rec.get("message") if isinstance(rec, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def is_search(block):
    name = str(block.get("name", ""))
    low = name.lower()
    if low in ("grep", "glob") or "lsp" in low:
        return True
    if name == "Bash":
        cmd = (block.get("input") or {}).get("command", "")
        return bool(BASH_SEARCH.search(str(cmd)))
    return False


def scan(transcript, needles):
    '(has_evidence, last user instruction) from one pass over the transcript.'
    evidence, instruction = False, ""
    for rec in records(transcript):
        blocks = message_blocks(rec)
        if rec.get("type") == "user":
            text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if text.strip():
                instruction = text.strip()
        elif rec.get("type") == "assistant" and not evidence:
            for b in blocks:
                if b.get("type") == "tool_use" and is_search(b):
                    if any(n in json.dumps(b.get("input", {})) for n in needles):
                        evidence = True
                        break
    return evidence, instruction


def load_ledger(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"denials": 0, "files": []}


def save_ledger(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp%d" % os.getpid()
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    if not isinstance(payload, dict) or payload.get("tool_name") not in ("Edit", "MultiEdit"):
        allow()
    tool_input = payload.get("tool_input")
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    session = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not path or not session or not isinstance(path, str):
        allow()
    path = os.path.abspath(path)
    if exempt(path):
        allow()

    agent = payload.get("agent_id") or ""
    key = "%s|%s" % (agent, path)
    ledger_path = os.path.join(os.path.expanduser("~"), ".local", "state", "agent-context",
                               "edit-gate", re.sub(r"[^\w.-]", "_", str(session)) + ".json")
    ledger = load_ledger(ledger_path)
    if key in ledger["files"]:
        allow()

    instruction = ""
    if not agent:
        if not transcript or not os.path.isfile(transcript):
            allow()
        base = os.path.basename(path)
        try:
            found, instruction = scan(transcript, {base, os.path.splitext(base)[0]})
        except Exception:
            allow()
        if found:
            allow()

    ledger["denials"] = int(ledger.get("denials", 0)) + 1
    ledger["files"].append(key)
    n = ledger["denials"]
    if not save_ledger(ledger_path, ledger):
        allow()                                     
    name = os.path.basename(path)
    if n > FULL_DENIALS:
        deny("Denial %d: search for callers and references of %s before editing; "
             "retry is allowed. %s" % (n, name, BATCH_TEXT % name))
    quoted = ('  Current instruction: "%s"\n' % instruction[:200].replace("\n", " ")
              if instruction else "")
    deny(FULL_TEXT % {"name": name, "instruction": quoted, "n": n, "batch": BATCH_TEXT % name})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
