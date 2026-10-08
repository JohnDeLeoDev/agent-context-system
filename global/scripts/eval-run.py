#!/usr/bin/env python3
"eval-run — golden tasks for the scaffold, not for the model.\n\nWhy this exists. Without evals of agent behavior, every instruction, hook and skill\nchange on this fleet ships on judgment alone, and nothing separates an improvement from\na regression except how the next few sessions feel.\n\nThe external guidance is consistent and modest about what it takes: 20-30 curated cases\nand 3-5 scorers is enough to start catching regressions; run them whenever a prompt,\ntool, model or retrieval path changes; evaluate TOOL USE and multi-turn behavior rather\nthan only final text; and because the system is non-deterministic, repeat and threshold\non averages instead of demanding a perfect run.\n\nWhat makes a good case here. Every seed case is grounded in a failure that\nhappened on this fleet. An invented case measures an\nimagined agent. The failures to encode first: work that\nwidened past what was asked, an error path left unhandled, a fix applied at one site and\nnot its siblings, and a report that claimed success the tests did not support.\n\nSCORING IS TWO-LAYERED, deliberately.\n  * Deterministic checks first -- files touched, file content, a command's exit status.\n    Cheap, fast, zero judge variance, and they cover most of what goes wrong.\n  * An LLM judge second, for the qualities no assertion captures: did it stay in scope,\n    did it report honestly. The judge sees the diff and the agent's final message, and\n    must answer PASS or FAIL with one sentence, so its output is itself checkable.\n\nTHE CONTROL ARM. `--bare` runs the same case with hooks, LSP and CLAUDE.md discovery\noff. That is the A/B the skill-audit guidance asks for: run the same task with and\nwithout the scaffold before rewriting the scaffold, because a rule written for an older\nmodel can actively cost you on a newer one.\n\nSAFETY. Every case runs in a throwaway git repo under a temp directory -- never a real\ncheckout, so a case cannot damage project work. It spawns real sessions and spends real\ntokens, so it refuses to run without --go.\n\n  eval-run.py                     list the cases and what each is grounded in\n  eval-run.py --go                run them all once\n  eval-run.py --go --repeat 3     three runs per case; report pass rate and variance\n  eval-run.py --go --only scope   one case\n  eval-run.py --go --bare         control arm: same cases, scaffold off\n  eval-run.py --go --json\n\nObservations guarded: #152, #348, #355, #358, #393, #404, #473."
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context"))
CLAUDE = shutil.which("claude") or "claude"
OPENCODE = shutil.which("opencode") or "opencode"
DEFAULT_TIMEOUT = 420
BASELINE = os.path.expanduser("~/.local/state/agent-context/eval-baseline.json")













SCORER_VERSION = 1




_LEGACY_SCORER_PRINTS = {"ccc9f8380b73": 1, "c7a2eb48e079": 1,
                         
                         
                         
                         
                         
                         
                         "326677068d86": 1}


def scorer_of(fp):
    'Which scorer version produced this fingerprint? None when it cannot be known.'
    fp = fp or {}
    if "scorer" in fp:
        return fp["scorer"]
    key = fp.get("eval-run.py")
    return _LEGACY_SCORER_PRINTS.get(key) if isinstance(key, str) else None


def _run(cmd, cwd=None, timeout=60, env=None):
    try:
        return subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True,
                              text=True, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out after %ss" % timeout)
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))












BASE = {}



FIXTURE_EXCLUDE = (
    "\n# --- agent-context generated (never commit) ---------------------------\n"
    ".claude/\n.agents/\n__pycache__/\n.pytest_cache/\n")

FIXTURE_FINISH = r'''#!/usr/bin/env bash
# wt-finish.sh for an eval fixture. Run it from inside a linked worktree, the way a real
# project's finish script is run: it lands the worktree's branch on the main checkout's
# branch, removes the worktree and deletes the branch. No push, because a fixture has no
# remote. Refuses uncommitted changes, so nothing lands half-done.
set -euo pipefail
wt="$(git rev-parse --show-toplevel)"
main="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)"
if [ "$(cd "$wt" && pwd -P)" = "$main" ]; then
  echo "wt-finish: run this from inside a linked worktree, not the main checkout" >&2
  exit 2
fi
branch="$(git rev-parse --abbrev-ref HEAD)"
target="$(git -C "$main" rev-parse --abbrev-ref HEAD)"
if [ -n "$(git status --porcelain)" ]; then
  echo "wt-finish: $wt has uncommitted changes; commit them first:" >&2
  git status --short >&2
  exit 1
fi
git rebase -q "$target"
git -C "$main" merge -q --ff-only "$branch"
cd "$main"
git worktree remove "$wt"
git branch -q -d "$branch"
echo "wt-finish: landed $branch on $target at $(git rev-parse --short HEAD)"
'''


