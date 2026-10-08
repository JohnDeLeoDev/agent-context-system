#!/usr/bin/env python3

'PreToolUse(Bash): block `git commit|push|merge` unless run from a worktree.\n\nEnforces the worktree-landing mandate and "pushes only when explicitly requested".\nwt-finish.sh is unaffected: its git ops are subprocesses inside one bash call, which\nnever trigger this per-tool-call hook. The chezmoi dotfiles repo is user-exempted: it\nis a directly-committed config repo with no worktree workflow. The agent-context store\nhas no exemption: an agent has no sanctioned git write in the store\'s main checkout,\nso it gets the same rule as any project.\n\nCross-project worktrees are allowed: when the tool cwd is one project\'s checkout\nbut the git op targets another project\'s .agents/worktrees/<desc> (or legacy\n.claude/worktrees/<desc>) (via `cd <wt>` or `git -C <wt>`), allowed iff that path\nresolves to a linked worktree.\n\nRemote-branch deletion is allowed from anywhere (changes no source). A repo with\nno commits yet and an explicit user consent token are also allowed.\n\nA git write that runs on another host (inside an `ssh host \'...\'` argument) is\nno write to this checkout and is never blocked. Nor is one in a scratch clone\nunder a temp root.\n\nBookkeeping-only commit/push. The mandate is about project source. A loop\'s own\nledger, state files and markdown are not source (require-worktree-edit exempts\nthem on the Edit side), so this hook must agree, or a loop could write its ledger\nand be unable to commit it. Reads the index for a commit and the commits in\n`origin/<branch>..HEAD` for a push, never the command line, which proves nothing\nabout what is staged or shipped. The push exemption reads the remote tip via\n`ls-remote` when reachable: a stale local tracking ref only ever widens the range\nand re-includes commits already on the remote, which would refuse a ledger-only\npush for a reason that no longer holds.\n\nConsent token. Single-use, time-boxed, logged: the sanctioned escape for a write\nthe user explicitly authorized (for example fast-forwarding one feature branch\ninto another, or a project with no wt-finish.sh). It is the last check: every\nallow branch above it must run first and for free, or a write that was never\ngoing to be blocked would spend the token.\n\nNear-miss notes name the condition that disqualified an exemption\n-- an empty index at commit time (this hook is PreToolUse and runs before\n`git add`), or which non-exempt paths sit in a range being pushed -- so a\nnear miss is not confused with a categorical refusal.\n\n--would-block preflight. `guard-git-write.py --would-block <repo> <command...>`\nanswers "would this be refused, and why" without running anything and without\nminting or spending a token, so an agent can check before asking a human for a\nwrite this hook would allow. Exit 0 = allowed, 2 = would block, reason on stdout.'
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import deploy_memory  
import git_write_token  
import harness_paths as hp  
import store_mcp  

HOME = os.path.expanduser("~")

WRITES = {"commit", "push", "merge"}

TAKES_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
             "--exec-path", "--config-env", "--super-prefix"}
PREFIX = ("sudo", "command", "time", "nohup", "env")


REMOTE = ("ssh", "mosh", "et", "autossh")

FALLBACK_RE = re.compile(
    r"(^|[;&|]\s*)(sudo\s+)?git(\s+-[^;&|]*)?\s+(commit|push|merge)(\s|$)")
COMMIT_RE = re.compile(r"(^|[;&|\s])git(\s+-[^\s]+(\s+[^\s]+)?)*\s+commit(\s|$)")
NOT_PUSH_MERGE_REBASE_RE = re.compile(r"(^|[;&|\s])git[^;&|]*\s(push|merge|rebase)(\s|$)")
PUSH_RE = re.compile(r"(^|[;&|\s])git(\s+-[^\s]+(\s+[^\s]+)?)*\s+push(\s|$)")
PUSH_NOFORCE_EXCLUDE_RE = re.compile(
    r"(--force|--mirror|--all|--tags|--follow-tags|\s-f(\s|$)|--delete|\s:)")
