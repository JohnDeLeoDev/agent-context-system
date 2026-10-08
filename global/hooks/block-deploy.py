#!/usr/bin/env python3

"block-deploy: PreToolUse(Bash): hard-block running deploy/publish/reset scripts\nunless the deploy was authorized this turn via a sanctioned pathway:\n\nAn agent deciding on its own to run deploy.sh/full-upgrade against the live server is\nblocked. Authorization is derived from the session transcript; if it can't be read\nthe hook fails closed (block). Deploying, publishing and resetting stay user/CI\nterritory by default.\n\nFast path: the deploy-family script names all carry one of a few words, matched\ncase-insensitively against the raw stdin -- so a payload with none of them cannot be a\ndeploy. Runs on every Bash call; skips the rest of this hook for most of them.\n\nVerify-script exemption. A read-only checker such as verify-deployment.sh only probes\nendpoints, so it must never be blocked: a guard that fires on honest work is one people\nlearn to route around. The rule is the basename prefix (verify-/check-/probe-/test-),\nanchored right after the last `/` so a deploy under a directory called `verify/` is\nuntouched. A deploy script named `verify-and-deploy.sh` would slip through; that was\nweighed against a hand-maintained name list, which blocks every new read-only checker\nuntil someone adds it.\n\n`bash -n <script>` parses without executing a line of it. Blocking a syntax check\nteaches the workaround of copying a deploy script elsewhere to run `bash -n` on the\ncopy. Neutralized per statement, so a deploy chained after one still matches.\n\nSkill pathway. An assistant `Skill` tool_use on its own, or a `<command-name>` in a\nharness-injected user-role message, is no proof the user asked: no skill in the store\nsets `disable-model-invocation`, so the model can select /full-reset-dev itself. Both\nbranches require corroboration from a user-authored message in the transcript window."
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import deploy_memory  
import git_write_token  
import store_mcp  

FAST_WORDS = ("deploy", "full-upgrade", "full-reset", "reset-server", "push")

VERIFY_DEPLOYMENT_RE = re.compile(
    r'(?:bash[ \t]+|sh[ \t]+|\./)[^;&|]*verify-deployment\.sh')
VERIFY_PREFIX_RE = re.compile(
    r'(?:bash[ \t]+|sh[ \t]+|\./)(?:[^;&|\s]*/)?(?:verify|check|probe|test)-[^;&|\s]*\.sh')
BASH_N_RE = re.compile(
    r'(^|[;&|])([ \t]*)(?:sudo[ \t]+)?(?:bash|sh)[ \t]+-n[ \t]+[^;&|]*')
DEPLOY_SCRIPT_RE = re.compile(
    r'(?:^|[;&|][ \t]*)(?:sudo[ \t]+)?(?:bash[ \t]+|sh[ \t]+|\./)(?:[^;&|\s]*/)?'
    r'[a-z0-9_.-]*(?:deploy|full-upgrade|full-reset|reset-server|deploy-?secrets)'
    r'[a-z0-9_-]*\.sh', re.I)
GIT_PUSH_RE = re.compile(
    r'(?:^|[;&|][ \t]*)(?:sudo[ \t]+)?git(?:[ \t]+-\S+)*[ \t]+push\b')
GIT_PUSH_EXEMPT_RE = re.compile(
    r'push[^;&|]*(?:--delete|--mirror[^;&|]*--prune|[ \t]:[a-zA-Z0-9_/.\-]+)')


def normalize(command):
    n = command.replace("\n", ";")
    n = re.sub(r'[ \t]+', ' ', n)
    return n


def neutralize(n):
    n = VERIFY_DEPLOYMENT_RE.sub('__verify_only__', n)
    n = VERIFY_PREFIX_RE.sub('__verify_only__', n)
    n = BASH_N_RE.sub(r'\1\2__parse_only__', n)
    return n


def find_deploy_memory(data):
    '(repo top level, deploy memory or None, why the store could not answer or "").\n\n    The lookup is deploy_memory.find, over MCP, shared with guard-git-write (policy).'
    cwd = data.get("cwd") or os.getcwd()
    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    m = re.search(r"-C\s+(\S+)", cmd)
    if m:
        cwd = os.path.expanduser(m.group(1).strip("'\""))
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return "", None, ""
    if not top:
        return "", None, ""
    try:
        return top, deploy_memory.find(top), ""
    except store_mcp.StoreUnreachable as exc:
        return top, None, str(exc)