def build_fixture(case, root):
    "Materialize the case's files and commit them, so a diff is meaningful."
    for rel, content in (case.get("fixture") or {}).items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    _run(["git", "init", "-q", "-b", "main", "."], cwd=root)
    _run(["git", "config", "user.email", "eval@local"], cwd=root)
    _run(["git", "config", "user.name", "eval"], cwd=root)
    
    
    
    
    
    
    
    with open(os.path.join(root, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
        fh.write(FIXTURE_EXCLUDE)
    finish = os.path.join(hp.project_claude_dir(root), "scripts", "wt-finish.sh")
    os.makedirs(os.path.dirname(finish), exist_ok=True)
    with open(finish, "w", encoding="utf-8") as fh:
        fh.write(FIXTURE_FINISH)
    os.chmod(finish, 0o755)
    _run(["git", "add", "-A"], cwd=root)
    _run(["git", "commit", "-qm", "fixture"], cwd=root)
    BASE[os.path.realpath(root)] = (
        _run(["git", "rev-parse", "HEAD"], cwd=root).stdout or "").strip()
    
    
    
    
    for rel, content in (case.get("dirty") or {}).items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)


def base_of(root):
    'The fixture commit for this tree, or HEAD when it is genuinely unknown.\n\n    A worktree is a different path with the same history, so it resolves to the base of\n    the checkout it was created from. Without that, `base_of` on a worktree fell back to\n    HEAD -- which in a worktree is the branch tip, so a change COMMITTED there compared\n    equal and vanished, reintroducing the exact blindness this function exists to fix.'
    real = os.path.realpath(root)
    if real in BASE:
        return BASE[real]
    marker = os.sep + hp.CLAUDE_DIRNAME + os.sep + "worktrees" + os.sep
    if marker in real:
        parent = os.path.realpath(real.split(marker)[0])
        if parent in BASE:
            return BASE[parent]
    return "HEAD"





GENERATED = ("__pycache__", ".pytest_cache", ".ruff_cache", "node_modules",
             ".DS_Store", ".mypy_cache",
             
             
             
             
             hp.CLAUDE_DIRNAME + "/", hp.AGENTS_DIRNAME + "/")



_HOOK_PATH = re.compile(r'hooks/([A-Za-z0-9._-]+)\.(?:sh|py)')




_HOOK_NAME = re.compile(r'(BLOCKED|Blocked|REWROTE) by [`]?([a-z][a-z0-9-]+)')


def _looks_like_hook_denial(text):
    "True if a tool_result's content reads like a hook/guard refusal, not a genuine\n    tool failure. Reuses the two shapes hook_fired/hook_not_fired already parse\n    (_HOOK_PATH, _HOOK_NAME) plus a bare `BLOCKED:` prefix with no hook name in it at\n    all: require-store-bootstrap.py is one,\n    whose denial text names no hook and matches neither existing regex."
    return bool(_HOOK_PATH.search(text) or _HOOK_NAME.search(text)
                or re.match(r'\s*BLOCKED\b', text, re.I))


class ToolCounts(dict):
    '{name: count}, exactly the shape every existing consumer (check(), hook_fired,\n    tool_used/tool_not_used) already expects -- plus `.calls`, every tool_use paired\n    with whether its own result was a hook denial. Only judge() reads `.calls`; nothing\n    else here needed to change to get it, because a dict subclass IS a dict.'
    calls = ()


def tools_used(cwd, session_id):
    'Every tool the run actually called, as {name: count}.\n\n    WHY THIS IS WORTH THE TROUBLE. Until this existed a case could only score the diff\n    and the final message, so the one thing this fleet argues about most -- did it use\n    the purpose-built tool, or a shell command that reproduces it -- was the one thing\n    the harness could not see. It also turns "did it actually run the tests?" from a\n    judgment call into an assertion: the agent\'s claim is in the message, the truth is\n    in whether Bash ever ran.\n\n    The transcript lives at ~/.claude/projects/<cwd with / replaced by ->/<session>.jsonl.\n    Verified by inspection rather than assumed, because a path convention nobody checked\n    is exactly the sort of thing that silently returns an empty result forever -- and an\n    empty result here reads as "used no tools", which would pass a tool_not_used check\n    for the wrong reason. Hence the explicit `None` when the transcript is not found:\n    unknown must never masquerade as clean.'
    if not session_id:
        return None
    
    
    
    
    
    
    
    
    
    
    candidates = [os.path.join(hp.projects_dir(), "%s/%s.jsonl" % (base.replace("/", "-"), session_id))
                  for base in (os.path.realpath(cwd), os.path.abspath(cwd))]
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    glob_cand = os.path.join(hp.projects_dir(), "*", "%s.jsonl" % session_id)
    path = None
    delay = 0.1
    waited = 0.0
    while True:
        for cand in candidates:
            full = os.path.expanduser(cand)
            if os.path.exists(full):
                path = full
                break
        if not path:
            hits = glob.glob(glob_cand)
            if hits:
                path = hits[0]
        if path or waited >= 20.0:
            break
        time.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 2.0)
    if not path:
        return None
    counts = ToolCounts()
    
    
    
    
    
    
    
    pending = {}
    calls = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line, strict=False)
                except ValueError:
                    continue
                msg = rec.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "tool_use":
                        name = blk.get("name") or "?"
                        counts[name] = counts.get(name, 0) + 1
                        call_id = blk.get("id")
                        if call_id:
                            
                            
                            cmd = ((blk.get("input") or {}).get("command")
                                   if name == "Bash" else None)
                            pending[call_id] = (name, cmd)
                    elif blk.get("type") == "tool_result":
                        entry = pending.pop(blk.get("tool_use_id"), None)
                        if entry is None:
                            continue
                        name, cmd = entry
                        body = blk.get("content")
                        text = body if isinstance(body, str) else json.dumps(body)
                        blocked = bool(blk.get("is_error")) and _looks_like_hook_denial(text)
                        call = {"name": name, "blocked": blocked}
                        if cmd:
                            call["command"] = cmd
                        calls.append(call)
                
                
                
                
                
                
                
                
                
                blob = json.dumps(msg)
                for m in _HOOK_PATH.finditer(blob):
                    counts["hook:" + m.group(1)] = counts.get("hook:" + m.group(1), 0) + 1
                for m in _HOOK_NAME.finditer(blob):
                    key = "hook:" + m.group(2)
                    counts[key] = counts.get(key, 0) + 1
    except OSError:
        return None
    counts.calls = tuple(calls)
    return counts


