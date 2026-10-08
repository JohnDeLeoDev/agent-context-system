#!/usr/bin/env python3
'SubagentStop: gate what a worker hands back before the lead ever sees it.\n\nWHY THIS EXISTS\n    The fleet delegates real work to workers a tier down, and until now nothing\n    inspected what came back. A subagent\'s summary lands directly in the lead\'s\n    context, and from there in the transcript, which syncs across the fleet and is\n    read back by later sessions. A credential that a worker happened to print while\n    debugging therefore had a clear path from a tool result the lead never saw into\n    permanent, replicated storage.\n\n    This is the deterministic form of a rule the global instruction already carries in\n    prose ("a subagent\'s self-report - inspect the tree/output yourself"). Anthropic\'s\n    Opus 5 guidance says to DELETE self-verification instructions of that shape because\n    the model now does them unprompted; the correct replacement for the half that is\n    about safety rather than correctness is a hook, not a sentence.\n\nSCOPE, STATED HONESTLY\n    Three things: leaked credentials, a decision the worker tried to hand to a human\n    it cannot reach, and a report long enough to damage the lead. For credentials,\n    only the near-zero-false-positive provider prefixes that memory-husk-guard already\n    blocks on (gh[pousr]_, sk-, AKIA, xox[abprs]-, AIza).\n    It does NOT verify tests passed, check for out-of-scope writes, or judge the\n    work - those need per-project knowledge this global hook does not have, and a\n    gate that guesses is a gate that gets disabled. Add project-scoped SubagentStop\n    hooks for those.\n\nMECHANISM\n    Exit 2 prevents the stop and feeds stderr back to the worker, which then gets a\n    turn to redact and report again. Exit 0 lets the return through. Fails OPEN on any\n    parse problem, like every other guard here: a worker that cannot report at all is\n    worse than one that reports something this hook could not read.'
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp













PHRASES = os.path.join(hp.scripts_dir(), "decision-phrases.py")






















WORD_BUDGET = 400






STATE_DIR = os.environ.get("SUBAGENT_LENGTH_STATE_DIR") or os.path.join(
    hp.state_dir(), "subagent-length")

FENCE = re.compile(r"```.*?```", re.S)
CODE = re.compile(r"`[^`]*`")


def _load_phrases():
    import importlib.util
    spec = importlib.util.spec_from_file_location("decision_phrases", PHRASES)
    if spec is None or spec.loader is None:
        raise ImportError(PHRASES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DECISION_FEEDBACK = """BLOCKED by subagent-return-gate: your report asks a human to decide something
({matched}), and you cannot reach one. You have no AskUserQuestion tool; only the lead
does.

Asking in prose here is how a decision gets made by the wrong party: the lead may
paraphrase it into a summary, or act on its own reading of it, and user never sees the
fork at all.

Report again, and hand the fork back in this shape so the lead can put it straight into
AskUserQuestion:

  DECISION NEEDED: <the one question, in a sentence>
  OPTION A: <label> -- <what it means, and the consequence>
  OPTION B: <label> -- <what it means, and the consequence>
  RECOMMENDED: <A or B, and why in one line>
  BLOCKED: <yes if you stopped work over it, no if you proceeded under an assumption>

If you proceeded under an assumption, say which one on the BLOCKED line. Do not pick one
and move on without saying so.

If this was NOT a real decision -- a rhetorical question, or something you have already
settled -- rewrite the sentence so it does not read as one, and report again. A choice
with a sensible default was yours to make, not user's."""

LENGTH_FEEDBACK = """BLOCKED by subagent-return-gate: your report is {words} words of prose. The budget
is {budget}, and fenced code and command output are NOT counted against it.

This is not a style note. Your report is pasted whole into the lead's context and is
re-sent with every later request in that session, so length here is charged over and
over to work you are not part of. It is also the lead's most recent example of how to
write, and the lead writes the answer user reads.

Report again, keeping only what the lead cannot get without you:

  - the ANSWER or the outcome, first, in a sentence
  - findings, one line each, with file:line so they can be checked
  - what you could NOT do, or did not verify
  - evidence in fenced blocks, which do not count against the budget

Cut: the restatement of your brief, the narration of which files you opened and in
what order, the recap of steps that led nowhere, and any table of things the lead
already knows. If a finding needs explanation, explain that one: the budget is
for findings."""





JUDGE = os.environ.get("TERSE_JUDGE") or os.path.join(
    hp.scripts_dir(), "terse-judge.py")

WORDS_FEEDBACK = """BLOCKED by subagent-return-gate: your report uses language user has banned:
{hits}.

Report again without it. State the fact alone: cut the intensifier, the comparison,
the narration of your steps, the offer and any Confidence or Severity tag. An em dash
becomes a period, a comma, or a colon. A quotation of a banned phrase goes in
backticks. Lists: get_doc("plain-language.md")."""




MAX_WORD_BLOCKS = 3
WORDS_STATE_DIR = os.environ.get("SUBAGENT_WORDS_STATE_DIR") or os.path.join(
    hp.state_dir(), "subagent-words")

CREDENTIAL = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{35})"
)

