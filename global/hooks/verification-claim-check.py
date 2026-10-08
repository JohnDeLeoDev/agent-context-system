#!/usr/bin/env python3

'verification-claim-check — a claim that something was verified must have been verified.\n\nWhy this exists. The defect it catches: the code is correct and the final message\nclaims a verification that never happened. For example the message says "Both tests\npass" and the transcript shows no test command was run. That is a reporting failure,\nso a hook can catch it.\n\nWhy a hook and no instruction. "Report outcomes faithfully" and "verify with the\nstack\'s build/test" are in the always-loaded instruction. They are rules that get\ndropped late in a long turn, which is when the summary gets written. The claim and the\nevidence are both mechanically observable: the claim is in the final message, the\nevidence is whether a test or build command appears in the turn\'s tool calls.\n\nWhy UserPromptSubmit and not Stop. A Stop hook\'s `systemMessage` is terminal output\nfor the human; it does not enter model context, so an advisory there corrects nothing.\nUserPromptSubmit fires before the next turn is answered and injects `additionalContext`,\nwhich does reach the model. So this looks backward at the turn that just finished.\n\nConservative. A false positive here is expensive: it accuses an honest report, and a\nguard that cries wolf gets ignored. So it fires only when all of these hold:\n\n  * the message makes an assertive claim (not "if the tests pass", not "run the tests"),\n  * no test/build/lint command ran anywhere in that turn,\n  * no subagent was spawned (a worker may have run them where this cannot see),\n  * the turn used tools (a pure conversation turn claims nothing about a repo).\n\nNever blocks. The point is to put the gap in front of the model, not to litigate it.'
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp



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

SPAWNS = {"Task", "Agent", "Workflow"}


def _is_prompt(rec):
    'A real user message, not a tool result wearing the user role.'
    if rec.get("type") != "user" or rec.get("isMeta"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in content)
    return True


def previous_turn(path):
    'The records of the turn that just finished.\n\n    Whether the prompt being submitted is already in the transcript when this fires is\n    not something to assume -- the answer differs by harness version, and guessing wrong\n    silently yields an empty slice, which reads as "nothing to report" and would make\n    this hook permanently quiet. So: find the last prompt that actually has assistant\n    output after it, and take everything from there.'
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line, strict=False))
        except ValueError:
            continue
    for idx in range(len(records) - 1, -1, -1):
        if not _is_prompt(records[idx]):
            continue
        tail = records[idx + 1:]
        if any(r.get("type") == "assistant" for r in tail):
            return tail
    return []


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
    if not tools:
        return None                      
    if tools & SPAWNS:
        return None                      
    if any(RUNNER.search(c) or VAR_RUNNER.search(c) for c in commands):
        return None                      
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if CLAIM.search(sentence) and not HEDGE.search(sentence):
            return sentence.strip()[:200]
    return None













def disabled():
    names = (os.environ.get("AGENT_CONTEXT_DISABLE_HOOKS") or "").split(",")
    return "verification-claim-check" in {n.strip() for n in names}







def _judge():
    try:
        import importlib.util
        path = os.environ.get("VERIFICATION_CLAIM_JUDGE") or os.path.join(
            hp.scripts_dir(), "verification-claim-judge.py")
        spec = importlib.util.spec_from_file_location("vcj", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def main():
    if disabled():
        return
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return
    mod = _judge()
    if mod is not None:
        records = mod.previous_turn(path)
        if not records:
            return
        text, commands, tools = mod.scan(records)
        hit = mod.claimed_without_running(text, commands, tools)
    else:
        records = previous_turn(path)
        if not records:
            return
        text, commands, tools = scan(records)
        hit = claimed_without_running(text, commands, tools)
    if not hit:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": (
            "VERIFICATION CLAIM WITH NO RUN, in your previous message:\n"
            "  “%s”\n"
            "No test, build or lint command appears anywhere in that turn's tool calls.\n\n"
            "The work may be correct, but it is described as verified when it was "
            "not. Before continuing, "
            "either run it and report what came back, or say that it "
            "is unverified. Reading the code is not running it." % hit)}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:                    
        pass
    sys.exit(0)