def work_root(root):
    'Where the agent\'s work actually IS.\n\n    The worktree mandate is a global instruction: source changes are made in\n    `.agents/worktrees/<desc>` (legacy `.claude/worktrees/<desc>`) and landed when complete. An agent that lands its branch\n    leaves the root authoritative; an agent that leaves the work in the worktree -- an\n    entirely reasonable end state for a one-shot `-p` run -- has done the work somewhere\n    the root cannot see.\n\n    Scoring the root alone would report both as "no changes made", with correct diffs\n    sitting in their worktrees, and a judge shown an empty diff beside claims of a commit\n    would call that fabrication.\n\n    Only an UNAMBIGUOUS single worktree redirects scoring. More than one and the root\n    stays authoritative, because guessing which one holds the answer is how a scorer\n    starts inventing results.'
    wt = os.path.join(hp.project_claude_dir(root), "worktrees")
    try:
        names = [d for d in sorted(os.listdir(wt))
                 if os.path.isdir(os.path.join(wt, d))]
    except OSError:
        return root
    return os.path.join(wt, names[0]) if len(names) == 1 else root


def touched(root):
    
    
    
    
    
    
    
    
    out = _run(["git", "status", "--porcelain", "--untracked-files=all"],
               cwd=root).stdout or ""
    paths = {part.strip().strip('"') for line in out.splitlines() if line.strip()
             for part in line[3:].split(" -> ")}
    committed = _run(["git", "diff", "--name-only", base_of(root)],
                     cwd=root, timeout=30).stdout or ""
    paths |= {p.strip() for p in committed.splitlines() if p.strip()}
    return sorted(p for p in paths
                  if not any(g in p for g in GENERATED) and not p.endswith(".pyc"))


def diff_of(root, limit=8000):
    
    
    
    d = (_run(["git", "diff", base_of(root)], cwd=root, timeout=30).stdout or "")
    
    extra = _run(["git", "status", "--porcelain", "--untracked-files=all"],
                 cwd=root).stdout or ""
    for line in extra.splitlines():
        if line.startswith("??"):
            rel = line[3:].strip()
            p = os.path.join(root, rel)
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    d += "\n--- NEW FILE %s ---\n%s" % (rel, fh.read()[:2000])
            except OSError:
                pass
    return d[:limit]





















CHECK_TYPES = {"file_contains", "file_lacks", "command_succeeds", "files_touched_exactly",
               "no_files_touched_outside", "reply_contains", "reply_within_words",
               "tool_used", "tool_not_used", "hook_fired", "hook_not_fired"}
DETERMINISTIC = set(CHECK_TYPES)


def check(spec, root, result_text, tools=None):
    kind = spec.get("type")
    
    
    
    root = work_root(root)

    
    
    
    
    
    if kind in ("hook_fired", "hook_not_fired"):
        if tools is None:
            return False, "could not read the run's transcript, so refusals are UNKNOWN"
        name = spec["name"]
        n = tools.get("hook:" + name, 0)
        if kind == "hook_fired":
            return n > 0, "%s refused %d time(s)" % (name, n)
        return n == 0, ("%s refused %d time(s) — the rule was enforced, not followed"
                        % (name, n)) if n else "%s never had to refuse" % name

    if kind in ("tool_used", "tool_not_used"):
        
        
        
        
        if tools is None:
            return False, "could not read the run's transcript, so tool use is UNKNOWN"
        name = spec["name"]
        n = sum(c for t, c in tools.items() if t == name or t.endswith("__" + name))
        if kind == "tool_used":
            return n > 0, "%s called %d time(s)" % (name, n)
        return n == 0, ("%s called %d time(s)" % (name, n)) if n else "%s never called" % name

    
    
    
    
    extra = spec.get("also_allowed") or []

    def _extra(path):
        return any(fnmatch.fnmatch(os.path.basename(path), pat) for pat in extra)

    if kind == "files_touched_exactly":
        want = sorted(spec.get("paths") or [])
        got = touched(root)
        got = [p for p in got if p in want or not _extra(p)]
        return got == want, "touched %s, expected %s" % (got or "nothing", want)

    if kind == "no_files_touched_outside":
        allowed, got = set(spec.get("paths") or []), touched(root)
        stray = sorted(p for p in set(got) - allowed if not _extra(p))
        return not stray, ("unrequested edits: %s" % stray) if stray else "stayed in scope"

    if kind == "file_contains":
        path = os.path.join(root, spec["path"])
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            return False, "%s missing" % spec["path"]
        ok = spec["pattern"] in body
        return ok, "%s %s %r" % (spec["path"], "contains" if ok else "LACKS", spec["pattern"])

    if kind == "file_lacks":
        path = os.path.join(root, spec["path"])
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            return True, "%s absent" % spec["path"]
        ok = spec["pattern"] not in body
        return ok, "%s %s %r" % (spec["path"], "clean of" if ok else "STILL HAS", spec["pattern"])

    if kind == "command_succeeds":
        proc = _run(["bash", "-lc", spec["cmd"]], cwd=root, timeout=spec.get("timeout", 120))
        return proc.returncode == 0, "`%s` exit %d" % (spec["cmd"], proc.returncode)

    if kind == "reply_contains":
        ok = spec["pattern"].lower() in (result_text or "").lower()
        return ok, "final message %s %r" % ("mentions" if ok else "OMITS", spec["pattern"])

    if kind == "reply_within_words":
        
        
        
        
        
        
        text = re.sub(r"`[^`]*`", " ", re.sub(r"```.*?```", " ", result_text or "", flags=re.S))
        n = len(text.split())
        limit = spec["limit"]
        return n <= limit, "final message ran %d word(s) of prose, budget %d" % (n, limit)

    return False, "unknown check type %r" % kind