PUSH_FORCE_ONLY_RE = re.compile(r"(--force|--mirror|--all|--tags|--follow-tags|\s-f(\s|$))")
ALLOWED_PATH_RE = re.compile(
    r"(\.md$|^" + re.escape(hp.AGENTS_DIRNAME) + "/|^" + re.escape(hp.CLAUDE_DIRNAME)
    + "/|^" + re.escape(hp.STORE_DIRNAME) + r"/|^parity\.yaml$"
    r"|^parity-history\.jsonl$|^parity-cohort\.txt$|^parity-core\.txt$)")
WT_RE = re.compile(r"/\S*\.(?:agents|claude)/worktrees/[^/\s]+")


def segments(line):
    'Simple commands, split at unquoted ; && || | & and newlines.\n\n    Quoted strings stay whole, so `ssh host "a; git commit"` is one segment whose\n    first word is ssh -- where a naive split cuts inside the quotes and sees a bare\n    `git commit`. Newlines are separators too (a multi-line tool call is a script),\n    but a newline inside quotes -- a heredoc-fed commit message -- stays in its token.\n    Raises ValueError on unbalanced quotes; the caller fails safe.'
    lx = shlex.shlex(line, posix=True, punctuation_chars=";&|\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    segs, cur = [], []
    for tok in lx:
        if tok and all(c in ";&|\n" for c in tok):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs


def inspect(parts):
    '(is_git_write, -C target) for one segment.'
    while parts and parts[0] in PREFIX:
        parts = parts[1:]
    if not parts or parts[0] in REMOTE:
        return False, ""
    if len(parts) < 2 or (parts[0] != "git" and not parts[0].endswith("/git")):
        return False, ""
    i, target = 1, ""
    while i < len(parts) and parts[i].startswith("-"):
        if parts[i] == "-C" and i + 1 < len(parts):
            target = parts[i + 1]
        i += 2 if parts[i] in TAKES_ARG else 1
    return (i < len(parts) and parts[i] in WRITES), target


def absol(p, base):
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base or ".", p))


