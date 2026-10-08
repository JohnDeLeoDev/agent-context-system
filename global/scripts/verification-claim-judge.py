#!/usr/bin/env python3
'Did this turn assert a verification it never ran?\n\nThe judgment lives here, once. Two hooks need the same answer through two\ndifferent delivery paths:\n\n  verification-claim-check  UserPromptSubmit, advisory. Looks backward at the\n                            finished turn and injects additionalContext, which\n                            demonstrably reaches the model. Corrects the next\n                            turn.\n  verification-claim-gate   Stop, blocking. Emits {"decision": "block"}, the one\n                            Stop channel that does reach the model, so the turn\n                            does not end and the claim can be corrected in the\n                            same turn.\n\nThe advisory path alone cannot be measured by eval-run, which drives\n`claude -p` with one turn per case: there is no next turn for a correction to\nland in. That is why the gate exists. Neither replaces the\nother: the gate catches it now, the advisory catches what the gate stands down\non.\n\nWhy this defect and not another. The most consistent defect eval-run finds is\ncode that is correct with a final message that claims a verification that never\nhappened. It is a reporting failure, which is why a hook can catch it.\n\nConservative, and more so for the gate than the advisory: a\nfalse positive on the advisory path spends a sentence of context, while a false\npositive on the gate refuses to let a correct turn end. So the judgment fires\nonly when all of these hold:\n\n  * the message makes an assertive claim (not "if the tests pass", not "run\n    the tests next"),\n  * no test/build/lint command ran anywhere in that turn,\n  * no subagent was spawned -- a worker may have run them where this cannot see,\n  * the turn actually used tools; a pure conversation turn asserts nothing about\n    a repository.\n\nSecond judgment: an edit left unchecked and unreported.\n`edited_without_running` fires when a turn edited source, ran no check, did not\nrun the edited file, spawned no subagent, and the closing message does not say\nthe change is unverified. It is the other half of the same defect: the report\ndoes not overclaim, it says nothing about verification. Every work session\nshould end in a tested outcome (test-first-delivery skill). Only\nverification-claim-gate uses it. As advice on every later turn it would repeat\non most edit turns, so the advisory hook keeps the claim judgment alone.\n\nObservations guarded: #338, #467.'

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import transcript_records

__all__ = [
    "CLAIM", "HEDGE", "RUNNER", "SPAWNS", "EDIT_TOOLS", "SOURCE_EXT", "UNVERIFIED",
    "previous_turn", "scan", "claimed_without_running", "judge_transcript",
    "edited_paths", "is_source", "edited_without_running", "judge_unverified_edit",
]



CLAIM = re.compile(
    r"\b("
    r"(?:all\s+)?tests?\s+(?:now\s+)?(?:pass(?:es|ed|ing)?|are\s+green|succeed(?:ed)?)"
    r"|(?:the\s+)?(?:test\s+)?suite\s+(?:is\s+green|passes|passed)"
    r"|build\s+(?:succeed(?:s|ed)|is\s+green|passes|passed)"
    r"|(?:it\s+)?compiles\s+(?:cleanly|fine|now)"
    r"|lint\s+(?:is\s+)?clean"
    r"|verified\s+(?:that\s+)?(?:it|this|the)"
    r"|I\s+(?:ran|have\s+run)\s+the\s+tests?"
    r")\b", re.I)


HEDGE = re.compile(r"\b(if|once|when|should|would|will|to\s+confirm|please\s+run|"
                   r"you\s+can\s+run|not\s+run|did\s+not\s+run|have\s+not\s+run|"
                   r"could\s+not\s+run|unable\s+to\s+run)\b", re.I)


RUNNER = re.compile(
    r"\b(pytest|py\.test|unittest|tox|nox"
    r"|npm\s+(?:test|run\s+(?:test|build|lint|typecheck))|yarn\s+(?:test|build)"
    r"|pnpm\s+(?:test|build)|jest|vitest|mocha|playwright"
    r"|go\s+(?:test|build)|cargo\s+(?:test|build|check|clippy)"
    r"|swift\s+(?:test|build)|xcodebuild|gradle|gradlew|mvn"
    r"|dotnet\s+(?:test|build)|make\b|cmake|ctest"
    r"|tsc\b|eslint|ruff|mypy|pyright|flake8|shellcheck|bash\s+-n"
    r"|python3?\s+-m\s+(?:pytest|unittest|py_compile|compileall)"
    r"|bun\s+(?:run\s+)?(?:test|build|lint)|deno\s+(?:test|check|lint)|just\s+(?:test|build|check|lint)"
    r"|rspec|phpunit|mix\s+test|dart\s+test|flutter\s+test|rake\s+test"
    r"|bazel\s+(?:test|build)|swiftlint|ktlint|detekt|biome\s+(?:check|lint)"
    
    
    r"|python[\d.]*\s+(?:-\S+\s+)*\S*?(?:^|/|\b)[\w.-]*(?:test|check)[\w.-]*\.py\b"
    r")", re.I)



VAR_RUNNER = re.compile(
    r"\b(\w+)=\"?\S*python[\d.]*\"?(?=[\s;&]).*?\$\{?\1\}?\s+(?:-\S+\s+)*\S*?[\w.-]*"
    r"(?:test|check)[\w.-]*\.py\b", re.S | re.I)