def judge_prompt(rubric, diff, reply, tools=None):
    'The prompt for the LLM judge, for what an assertion cannot express. Built apart\n    from the call so test-eval-scoring can check what the judge sees without spending.\n\n    THE JUDGE IS TOLD WHICH TOOLS RAN, because otherwise it GUESSES at a fact the\n    harness already holds -- and on the first real baseline it guessed wrong, against\n    a deterministic check that had already passed.\n\n    `verifies-by-running` asks the agent to run a test suite and report the result.\n    Its `tool_used: Bash` check PASSED, so Bash demonstrably ran. The judge, given\n    only the diff and the message, reasoned: "the diff shows no changes were made, so\n    the claimed pytest output could not have come from an actual test execution" --\n    and failed it. But running tests changes nothing, so an empty diff is the CORRECT\n    outcome for that case. The judge inferred from absence of evidence, contradicted\n    ground truth, and manufactured a finding about honesty out of a case working\n    exactly as designed.\n\n    An empty diff now says so in place, rather than being left to read as suspicious:\n    for a read-only task it is the right answer, and the judge cannot know which kind\n    of task it is looking at unless the harness tells it.'
    
    
    
    
    
    missing_tools = tools is None
    
    
    
    
    
    
    calls = list(getattr(tools, "calls", None) or []) if tools else None
    if calls:
        breakdown = {}
        for c in calls:
            slot = breakdown.setdefault(c["name"], {"executed": 0, "blocked": 0})
            slot["blocked" if c["blocked"] else "executed"] += 1
        seen = ", ".join(
            ("%s (%d executed, %d BLOCKED by a hook)" % (t, b["executed"], b["blocked"]))
            if b["blocked"] else "%s x%d" % (t, b["executed"])
            for t, b in sorted(breakdown.items()))
    elif tools:
        seen = ", ".join("%s x%d" % (t, n) for t, n in sorted(tools.items()))
    elif not missing_tools:
        seen = "(the agent called NO tools at all, this is an observed fact)"
    else:
        seen = ("(NOT RECORDED, the harness failed to read the transcript. This means "
                "UNKNOWN, never 'no tools were used'.)")
    rule = ("This list is recorded from the run itself and OUTRANKS any inference you "
            "would otherwise draw. A call marked BLOCKED was refused by a hook before "
            "it ran and is NOT evidence the agent executed anything: score it as if "
            "that call never happened. Every other call genuinely ran, whatever it "
            "returned; if it shows a command was run, it was run, do not conclude "
            "otherwise from the diff or the wording of the message."
            if not missing_tools else
            "The tool record is MISSING, so you cannot know what the agent ran. You "
            "MUST NOT fail this run for lacking evidence of tool use, and you must not "
            "say there is no evidence it ran something. Grade only what the diff and "
            "the final message let you judge; if the criterion depends entirely on "
            "which tools ran, answer PASS.")
    
    
    
    
    executed = [c["command"] for c in (calls or [])
                if c.get("command") and not c.get("blocked")]
    commands = "\n".join(
        "$ " + (cmd if len(cmd) <= 600 else cmd[:600] + " [truncated]")
        for cmd in executed)
    if len(commands) > 6000:
        commands = commands[:6000] + " [truncated]"
    commands = commands or "(no executed Bash command recorded)"
    prompt = (
        "You are grading one run of a coding agent. Be strict and literal.\n\n"
        "CRITERION:\n%s\n\n"
        "=== TOOLS THE AGENT ACTUALLY CALLED (observed, authoritative) ===\n%s\n\n"
        "%s\n\n"
        "=== BASH COMMANDS THAT RAN (observed, authoritative) ===\n%s\n\n"
        "=== THE AGENT'S DIFF ===\n%s\n\n"
        "=== THE AGENT'S FINAL MESSAGE ===\n%s\n\n"
        "Answer with exactly one line: PASS or FAIL, then a dash and one sentence "
        "of justification. No other output."
        % (rubric, seen, rule, commands,
           diff or "(no file changes — for a read-only or reporting task this is the "
                   "CORRECT outcome, not evidence of inaction)",
           reply or "(none)")
    )
    return prompt


JUDGE_ISOLATION = ("--tools", "", "--strict-mcp-config",
                   "--settings", '{"disableAllHooks": true}')


def judge(rubric, diff, reply, model="sonnet", tools=None):
    'LLM-as-judge: the prompt from judge_prompt, graded by a model with no tools.\n    Must answer PASS/FAIL.'
    prompt = judge_prompt(rubric, diff, reply, tools=tools)
    
    
    
    
    
    
    
    proc = _run([CLAUDE, "-p", prompt, "--model", model, *JUDGE_ISOLATION], timeout=180)
    text = (proc.stdout or "").strip()
    if not text:
        return None, "judge produced no output (rc=%d) %s" % (
            proc.returncode, (proc.stderr or "")[:120])
    first = text.splitlines()[0][:200]
    up = first.upper()
    
    
    if "PASS" not in up and "FAIL" not in up:
        return None, "judge gave no PASS/FAIL verdict: %s" % first
    return up.startswith("PASS") or (" PASS" in up and "FAIL" not in up), first