DEPLOY_NAME_RE = re.compile(
    r'(?:full-upgrade|full-reset)(?:-(?:dev|test|prod|all))?|ralph-deploy-audit-(?:dev|test)',
    re.I)

INTENT_RE = re.compile(
    r'\b(?:deploy|redeploy|deploys|deploying|deploysecrets|full[-\s]?upgrade|'
    r'full[-\s]?reset|reset[-\s]?server|reset\s+the\s+server|publish|ship\s+it|'
    r'push\s+to\s+(?:dev|test|prod|production))\b', re.I)



AFFIRM_RE = re.compile(
    r"^\W*(?:yes(?:\s+please)?|yep|yeah|yup|ok|okay|sure|please\s+do|do\s+it|do\s+that|"
    r"run\s+it|run\s+that|go|go\s+ahead|go\s+for\s+it|proceed|send\s+it|affirmative|"
    r"confirmed|approved|sounds\s+good|lgtm|make\s+it\s+so)\W*$", re.I)


_PROPOSE = (r"(?:shall\s+i|shall\s+we|should\s+i|should\s+we|do\s+you\s+want\s+me\s+to|"
            r"want\s+me\s+to|would\s+you\s+like\s+me\s+to|can\s+i|may\s+i|ready\s+to|"
            r"ok(?:ay)?\s+to|say\s+the\s+word|say\s+when|let\s+me\s+know|i\s+can|i'?ll|"
            r"i\s+will|about\s+to|propose)")


_DEPLOY_W = (r"(?:(?:deploy|redeploy|deploying)(?!\s+(?:logs?|output|script|journal|"
             r"history|notes?|records?|steps?|docs?|documentation))"
             r"|deploysecrets|full[-\s]?upgrade|full[-\s]?reset|reset[-\s]?server|"
             r"publish|push\s+to\s+(?:dev|test|prod|production))")




_GAP = r"(?:[^.?!\n]|[.?!](?=\S))"
PROPOSAL_RE = re.compile(
    _PROPOSE + _GAP + r"{0,80}?" + _DEPLOY_W
    + r"|" + _DEPLOY_W + _GAP + r"{0,80}?" + _PROPOSE
    + r"|(?:^|[.?!\n]\s*)" + _DEPLOY_W + _GAP + r"{0,80}\?", re.I)

SYSREM_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S | re.I)


SENTINEL = "The user sent a new message while you were working:"
BOILERPLATE = "This is how Claude Code surfaces"


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", "") or "")
    return "\n".join(out)


def is_real_user_prompt(msg):
    if msg.get("role") != "user":
        return False
    c = msg.get("content")
    if isinstance(c, str):
        return c.strip() != ""
    if isinstance(c, list):
        has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in c)
        only_tr = len(c) > 0 and all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
        return has_text and not only_tr
    return False


def load_messages(transcript_path):
    msgs = []
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            m = e.get("message") if isinstance(e, dict) else None
            if isinstance(m, dict) and m.get("role"):
                msgs.append(m)
            elif isinstance(e, dict) and e.get("role"):
                msgs.append(e)
    return msgs


def user_authored_items(win, offset):
    'User-authored text within this turn, paired with its index in msgs.\n\n    Text blocks of user prompts, plus mid-turn messages the harness injected\n    (identified by SENTINEL, wherever they land -- a text or a tool_result block).\n    Tool output is never trusted unless it carries that sentinel, so tool text that\n    merely mentions "deploy" cannot self-authorize a deploy. The index is what lets\n    pathway A2 find the assistant message a bare affirmative is answering.'
    items = []
    for i, m in enumerate(win):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            blocks = [{"type": "text", "text": c}]
        elif isinstance(c, list):
            blocks = c
        else:
            blocks = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = None
            if b.get("type") == "text":
                t = b.get("text", "") or ""
            elif b.get("type") == "tool_result":
                raw = b.get("content")
                rt = raw if isinstance(raw, str) else text_of(raw)
                if rt and SENTINEL in rt:
                    t = rt
            if not t:
                continue
            if SENTINEL in t:
                seg = t.split(SENTINEL, 1)[1]
                seg = seg.split(BOILERPLATE, 1)[0]
                items.append((offset + i, seg))
            else:
                items.append((offset + i, t))
    return items


