#!/usr/bin/env python3
"Stop: refuse a turn that hands user a step the agent could take itself.\n\nWHY THIS EXISTS\n    An agent that can run a command should run it and report the result. A final\n    message that ends with a command for user to run, or a check for him to make,\n    gives him work the agent could have done.\n\n    A step gated on a sync or a build is no exception. The way to wait is one bounded\n    foreground command that re-checks and then does the step. A background task is the\n    wrong tool: Claude Code writes Monitor and run_in_background output under\n    /private/tmp, outside home.\n\nWHAT IT DOES\n    On Stop, reads the final assistant message's visible prose (fenced blocks, code\n    spans and quoted lines stripped, through decision-phrases.visible_text). If the\n    prose tells user to run, re-run, check or verify something, exit 2 with the reason.\n\n    Second trigger: a short fenced shell command in a message that never says it ran.\n    Such a message can address nobody, so no verb pattern fires, and user would still\n    have to paste the command.\n    Long or language-tagged blocks (a script he asked for) and any message with past\n    tense in it (a report of work already done) are left alone.\n\nWHAT IT ALLOWS\n    A step only user can take: a login or sign-in, a password or passphrase, Touch ID\n    or 2FA, sudo, an interactive prompt. Those are matched on the raw message, since the\n    command usually sits in a code span.\n\n    An approval is not such a step. user approves through AskUserQuestion (hook\n    approval-question), so a typed approval command handed to him is refused outright,\n    with its own message, before any allowance is considered.\n\nLOOP SAFETY\n    `stop_hook_active` means a Stop hook already blocked this stop, so this one allows\n    unconditionally. The worst case is one extra turn, never a wedged session. Fails\n    open on any parse problem or a missing phrase module.\n\nCROSS-HARNESS\n    Claude Code only. pi, opencode and Copilot CLI have no Stop wiring for this, so the\n    rule stays instruction-level there.\n\nPython 3.8-safe: the Synology nodes run hooks on the system 3.8.15."
import importlib.util
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

PHRASES = os.path.join(hp.scripts_dir(), "decision-phrases.py")


_VERBS = (r"(run|re-?run|execute|restart|ssh|chmod|check|verify|confirm|trigger|"
          r"kick\s+off|install|merge|push|delete|remove|apply)")



_CLAUSE = r"(?:[^.\n]|\.(?=\S)){0,100}"
HANDBACK = re.compile(
    r"\b(you|user)\s*(can|could|should|need\s+to|will\s+need\s+to|'ll\s+need\s+to|"
    r"may\s+want\s+to|might\s+want\s+to|just\s+need\s+to)\s+" + _VERBS + r"\b"
    
    
    r"|^\s*(then\s+|next,?\s+|finally,?\s+|now\s+)?(run|re-?run|execute|paste)"
    r"(\s*:|\s{2,}|\s+(this|these|the\s+following|it)\b)"
    
    r"|\b(once|when|after)\b" + _CLAUSE + r",\s*(run|re-?run|execute|check|verify|remove|apply)\b"
    r"|\bnext\s+step[^.\n]{0,30}:\s*(run|re-?run|execute|check|verify)\b",
    re.I | re.M,
)


ONLY_JOHN = re.compile(
    r"\blog\s*in\b|\blogin\b|sign\s*-?in|signin|password|passphrase|"
    r"\b2fa\b|touch\s*id|\bsudo\b|interactive|only\s+you\s+can",
    re.I,
)





FENCE_BLOCK = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)


CMD_HEAD = re.compile(
    r"^\s*(sudo\s+)?(systemctl|launchctl|service|git|gh|chezmoi|ssh|scp|rsync|chmod|"
    r"chown|mkdir|rm|cp|mv|ln|touch|curl|wget|tar|unzip|npm|pnpm|yarn|node|python3?|"
    r"pip3?|pytest|make|cargo|go|dotnet|swift|xcodebuild|gradlew?|docker|podman|apt|"
    r"brew|tmux|crontab|claude|opencode|pi|bash|sh|zsh)\b")

ALREADY_RAN = re.compile(
    r"\b(ran|re-?ran|running\s+it\s+now|executed|i\s+ran|already\s+(ran|did)|applied|"
    r"committed|installed|disabled|enabled|removed|landed|started|stopped|restarted|"
    r"created|deleted|pushed|merged|passed|failed)\b", re.I)


def _pending_command(msg):
    'The first fenced shell command in a message that never says it was run.'
    if ALREADY_RAN.search(msg):
        return None
    for lang, body in FENCE_BLOCK.findall(msg):
        if lang.strip().lower() not in ("", "bash", "sh", "shell", "console", "zsh"):
            continue
        lines = [ln for ln in body.splitlines() if ln.strip()]
        
        if not lines or len(lines) > 3:
            continue
        if CMD_HEAD.match(lines[0]):
            return " ".join(lines[0].split())
    return None


FEEDBACK = """BLOCKED by block-handing-user-a-runnable-step: this turn hands user a step you can take yourself ({matched}).
Run it now and report the result. To wait on a sync or build, run one bounded foreground
command with a timeout that re-checks, then acts. Not Monitor or run_in_background.
Hand user only a login, password, sudo, interactive prompt, or decision (AskUserQuestion), and say which.
If you already ran it, report it in the past tense ("Ran ...: 784 passed")."""




APPROVAL_COMMAND = re.compile(r"\b(?:bash|python3?)\s+\S*-consent\.(?:sh|py)\b|`[^`\n]*-consent\.(?:sh|py)[^`\n]*`")

RAN_BEFORE = re.compile(r"\b(?:ran|re-?ran|executed|used)\W{0,3}\Z", re.I)

APPROVAL_FEEDBACK = """BLOCKED by block-handing-user-a-runnable-step: this turn hands user an approval command to type ({matched}).
Ask with AskUserQuestion in the approval shape; approval-question grants it when he picks Approve.
The refusal that led here prints the exact question. Every shape: HOW_TO_ASK in ~/.agent-context/global/hooks/approval-question.py."""


def _visible(msg):
    spec = importlib.util.spec_from_file_location("decision_phrases", PHRASES)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % PHRASES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.visible_text(msg)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict) or data.get("stop_hook_active"):
        return 0
    
    
    
    names = [n.strip() for n in (os.environ.get("AGENT_CONTEXT_DISABLE_HOOKS") or "").split(",")]
    if "block-handing-user-a-runnable-step" in names or \
            "block-handing-user-a-runnable-step.py" in names:
        return 0
    msg = data.get("last_assistant_message") or ""
    if not isinstance(msg, str) or not msg.strip():
        return 0
    approval = next((m for m in APPROVAL_COMMAND.finditer(msg)
                     if not RAN_BEFORE.search(msg[max(0, m.start() - 40):m.start()])), None)
    if approval:
        print(APPROVAL_FEEDBACK.format(matched=" ".join(approval.group(0).split())),
              file=sys.stderr)
        return 2
    if ONLY_JOHN.search(msg):
        return 0
    try:
        prose = _visible(msg)
    except Exception:
        return 0
    hits = []
    pending = _pending_command(msg)
    if pending:
        hits.append(pending)
    for m in HANDBACK.finditer(prose):
        h = " ".join(m.group(0).split())
        if h.lower() not in [x.lower() for x in hits]:
            hits.append(h)
    if not hits:
        return 0
    print(FEEDBACK.format(matched=", ".join('"%s"' % h for h in hits[:5])), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