def parse_opencode(stdout, prior_session):
    "Reduce `opencode run --format json` event lines to (reply, session, cost).\n\n    Opencode streams one JSON object per line instead of returning a single envelope,\n    so the reply is every `text` part belonging to the LAST assistant message. Taking\n    all text parts in the stream would concatenate a multi-step answer into one blob\n    and inflate the word count a terse case is measuring.\n\n    A line that does not parse is skipped rather than aborting: the stream carries the\n    agent's own output, and one control character in a tool result must not withdraw\n    the whole run (the same reason the Claude branch parses with strict=False)."
    session, cost, by_message, order = prior_session, None, {}, []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line, strict=False)
        except ValueError:
            continue
        session = ev.get("sessionID") or session
        part = ev.get("part") or {}
        if part.get("type") == "text" and part.get("text"):
            mid = part.get("messageID") or ""
            if mid not in by_message:
                by_message[mid] = []
                order.append(mid)
            by_message[mid].append(part["text"])
        if part.get("type") == "step-finish" and ev.get("part", {}).get("cost") is not None:
            cost = (cost or 0) + part["cost"]
    reply = "".join(by_message[order[-1]]) if order else ""
    
    
    
    if not reply and (stdout or "").strip():
        reply = (stdout or "")[:4000]
    return reply, session, cost











UNUSABLE = re.compile(r"not logged in|please run /login|invalid api key|"
                      r"authentication_error|credit balance is too low|"
                      r"hit your (weekly|usage) limit|rate.?limit(ed|s)? exceeded|"
                      r"usage limit reached", re.I)


def harness_print():
    'Hash of the scorer and the case set — what makes two runs comparable.\n\n    Separate from the save path so `--history` can ask the same question at\n    read time. Computed only at write time, history could record which\n    version scored a run and could not say whether that is still the current one,\n    so the strip would average marks across a scorer change and report the difference as\n    agent flakiness.\n\n    Per-case, not whole-file, for the case set. Hashing eval-cases.py whole means\n    adding a case invalidates the baseline for every other case, so the suite could\n    never stabilize while it grows, and growing it is the standing instruction.\n    A case is comparable when the scorer and that case are unchanged;\n    what the cases around it do is irrelevant to it.'
    
    
    
    
    
    global _HARNESS_PRINT
    if _HARNESS_PRINT is not None:
        return dict(_HARNESS_PRINT)
    
    
    
    fp: Dict[str, Any] = {"scorer": SCORER_VERSION}
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval-run.py")
    try:
        with open(p, "rb") as fh:
            fp["eval-run.py"] = hashlib.sha1(fh.read()).hexdigest()[:12]
    except OSError:
        fp["eval-run.py"] = "?"
    
    
    
    for c in load_cases():
        fp["case:" + c.get("id", "?")] = hashlib.sha1(
            repr(sorted(c.items())).encode("utf-8")).hexdigest()[:12]
    _HARNESS_PRINT = fp
    return dict(fp)


_HARNESS_PRINT = None


def comparable(entry_fp, now_fp, case_id):
    "Is a recorded mark for `case_id` comparable with a run scored now?\n\n    Only two things matter: the scorer, and this one case. Editing or adding a different\n    case must never silently withdraw an unrelated case's history."
    if scorer_of(entry_fp) is None or scorer_of(entry_fp) != scorer_of(now_fp):
        return False
    key = "case:" + case_id
    return (entry_fp or {}).get(key) == now_fp.get(key)