FEEDBACK = """BLOCKED by subagent-return-gate: your final message contains what looks like a
live credential ({kinds}).

Do not hand that back. Your summary goes straight into the lead's context and from
there into a transcript that is synced across the fleet and committed to three git
mirrors, so publishing it here is not recoverable by editing anything afterwards.

Report again with the secret removed. Name WHERE the value lives (the file, the 1Password
item, the env var). That is what the lead needs, and it is what the op gateway exists
to serve. If the value itself is the deliverable, say so and let the human fetch it; do
not route it through this channel."""


def _load_judge():
    import importlib.util
    spec = importlib.util.spec_from_file_location("terse_judge", JUDGE)
    if spec is None or spec.loader is None:
        raise ImportError(JUDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _worker_key(data):
    key = data.get("agent_id") or data.get("subagent_id") or ""
    session = data.get("session_id") or ""
    return ("%s-%s" % (session, key)).strip("-").replace("/", "_")


def word_blocks_left(data) -> bool:
    'Record one word block for this worker; False once MAX_WORD_BLOCKS are spent.\n\n    With no id there is nothing to count, so only a first report is blocked and\n    stop_hook_active marks the rewrite. A count that cannot be kept does the same.'
    name = _worker_key(data)
    if not name:
        return not data.get("stop_hook_active")
    path = os.path.join(WORDS_STATE_DIR, name)
    try:
        os.makedirs(WORDS_STATE_DIR, exist_ok=True)
        n = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                n = int((fh.read() or "0").strip() or 0)
        if n >= MAX_WORD_BLOCKS:
            return False
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(n + 1))
    except (OSError, ValueError):
        return not data.get("stop_hook_active")
    return True


def reset_word_blocks(data):
    name = _worker_key(data)
    if not name:
        return
    try:
        os.remove(os.path.join(WORDS_STATE_DIR, name))
    except OSError:
        pass


def length_blocked_already(data) -> bool:
    'Has this worker already been sent back once for length? Failure ALLOWS.'
    key = data.get("agent_id") or data.get("subagent_id") or ""
    session = data.get("session_id") or ""
    if not key and not session:
        return True
    name = ("%s-%s" % (session, key)).strip("-").replace("/", "_")
    path = os.path.join(STATE_DIR, name)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(path):
            return True
        with open(path, "w") as fh:
            fh.write("1")
    except OSError:
        return True
    return False


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  
    msg = data.get("last_assistant_message") or ""
    if not isinstance(msg, str) or not msg:
        return 0
    hits = CREDENTIAL.findall(msg)
    if hits:
        kinds = sorted({h[:4] + "..." for h in hits})
        print(FEEDBACK.format(kinds=", ".join(kinds)), file=sys.stderr)
        return 2

    
    
    try:
        asks = _load_phrases().find_decisions(msg)
    except Exception:
        return 0  
    if asks:
        
        if "DECISION NEEDED:" in msg:
            return 0
        matched = ['"%s"' % a for a in asks[:4]]
        print(DECISION_FEEDBACK.format(matched=", ".join(matched)), file=sys.stderr)
        return 2

    
    
    try:
        hits = _load_judge().banned(msg)
    except Exception:
        hits = []
    if hits and word_blocks_left(data):
        print(WORDS_FEEDBACK.format(hits=", ".join('"%s"' % h for h in hits)),
              file=sys.stderr)
        return 2
    if not hits:
        reset_word_blocks(data)

    if data.get("stop_hook_active"):
        return 0                        

    
    
    words = len(CODE.sub(" ", FENCE.sub(" ", msg)).split())
    if words > WORD_BUDGET and not length_blocked_already(data):
        print(LENGTH_FEEDBACK.format(words=words, budget=WORD_BUDGET), file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