def remote_branch_delete_verdict(cmd):
    'Is this one plain `git push` whose every refspec is a branch deletion?\n\n    A ref nobody deletes by accident. Matched on the full ref and on its last segment,\n    so neither refs/heads/main nor a stray release/main slips through. Anything that\n    could push a ref disqualifies, and so does anything that acts on refs wholesale.'
    PROTECTED = {"main", "master", "HEAD", "trunk", "develop"}
    FORBIDDEN = {"--mirror", "--all", "--tags", "--follow-tags", "-f", "--force"}

    
    
    if re.search(r"[;&|\n]|\$\(|`", cmd):
        return False
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if parts and parts[0] == "sudo":
        parts = parts[1:]
    if len(parts) < 2 or parts[0] != "git":
        return False
    i = 1
    while i < len(parts) and parts[i].startswith("-"):
        i += 2 if parts[i] in ("-C", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(parts) or parts[i] != "push":
        return False

    args = parts[i + 1:]
    if any(a in FORBIDDEN or a.startswith("--force") or a.startswith("--receive-pack")
           for a in args):
        return False

    positional = [a for a in args if not a.startswith("-")]
    if not positional:
        return False
    refs = positional[1:]
    if not refs:
        return False

    if any(a in ("--delete", "-d") for a in args):
        targets = refs
    elif all(r.startswith(":") for r in refs):
        targets = [r[1:] for r in refs]
    else:
        return False

    for t in targets:
        if not t or t in PROTECTED or t.rsplit("/", 1)[-1] in PROTECTED:
            return False
    return True


def _git_out(repo, *args):
    try:
        r = subprocess.run(["git", "-C", repo, *args],
                            capture_output=True, text=True, timeout=15)
    except Exception:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _git_ok(repo, *args):
    try:
        r = subprocess.run(["git", "-C", repo, *args],
                            capture_output=True, text=True, timeout=15)
    except Exception:
        return False
    return r.returncode == 0


def _lines(s):
    return [ln for ln in s.split("\n") if ln]


def _all_allowed_paths(paths):
    return all(ALLOWED_PATH_RE.search(p) for p in paths)


def _seq_contains(s, *parts):
    'True if `parts` occur, each after the previous, in `s` (glob-order match).'
    pos = 0
    for p in parts:
        idx = s.find(p, pos)
        if idx == -1:
            return False
        pos = idx + len(p)
    return True


def _approval_ask(top):
    return ("If user asked for this push, ask with one AskUserQuestion: header \"Approval\",\n"
            "options \"Approve\" and \"Deny\", question:\n"
            "    Allow one git commit, push or merge in %s within the next 10 minutes? [approval:git-write:%s:10]\n"
            "The approval-question hook mints the token when he picks Approve. Never mint it yourself.\n"
            % (top, top))


def _spend_consent(repo, n, dry_run):
    'Explicit user consent: the message when a live token covers this\n    write, spent unless dry_run, else "". Single-use, time-boxed, logged.'
    token = git_write_token.read()
    if not token:
        return ""
    if git_write_token.expired(token):
        if not dry_run:
            try:
                os.remove(git_write_token.path())  
            except OSError:
                pass
        return ""
    ctop = _git_out(repo, "rev-parse", "--show-toplevel")
    if not git_write_token.covers(token, ctop):
        return ""
    if dry_run:
        return ("guard-git-write: would be allowed by the user consent token now on disk\n"
                "  (this preflight did not consume it -- the real write still will).\n")
    try:
        os.remove(git_write_token.path())
    except OSError:
        pass
    try:
        with open(os.path.join(git_write_token.state_dir(), "git-write-consent.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("%s\t%s\t%s\n" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       ctop or repo, n))
    except OSError:
        pass
    return "guard-git-write: allowed by user consent token (single use, now consumed).\n"


def evaluate(raw_text, dry_run):
    '(allowed: bool, message: str). `message` is the text for stderr.'
    
    
    if not any(w in raw_text for w in ("commit", "push", "merge")):
        return True, ""

    try:
        d = json.loads(raw_text)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    a = d.get("tool_input") or d.get("tool_args") or d.get("params") or {}
    if not isinstance(a, dict):
        a = {}
    raw_command = a.get("command") or d.get("command") or ""
    if not isinstance(raw_command, str):
        raw_command = ""
    cwd = d.get("cwd") or d.get("workdir") or a.get("workdir") or a.get("cwd") or ""

    
    
    state, target = "no", ""
    try:
        eff_cwd = cwd
        for seg in segments(raw_command):
            if seg and seg[0] == "cd" and len(seg) > 1:
                eff_cwd = absol(seg[1], eff_cwd)
                continue
            w, t = inspect(list(seg))
            if w:
                state = "yes"
                target = absol(t, eff_cwd) if t else (eff_cwd if eff_cwd != cwd else "")
                break
            if t and not target:
                target = absol(t, eff_cwd)
    except ValueError:
        state = "unparseable"

    command = raw_command.replace("\n", " ")
    n = re.sub(r"[ \t\n\r\f\v]+", " ", command)

    
    if state == "no":
        return True, ""
    elif state != "yes":
        if not FALLBACK_RE.search(n):
            return True, ""

    cwd_top = _git_out(cwd or ".", "rev-parse", "--show-toplevel")

    
    if False:
        return True, ""
    if False:
        return True, ""

    

    repo = target or cwd or "."
    top = _git_out(repo, "rev-parse", "--show-toplevel")

    
    if ("git push" in command) or _seq_contains(command, "git -C", " push"):
        if "--delete" in command or " :" in command:
            pass  
        else:
            top_or_repo = top or repo
            try:
                hit, unreachable = deploy_memory.find(top_or_repo), ""
            except store_mcp.StoreUnreachable as exc:
                hit, unreachable = None, str(exc)
            if (hit or unreachable) and not os.environ.get("AGENT_GIT_WRITE_CONSENT_OK"):
                spent = _spend_consent(repo, n, dry_run)
                if spent:
                    return True, spent
                if hit:
                    msg = (
                        "BLOCKED by guard-git-write: pushing this repo DEPLOYS it.\n"
                        "\n"
                        "  repo   : %s\n"
                        "  memory : %s (%s)\n"
                        "  says   : %s\n"
                        "Next: read get_memory(\"%s\") and use the landing path it names (usually\n"
                        "wt-finish.sh with an explicit --push).\n"
                    ) % (os.path.basename(top_or_repo), hit["slug"], hit["scope"],
                         hit["description"] or "see the memory body", hit["slug"])
                else:
                    msg = (
                        "BLOCKED by guard-git-write: the store did not answer (%s), so this hook\n"
                        "cannot tell whether pushing %s deploys it (policy).\n"
                        "Next: check the daemon with get_health and retry.\n"
                    ) % (unreachable, top_or_repo)
                return False, msg + _approval_ask(top_or_repo)

    
    
    
    if False:
        return True, ""
    if False:
        return True, ""

    
    
    if top:
        if os.path.isdir(top):
            rp = os.path.realpath(top)
        else:
            rp = top
        for prefix in ("/tmp/", "/private/tmp/", "/var/tmp/", "/private/var/tmp/",
                       "/var/folders/", "/private/var/folders/"):
            if rp.startswith(prefix):
                return True, ""
        tmpdir = os.environ.get("TMPDIR")
        if tmpdir and rp.startswith(tmpdir.rstrip("/") + "/"):
            return True, ""

    
    
    if _git_ok(repo, "rev-parse", "--git-dir") and not _git_ok(repo, "rev-parse", "--verify", "-q", "HEAD"):
        return True, ""

    
    if remote_branch_delete_verdict(command.strip()):
        return True, ""

    
    
    if COMMIT_RE.search(command) and not NOT_PUSH_MERGE_REBASE_RE.search(command):
        staged = _lines(_git_out(repo, "diff", "--cached", "--name-only"))
        if staged and _all_allowed_paths(staged):
            msg = ("guard-git-write: bookkeeping-only commit (ledger/state/docs, no source) -- allowed.\n"
                   "  staged: %s\n" % " ".join(staged))
            return True, msg

    
    
    
    
    if PUSH_RE.search(command) and not PUSH_NOFORCE_EXCLUDE_RE.search(command):
        head_branch = _git_out(repo, "symbolic-ref", "--short", "HEAD")
        if head_branch in ("main", "master"):
            upstream = "origin/%s" % head_branch
            ls = _git_out(repo, "ls-remote", "--exit-code", "origin", "refs/heads/%s" % head_branch)
            remote_tip = ls.split("\n")[0].split("\t")[0].strip() if ls else ""
            if remote_tip and _git_ok(repo, "cat-file", "-e", "%s^{commit}" % remote_tip):
                upstream = remote_tip
            if _git_ok(repo, "rev-parse", "--verify", "-q", upstream):
                ahead_s = _git_out(repo, "rev-list", "--count", "%s..HEAD" % upstream)
                try:
                    ahead = int(ahead_s) if ahead_s else 0
                except ValueError:
                    ahead = 0
                if ahead > 0:
                    touched = _lines(_git_out(repo, "diff", "--name-only", "%s..HEAD" % upstream))
                    if touched and _all_allowed_paths(touched):
                        msg = ("guard-git-write: bookkeeping-only push of '%s' (%d commit(s), no source)"
                               " -- allowed.\n  files: %s\n" % (head_branch, ahead, " ".join(touched)))
                        return True, msg

    gitdir = _git_out(repo, "rev-parse", "--git-dir")
    if "/worktrees/" in (repo + gitdir):
        return True, ""

    
    
    wt_match = WT_RE.search(command)
    if wt_match:
        wt = wt_match.group(0)
        egit = _git_out(wt, "rev-parse", "--git-dir")
        if "/worktrees/" in egit:
            return True, ""

    
    
    
    
    spent = _spend_consent(repo, n, dry_run)
    if spent:
        return True, spent

    
    
    notes = []
    if COMMIT_RE.search(command):
        if not _git_out(repo, "diff", "--cached", "--name-only"):
            notes.append(
                "NOTE: the index is empty as this hook runs, so the bookkeeping-only exemption could not\n"
                "apply. This hook is PreToolUse -- it reads the index before your command runs, so\n"
                "`git add <paths> && git commit ...` can never qualify: at inspection time nothing is\n"
                "staged. Stage in its own tool call, then issue a bare `git commit` as the next one.\n")
    if PUSH_RE.search(command):
        hb = _git_out(repo, "symbolic-ref", "--short", "HEAD")
        if hb in ("main", "master"):
            if PUSH_FORCE_ONLY_RE.search(command):
                notes.append(
                    "NOTE: this push carries --force/--mirror/--all/--tags, which the bookkeeping-only\n"
                    "  exemption never covers, whatever the commits touch.\n")
            else:
                src = [p for p in _lines(_git_out(repo, "diff", "--name-only", "origin/%s..HEAD" % hb))
                       if not ALLOWED_PATH_RE.search(p)][:5]
                if src:
                    notes.append(
                        "NOTE: the bookkeeping-only exemption did not apply because these non-exempt paths\n"
                        "  are in the range being pushed: %s\n"
                        "  Those are project source and must reach the branch through a worktree landing.\n"
                        % " ".join(src))

    blocked = (
        "BLOCKED by guard-git-write hook: git commit/push/merge from the main checkout.\n"
        "Project source changes must be made in a git worktree and landed via wt-finish.sh:\n"
        "  git worktree add .agents/worktrees/<desc> -b <desc>   (then edit + commit there)\n"
        "For the agent-context store the same holds, with its own lander -- from outside the\n"
        "worktree, after a signed commit inside it:\n"
        "  python3 ~/.agent-context/global/scripts/store-wt-finish.py ~/.agent-context/.agents/worktrees/<desc>\n"
        "  python3 ~/.agent-context/global/scripts/store-landed.py <sha>    # landed is not shipped\n"
        "Entities (hooks, scripts, docs, memory) are never committed by hand: the MCP tools\n"
        "write them and the daemon commits them.\n"
        "Deleting a remote branch is allowed from here (it changes no source):\n"
        "  git push origin --delete <branch>      git push origin :<branch>\n"
        "main/master and any ref-updating push stay blocked.\n"
        "\n"
        "If the user explicitly asked for this write (e.g. landing one feature branch into\n"
        "another, or a project with no wt-finish.sh), they can authorize it. Ask with one\n"
        "AskUserQuestion: header \"Approval\", options \"Approve\" and \"Deny\", question:\n"
        "  Allow one git commit, push or merge in <repo top level> within the next <1-120> minutes? [approval:git-write:<repo top level>:<minutes>]\n"
        "The approval-question hook mints a single-use token when he picks Approve. Never mint\n"
        "it yourself: it is the user's authorization, not yours to grant.\n"
    )
    return False, "".join(notes) + blocked


def main(argv):
    args = argv[1:]
    if args and args[0] == "--would-block":
        rest = args[1:]
        pre_repo = rest[0] if rest else os.getcwd()
        pre_cmd = " ".join(rest[1:]) if len(rest) > 1 else ""
        payload = json.dumps({"tool_name": "Bash", "cwd": pre_repo,
                               "tool_input": {"command": pre_cmd}})
        allowed, out = evaluate(payload, dry_run=True)
        if allowed:
            print("allowed: guard-git-write would not block this. No consent token needed.")
            if out.strip():
                print(out.strip())
            return 0
        print("would block:")
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
        return 2

    raw_text = sys.stdin.read()
    dry_run = bool(os.environ.get("GUARD_GIT_WRITE_DRY_RUN"))
    allowed, msg = evaluate(raw_text, dry_run)
    if msg:
        sys.stderr.write(msg)
    return 0 if allowed else 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  
        sys.stderr.write("guard-git-write: internal error, failing closed: %r\n" % (exc,))
        sys.exit(2)