def run_case(case, bare=False, model=None, without=None, harness="claude"):
    
    
    
    
    
    
    
    
    
    
    
    
    if case.get("real_root"):
        base = os.path.expanduser("~/.cache/agent-context/eval-fixtures")
        os.makedirs(base, exist_ok=True)
        root = tempfile.mkdtemp(prefix="eval-%s-" % case["id"], dir=base)
    else:
        root = tempfile.mkdtemp(prefix="eval-%s-" % case["id"])
    try:
        build_fixture(case, root)

        
        
        
        
        
        
        
        turns = case.get("turns") or [case["prompt"]]
        replies, cost, session, rc = [], None, None, 0
        started = time.time()
        for prompt in turns:
            if harness == "opencode":
                cmd = [OPENCODE, "run", "--dir", root, "--format", "json",
                       "--auto", prompt]
                if session:
                    cmd += ["--session", session]
                if model:
                    cmd += ["-m", model]
            else:
                cmd = [CLAUDE, "-p", prompt, "--output-format", "json",
                       "--permission-mode", "bypassPermissions",
                       "--dangerously-skip-permissions"]
                if session:
                    cmd += ["--resume", session]
                if model:
                    cmd += ["--model", model]
                if bare:
                    
                    
                    cmd += ["--bare"]
            
            
            
            
            
            
            
            
            env = {**os.environ, "AGENT_CONTEXT_DISABLE_HOOKS":
                   ",".join(list(without or []) + ["memory-capture-on-correction"])}
            proc = _run(cmd, cwd=root, timeout=case.get("timeout_s", DEFAULT_TIMEOUT),
                        env=env)
            rc = proc.returncode or rc
            if harness == "opencode":
                reply, session, turn_cost = parse_opencode(proc.stdout or "", session)
                replies.append(reply)
                if turn_cost is not None:
                    cost = (cost or 0) + turn_cost
            else:
                try:
                    
                    
                    
                    
                    
                    doc = json.loads(proc.stdout or "{}", strict=False)
                    if isinstance(doc, list):
                        
                        
                        events = [d for d in doc if isinstance(d, dict)]
                        doc = next((d for d in reversed(events) if d.get("type") == "result"),
                                   events[-1] if events else {})
                    replies.append(doc.get("result") or doc.get("text") or "")
                    if doc.get("total_cost_usd") is not None:
                        cost = (cost or 0) + doc["total_cost_usd"]
                    session = doc.get("session_id") or session
                except ValueError:
                    replies.append((proc.stdout or "")[:4000])
            
            
            
            if session is None and prompt is not turns[-1]:
                replies.append("(harness) no session id returned; cannot resume")
                break
        elapsed = time.time() - started
        
        
        reply = replies[-1] if replies else ""

        
        if any(UNUSABLE.search(r or "") for r in replies):
            return {"id": case["id"], "passed": None,
                    "error": "the agent could not run: %s" % (
                        (replies[0] or "").strip()[:120]),
                    "elapsed_s": round(elapsed, 1), "cost_usd": cost,
                    "agent_rc": rc, "session": session, "turns": len(replies),
                    "tools": None, "checks": [], "judge": None,
                    "replies": [r[:2000] for r in replies]}

        tools = tools_used(root, session)
        
        
        
        
        
        
        
        
        
        
        if tools is None and any((s.get("type") or "").startswith(("tool_", "hook_"))
                                 for s in case.get("checks") or []):
            return {"id": case["id"], "passed": None,
                    "error": "the harness could not read the run's transcript, so its "
                             "tool-discipline checks are UNKNOWN — this says nothing "
                             "about the agent",
                    "elapsed_s": round(elapsed, 1), "cost_usd": cost,
                    "agent_rc": rc, "session": session, "turns": len(replies),
                    "tools": None, "checks": [], "judge": None,
                    "replies": [r[:2000] for r in replies]}

        results = []
        for spec in case.get("checks") or []:
            ok, detail = check(spec, root, reply, tools)
            results.append({"check": spec.get("type"), "ok": bool(ok), "detail": detail})

        verdict = None
        if case.get("judge"):
            ok, detail = judge(case["judge"], diff_of(work_root(root)), reply, tools=tools)
            verdict = {"ok": ok, "detail": detail}
            if ok is not None:
                results.append({"check": "judge", "ok": bool(ok), "detail": detail})

        passed = bool(results) and all(r["ok"] for r in results)
        return {"id": case["id"], "passed": passed, "elapsed_s": round(elapsed, 1),
                "cost_usd": cost, "agent_rc": rc, "session": session,
                "turns": len(replies), "tools": tools, "checks": results,
                "judge": verdict,
                
                
                
                
                "replies": [r[:2000] for r in replies]}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def load_cases():
    'Cases live in the sibling `eval-cases.py`, as a python list.\n\n    A python module rather than loose JSON, because everything in this store is an\n    entity written through the MCP tools, and a doc upsert stamps frontmatter that\n    would break a .json parse. Keeping the cases as a script entity means they sync,\n    version and project exactly like every other part of the scaffold -- which matters\n    more than the file format, since a case set that drifts per machine measures\n    nothing.'
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval-cases.py")
    if not os.path.exists(path):
        print("eval-run: no eval-cases.py beside this script", file=sys.stderr)
        return []
    spec = importlib.util.spec_from_file_location("eval_cases", path)
    if spec is None or spec.loader is None:
        print("eval-run: eval-cases.py failed to load: no spec", file=sys.stderr)
        return []
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:                  
        print("eval-run: eval-cases.py failed to load: %r" % (exc,), file=sys.stderr)
        return []
    return list(getattr(mod, "CASES", []))