def last_assistant_text_before(msgs, idx):
    'The last thing the agent said before this user message.'
    for j in range(idx - 1, -1, -1):
        m = msgs[j]
        if m.get("role") != "assistant":
            continue
        t = text_of(m.get("content")).strip()
        if t:
            return t
    return ""


def compute_decision(data):
    '"allow" or "block", derived from the session transcript. Fails closed.'
    tp = data.get("transcript_path", "")
    if not tp or not os.path.exists(tp):
        return "block"

    try:
        msgs = load_messages(tp)
    except OSError:
        return "block"

    start = 0
    for i in range(len(msgs) - 1, -1, -1):
        if is_real_user_prompt(msgs[i]):
            start = i
            break
    window = msgs[start:]

    items = user_authored_items(window, start)

    
    
    for _idx, pt in items:
        if INTENT_RE.search(pt):
            return "allow"
        if ("<command-name>" in pt or "/full-" in pt or "/ralph-deploy-audit" in pt) \
                and DEPLOY_NAME_RE.search(pt):
            return "allow"

    
    
    
    
    for idx, pt in items:
        if not AFFIRM_RE.match(SYSREM_RE.sub(" ", pt).strip()):
            continue
        if PROPOSAL_RE.search(last_assistant_text_before(msgs, idx)):
            return "allow"

    
    
    user_named_deploy = any(DEPLOY_NAME_RE.search(pt) for _i, pt in items)

    for m in window:
        if not user_named_deploy:
            break
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "Skill":
                    inp = b.get("input") or {}
                    blob = " ".join(str(v) for v in inp.values()
                                    if isinstance(v, (str, int, float)))
                    if DEPLOY_NAME_RE.search(blob):
                        return "allow"
        if m.get("role") == "user":
            t = text_of(c)
            if "<command-name>" in t and DEPLOY_NAME_RE.search(t):
                return "allow"

    return "block"


STANDARD_BLOCK_MESSAGE = """BLOCKED by block-deploy hook: deploy/publish/reset needs the user's authorization this turn.
Any one lifts it: the user asked to deploy this turn (mid-turn messages count); the user
answered a deploy you proposed with a bare "yes"/"go ahead"; the user invoked
/full-upgrade[-*], /full-reset[-*] or /ralph-deploy-audit-{dev,test}. Selecting the skill yourself does not count.
Otherwise ask the user to run it (`! <cmd>` or the skill), or hand them the command.
A read-only script caught by its name: do not rename it; run its commands directly, and say the guard fired.
"""


def main():
    stdin_raw = sys.stdin.read()

    lower = stdin_raw.lower()
    if not any(w in lower for w in FAST_WORDS):
        return 0

    try:
        data = json.loads(stdin_raw)
    except ValueError:
        data = {}

    command = (data.get("tool_input") or {}).get("command", "") or ""
    n = normalize(command)
    n_check = neutralize(n)

    memory, unreachable = None, ""
    if not DEPLOY_SCRIPT_RE.search(n_check):
        if GIT_PUSH_RE.search(n_check) and not GIT_PUSH_EXEMPT_RE.search(n_check):
            top, memory, unreachable = find_deploy_memory(data)
            
            
            if (memory or unreachable) and git_write_token.live_for(top):
                return 0
        if not memory and not unreachable:
            return 0

    decision = compute_decision(data)
    if decision == "allow":
        return 0

    if memory:
        sys.stderr.write(
            "BLOCKED by block-deploy hook: this push is a deploy; the remote rebuilds the live site.\n"
            "Memory %s: %s\n"
            "Next: read get_memory(\"%s\") and use the landing path it names (often wt-finish.sh),\n"
            "or hand user the command. The pathways below apply, and so does his git-write approval.\n\n"
            % (memory["slug"], memory["description"], memory["slug"]))
    elif unreachable:
        sys.stderr.write(
            "BLOCKED by block-deploy hook: the store did not answer (%s), so this hook cannot tell\n"
            "whether this push deploys (policy). Next: check the daemon with get_health and retry.\n"
            "The pathways below apply, and so does user's git-write approval for this repo.\n\n"
            % unreachable)

    sys.stderr.write(STANDARD_BLOCK_MESSAGE)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                  
        sys.stderr.write(STANDARD_BLOCK_MESSAGE)
        sys.exit(2)