def ran_something(commands):
    'True when any command ran a test, build, lint or check.'
    return any(RUNNER.search(c) or VAR_RUNNER.search(c) for c in commands)

SPAWNS = {"Task", "Agent", "Workflow"}

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}



SOURCE_EXT = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".swift", ".kt", ".kts", ".java", ".scala", ".cs", ".go", ".rs", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm", ".dart",
    ".sh", ".bash", ".zsh", ".fish", ".sql",
}
AGENT_DIRS = ("/" + hp.CLAUDE_DIRNAME + "/", "/" + hp.AGENTS_DIRNAME + "/",
              "/" + hp.STORE_DIRNAME + "/")


_VERB = r"(?:run|verify|verified|test|tested|check|checked)"
UNVERIFIED = re.compile(
    r"\b(?:not\s+(?:yet\s+)?(?:been\s+)?" + _VERB +
    r"|unverified|untested|without\s+(?:running|testing|verifying)"
    r"|\w+n[o']t\s+(?:yet\s+)?(?:been\s+)?" + _VERB +
    r"|cannot\s+" + _VERB +
    r"|(?:unable|no\s+way)\s+to\s+" + _VERB +
    r"|skip(?:ped|ping)?\s+(?:the\s+)?(?:tests?|verification|checks?)"
    r"|no\s+tests?\s+(?:exist|ran|were\s+run|to\s+run|cover))", re.I)




_is_prompt = transcript_records.is_human_prompt


def previous_turn(path):
    'The records of the turn that just FINISHED.\n\n    Whether the prompt being submitted is already in the transcript when this\n    fires is not something to assume -- the answer differs by harness version,\n    and guessing wrong silently yields an empty slice, which reads as "nothing\n    to report" and would make both hooks permanently quiet. So: find the last\n    prompt that actually has assistant output after it, and take everything\n    from there.'
    records = transcript_records.recent(path)
    for idx in range(len(records) - 1, -1, -1):
        if not _is_prompt(records[idx]):
            continue
        tail = records[idx + 1:]
        if any(r.get("type") == "assistant" for r in tail):
            return list(tail)
    return []


def last_reply(records):
    "The records after the turn's last user record of any kind (a prompt, a task\n    notification, hook feedback), which hold the reply the user just read. A claim is\n    judged there; the evidence for it may sit anywhere in the turn, so\n    previous_turn stays the evidence window. Judging claims over the whole turn re-reported\n    one early claim on every later prompt of a long, notification-driven turn."
    for idx in range(len(records) - 1, -1, -1):
        rec = records[idx]
        if rec.get("type") != "user":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                             for b in content):
            continue
        return records[idx + 1:]
    return records


def scan(records):
    '(assistant text, commands run, tools used) for the turn.'
    text, commands, tools = [], [], set()
    for rec in records:
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and rec.get("type") == "assistant":
            text.append(content)
            continue
        if not isinstance(content, list):
            continue
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "text" and rec.get("type") == "assistant":
                text.append(blk.get("text") or "")
            elif blk.get("type") == "tool_use":
                tools.add(blk.get("name") or "")
                inp = blk.get("input") or {}
                if isinstance(inp, dict) and isinstance(inp.get("command"), str):
                    commands.append(inp["command"])
    return "\n".join(text), commands, tools


def claimed_without_running(text, commands, tools):
    'The offending sentence, or None when there is nothing to report.'
    if not tools:
        return None                      
    if tools & SPAWNS:
        return None                      
    if ran_something(commands):
        return None                      
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if CLAIM.search(sentence) and not HEDGE.search(sentence):
            return sentence.strip()[:200]
    return None


def judge_transcript(path):
    'Convenience: (sentence or None) straight from a transcript path.'
    records = previous_turn(path)
    if not records:
        return None
    _, commands, tools = scan(records)
    text = scan(last_reply(records))[0]
    return claimed_without_running(text, commands, tools)


def edited_paths(records):
    'Paths passed to Edit/Write/MultiEdit/NotebookEdit in the turn.'
    paths = []
    for rec in records:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if (not isinstance(blk, dict) or blk.get("type") != "tool_use"
                    or blk.get("name") not in EDIT_TOOLS):
                continue
            inp = blk.get("input") or {}
            if not isinstance(inp, dict):
                continue
            for key in ("file_path", "filePath", "notebook_path", "notebookPath"):
                if isinstance(inp.get(key), str) and inp[key]:
                    paths.append(inp[key])
                    break
    return paths


def is_source(path):
    p = path.replace("\\", "/")
    if any(d in p for d in AGENT_DIRS):
        return False
    return os.path.splitext(p)[1].lower() in SOURCE_EXT


def edited_without_running(records):
    'Source files edited in a turn that checked nothing and said nothing, else None.'
    text, commands, tools = scan(records)
    if tools & SPAWNS:
        return None                      
    if ran_something(commands):
        return None                      
    edited = sorted({p for p in edited_paths(records) if is_source(p)})
    if not edited:
        return None
    names = {os.path.basename(p) for p in edited}
    if any(n in c for c in commands for n in names):
        return None                      
    if UNVERIFIED.search(text):
        return None                      
    return edited


def judge_unverified_edit(path):
    'Convenience: (edited source paths or None) straight from a transcript path.'
    records = previous_turn(path)
    if not records:
        return None
    return edited_without_running(records)