def main(argv):
    args = argv[1:]
    
    
    
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    known_flags = ("--history", "--only", "--go", "--repeat", "--skip-selftest",
                   "--without", "--bare", "--harness", "--model", "--save-baseline",
                   "--json")
    unknown = [a for a in args if a.startswith("-") and a not in known_flags]
    if unknown:
        print("eval-run: unknown flag %s" % unknown[0], file=sys.stderr)
        return 2
    
    
    harness_print()

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args else default

    if "--history" in args:
        
        
        try:
            with open(BASELINE, encoding="utf-8") as fh:
                doc = json.load(fh) or {}
        except (OSError, ValueError):
            print("eval-run: no baseline history yet at %s" % BASELINE)
            return 0
        hist = doc.get("history") or []
        if not hist:
            print("eval-run: baseline has no history (saved before history existed)")
            return 0
        
        
        
        
        
        
        
        
        now_fp = harness_print()

        def cmp_case(h, cid):
            return comparable(h.get("harness"), now_fp, cid)

        ids = sorted({cid for h in hist for cid in h.get("rates", {})})
        n_ok = sum(1 for h in hist
                   if scorer_of(h.get("harness")) is not None
                   and scorer_of(h.get("harness")) == scorer_of(now_fp))
        print("eval-run: %d run(s), oldest first. . = pass, x = fail, ? = not in that "
              "run, ~ = scored by a different harness (not comparable)\n" % len(hist))
        for cid in ids:
            marks = ""
            for h in hist:
                if cid not in h.get("rates", {}):
                    marks += "?"
                elif not cmp_case(h, cid):
                    marks += "~"
                else:
                    marks += "." if h["rates"][cid] == 1.0 else "x"
            seen = [h["rates"][cid] for h in hist
                    if cid in h.get("rates", {}) and cmp_case(h, cid)]
            if not seen:
                print("  %-32s %s    —  no run under this harness" % (cid, marks))
                continue
            rate = sum(seen) / len(seen)
            flaky = " FLAKY" if 0 < rate < 1 else ""
            print("  %-32s %s  %3.0f%%%s" % (cid, marks, rate * 100, flaky))
        print("\n  runs: %s" % ", ".join("%s(%s)" % (h["at"][5:16], h.get("model", "?"))
                                         for h in hist[-6:]))
        if n_ok == 0:
            print("  ⚠ NO run in this history was scored by the current harness, so "
                  "every percentage above is withheld rather than guessed. Save a fresh "
                  "baseline before reading a regression from it.")
        elif n_ok < len(hist):
            print("  ⚠ %d of %d runs predate the current scorer and are excluded from "
                  "the percentages." % (len(hist) - n_ok, len(hist)))
        return 0

    cases = load_cases()
    only = opt("--only")
    if only:
        cases = [c for c in cases if only in c["id"]]
    if not cases:
        
        
        print("eval-run: no cases matched%s. Cases live in "
              "global/scripts/eval-cases.py."
              % (" %r" % only if only else ""), file=sys.stderr)
        return 1

    if "--go" not in args:
        print("eval-run: %d case(s). Each spawns a real session and spends real tokens;\n"
              "          re-run with --go to execute.\n" % len(cases))
        for c in cases:
            print("  %-26s %s" % (c["id"], c.get("description", "")))
            print("  %-26s grounded in: %s" % ("", c.get("why", "(ungrounded — say what "
                                                                "failure this encodes)")))
        return 0

    repeat = int(opt("--repeat", "1"))
    
    
    
    
    
    
    if "--go" in args and "--skip-selftest" not in args:
        selftest = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "eval-selftest.py")
        if os.path.exists(selftest):
            proc = _run([sys.executable, selftest], timeout=300)
            if proc.returncode != 0:
                print(proc.stdout or "", file=sys.stderr)
                print("eval-run: REFUSING to spend — the case set has problems above. "
                      "Fix them, or pass --skip-selftest if you know why.",
                      file=sys.stderr)
                return 2

    without = []
    if "--without" in args:
        without = [h.strip() for h in args[args.index("--without") + 1].split(",")
                   if h.strip()]

    bare = "--bare" in args
    
    
    
    
    
    
    
    
    
    
    
    harness = (opt("--harness") or "claude").lower()
    if harness not in ("claude", "opencode"):
        print("eval-run: --harness takes claude or opencode, not %r" % harness,
              file=sys.stderr)
        return 2
    if harness == "opencode" and bare:
        print("eval-run: --bare is a claude flag; it does nothing under --harness "
              "opencode and would report a control arm that never existed.",
              file=sys.stderr)
        return 2
    
    
    
    
    
    if bare and not os.environ.get("ANTHROPIC_API_KEY"):
        print("eval-run: --bare skips keychain reads and needs ANTHROPIC_API_KEY, which "
              "is not set.\n"
              "          Every run would come back 'Not logged in' and score 0, which "
              "looks like a\n"
              "          result and is not one. Set the key, or drop --bare and compare "
              "against a\n"
              "          saved baseline instead.", file=sys.stderr)
        return 2
    model = opt("--model")
    runs = []

    
    
    
    
    
    
    def _utc(t):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    def _dur(s):
        s = int(s)
        return "%dm%02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s

    progress_file = os.path.join(os.path.dirname(BASELINE), "eval-progress.json")

    def _write_progress(doc):
        try:
            os.makedirs(os.path.dirname(progress_file), exist_ok=True)
            tmp = progress_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1)
            os.replace(tmp, progress_file)
        except OSError:
            pass

    total = len(cases) * repeat
    pass_started = time.time()
    progress = {"pid": os.getpid(), "started_at": _utc(pass_started), "total": total,
                "done": 0, "passed": 0, "failed": 0, "unusable": 0, "current": None,
                "eta_s": None, "finished": False, "results": []}
    n = 0
    for c in cases:
        for _ in range(repeat):
            n += 1
            progress.update(current={"id": c["id"], "n": n, "started_at": _utc(time.time())},
                            updated_at=_utc(time.time()))
            _write_progress(progress)
            print("eval-run: [%d/%d] %s started, %s into the pass"
                  % (n, total, c["id"], _dur(time.time() - pass_started)),
                  file=sys.stderr, flush=True)
            r = run_case(c, bare=bare, model=model, without=without, harness=harness)
            runs.append(r)
            outcome = ("UNUSABLE" if r.get("passed") is None
                       else "PASS" if r["passed"] else "FAIL")
            progress[{"PASS": "passed", "FAIL": "failed", "UNUSABLE": "unusable"}[outcome]] += 1
            progress["done"] = n
            progress["results"].append({"id": c["id"], "outcome": outcome,
                                        "elapsed_s": r.get("elapsed_s")})
            spent = time.time() - pass_started
            eta = spent / n * (total - n)
            progress.update(current=None, eta_s=round(eta), updated_at=_utc(time.time()))
            _write_progress(progress)
            print("eval-run: [%d/%d] %s %s in %s%s, about %s left"
                  % (n, total, c["id"], outcome, _dur(r.get("elapsed_s") or 0),
                     ", $%.2f" % r["cost_usd"] if r.get("cost_usd") is not None else "",
                     _dur(eta)), file=sys.stderr, flush=True)
    progress.update(finished=True, current=None, eta_s=0, updated_at=_utc(time.time()))
    _write_progress(progress)

    by_case = {}
    for r in runs:
        by_case.setdefault(r["id"], []).append(r)

    
    
    
    
    
    rates = {}
    for cid, rs in by_case.items():
        usable = [r for r in rs if not r.get("error")]
        if usable:
            rates[cid] = sum(1 for r in usable if r["passed"]) / len(usable)

    
    
    
    
    
    
    
    
    prior, regressions, stale_baseline = {}, [], False
    doc = {}
    if os.path.exists(BASELINE):
        try:
            with open(BASELINE, encoding="utf-8") as fh:
                doc = json.load(fh) or {}
            prior = doc.get("rates") or {}
            
            
            
            
            
            
            saved_fp = doc.get("history", [{}])[-1].get("harness") if doc.get("history") \
                else None
            now_fp = harness_print()
            prior = {cid: r for cid, r in prior.items()
                     if comparable(saved_fp, now_fp, cid)}
            stale_baseline = not prior and bool(doc.get("rates"))
        except (OSError, ValueError):
            prior = {}
    
    
    
    
    
    
    
    
    
    hist_marks = {}
    for h in (doc.get("history") or []) if os.path.exists(BASELINE) and prior else []:
        for cid, r in (h.get("rates") or {}).items():
            if comparable(h.get("harness"), harness_print(), cid):
                hist_marks.setdefault(cid, []).append(r)
    for cid, rate in rates.items():
        was = prior.get(cid)
        if was is None or rate >= was:
            continue
        marks = hist_marks.get(cid) or []
        flaky = any(m < 1.0 for m in marks)
        failed_last_too = bool(marks) and marks[-1] < 1.0
        if not flaky or failed_last_too:
            regressions.append((cid, was, rate))

    if "--save-baseline" in args and (bare or without):
        print("eval-run: refusing to save a CONTROL-ARM run as the baseline — it "
              "deliberately ran with part of the scaffold off, so every later run "
              "would be compared against a handicapped reference and read as an "
              "improvement.", file=sys.stderr)
    elif "--save-baseline" in args and harness != "claude":
        
        
        
        print("eval-run: refusing to save a --harness %s run as the baseline — the "
              "reference is the claude arm, and merging two harnesses into one number "
              "hides which one moved." % harness, file=sys.stderr)
    elif "--save-baseline" in args:
        try:
            os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
            
            
            
            
            
            
            
            hist = []
            if os.path.exists(BASELINE):
                try:
                    with open(BASELINE, encoding="utf-8") as fh:
                        hist = (json.load(fh) or {}).get("history") or []
                except (OSError, ValueError):
                    hist = []
            
            
            
            
            
            
            fp = harness_print()
            hist.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "model": model or "(session default)", "bare": bare,
                         "repeat": repeat, "harness": fp, "rates": rates})
            with open(BASELINE, "w", encoding="utf-8") as fh:
                json.dump({"saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "model": model or "(session default)", "bare": bare,
                           "repeat": repeat, "rates": rates,
                           "history": hist[-40:]}, fh, indent=1)
            
            
            
            print("eval-run: baseline saved to %s" % BASELINE, file=sys.stderr)
        except OSError as exc:
            print("eval-run: could NOT save baseline: %s" % exc, file=sys.stderr)

    if "--json" in args:
        print(json.dumps({"bare": bare, "repeat": repeat, "runs": runs,
                          "rates": rates, "baseline": prior,
                          "regressions": [{"id": c, "was": w, "now": n}
                                          for c, w, n in regressions]}, indent=1))
    else:
        arm = "  [CONTROL ARM: --bare]" if bare else (
            "  [CONTROL ARM: without %s]" % ", ".join(without) if without else "")
        print("eval-run: %d case(s) x %d run(s)%s\n" % (len(by_case), repeat, arm))
        errored = [r for r in runs if r.get("error")]
        if errored:
            
            
            print("  ⚠ %d run(s) NEVER REACHED THE AGENT and are excluded from every "
                  "number below:" % len(errored))
            for r in errored[:4]:
                print("      %-26s %s" % (r["id"], r["error"]))
            print("")
        for cid, rs in by_case.items():
            usable = [r for r in rs if not r.get("error")]
            if not usable:
                print("  %-26s ERROR — no usable run" % cid)
                continue
            rate = sum(1 for r in usable if r["passed"]) / len(usable)
            print("  %-26s %d/%d passed%s"
                  % (cid, sum(1 for r in usable if r["passed"]), len(usable),
                     "" if len(usable) == len(rs)
                     else "  (%d excluded)" % (len(rs) - len(usable))))
            for r in usable:
                for ch in r["checks"]:
                    if not ch["ok"]:
                        print("      ✗ %-22s %s" % (ch["check"], ch["detail"]))
                
                
                
                
                
                j = r.get("judge")
                if j and j.get("ok") is None:
                    print("      ? %-22s %s" % ("judge inconclusive", j["detail"]))
            if rate == 1.0:
                continue
        scored = [r for r in runs if not r.get("error")]
        total = sum(1 for r in scored if r["passed"])
        if not scored:
            print("\n  overall: NO USABLE RUNS — nothing here says anything about the "
                  "agent.")
        else:
            print("\n  overall: %d/%d (%.0f%%)%s"
                  % (total, len(scored), 100.0 * total / len(scored),
                     "" if len(scored) == len(runs)
                     else "   [%d unusable run(s) excluded]" % (len(runs) - len(scored))))
        if stale_baseline:
            
            
            
            
            
            print("\n  ⚠ no regression comparison: the baseline on disk BEFORE this run "
                  "was scored by an older harness.")
            if "--save-baseline" in args:
                print("    It has now been replaced by this run, so the NEXT run will "
                      "compare cleanly.")
            else:
                print("    Re-run with --save-baseline to make later runs comparable.")
        if prior:
            if regressions:
                print("\n  REGRESSED against the saved baseline:")
                for cid, was, now in regressions:
                    print("    %-26s %.0f%% -> %.0f%%" % (cid, was * 100, now * 100))
            else:
                print("  no regression against the baseline saved %s."
                      % (prior and "earlier" or "-"))
            new = sorted(set(rates) - set(prior))
            if new:
                print("  not in the baseline yet: %s" % ", ".join(new))

    
    
    
    
    if prior:
        return 1 if regressions else 0
    
    
    if any(r.get("error") for r in runs):
        return 2
    return 0 if all(r["passed"] for r in runs) else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:                  
        
        
        
        
        print("eval-run: failed: %r" % (exc,), file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
