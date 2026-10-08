#!/usr/bin/env python3
'Test battery for guard-git-write, focused on the USER CONSENT TOKEN.\n\nEvery case therefore asserts two things: the verdict, AND whether the token survived. A\nguard that allows the right things while quietly burning the token is still broken, and\nno verdict-only test would see it.\n\nFixture lives under ~/.cache (NOT a temp root): guard-git-write deliberately exempts\nrepos under /tmp, $TMPDIR and /var/folders as throwaway clones, so a mktemp fixture\nwould be exempt from the very policy under test.'
import importlib.util
import json
import os
import shutil
import subprocess
import sys

HOOKS_DIR = os.environ.get("HOOKS_DIR") or os.path.expanduser("~/.agent-context/global/hooks")
HOOK = os.path.join(HOOKS_DIR, "guard-git-write.py")
if not os.path.isfile(HOOK):
    print("missing or not executable: %s" % HOOK)
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store_mcp  

_spec = importlib.util.spec_from_file_location("test_store_mcp", os.path.join(HERE, "test-store-mcp.py"))
assert _spec is not None and _spec.loader is not None
fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake)

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, ".cache", "agent-context", "guard-git-test.%d" % os.getpid())
R = os.path.join(ROOT, "repo")
STATE = os.path.join(ROOT, "state")
CONSENT = os.path.join(STATE, "agent-context", "git-write-consent")
shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(R, exist_ok=True)
os.makedirs(os.path.join(STATE, "agent-context"), exist_ok=True)


def git(*args, cwd=R, check=True):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True,
                           check=check)


git("init", "-q", "-b", "main", ".")
git("config", "user.email", "t@t")
git("config", "user.name", "t")
with open(os.path.join(R, "src.txt"), "w") as fh:
    fh.write("x\n")
with open(os.path.join(R, "NOTES.md"), "w") as fh:
    fh.write("note\n")
os.makedirs(os.path.join(R, ".agents"), exist_ok=True)
with open(os.path.join(R, ".agents", "LEDGER.md"), "w") as fh:
    fh.write("led\n")
git("add", "-A")
git("commit", "-qm", "init")
subprocess.run(["git", "worktree", "add", "-q", os.path.join(R, ".claude/worktrees/wip"), "-b", "wip"],
               cwd=R, capture_output=True, text=True)

failed = 0


def mint(scope=None):
    os.makedirs(os.path.dirname(CONSENT), exist_ok=True)
    lines = ["expires=%d" % (__import__("time").time() + 600)]
    if scope:
        lines.append("scope=%s" % scope)
    with open(CONSENT, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def run(want, wanttok, wd, cmd):
    global failed
    payload = json.dumps({"tool_name": "Bash", "cwd": wd, "tool_input": {"command": cmd}})
    env = {**os.environ, "XDG_STATE_HOME": STATE}
    proc = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True,
                           text=True, env=env)
    got = "ALLOW" if proc.returncode == 0 else "BLOCK"
    tok = "kept" if os.path.exists(CONSENT) else "spent"
    if wanttok == "none":
        tok = "none"
    if got == want and tok == wanttok:
        print("  ok   %-5s token=%-5s %s" % (got, tok, cmd[:52]))
    else:
        print("  FAIL want=%s/%s got=%s/%s :: %s" % (want, wanttok, got, tok, cmd[:52]))
        failed = 1
    try:
        os.remove(CONSENT)
    except OSError:
        pass


print("--- policy: an allowed write must NEVER spend the token ---")
mint(); run("ALLOW", "kept", os.path.join(R, ".claude/worktrees/wip"), "git commit --amend --no-edit")
mint(); run("ALLOW", "kept", os.path.join(R, ".claude/worktrees/wip"), "git commit -m x")
mint(); run("ALLOW", "kept", R, "git push origin --delete somebranch")

git("add", ".agents/LEDGER.md", check=False)
with open(os.path.join(R, ".agents", "LEDGER.md"), "a") as fh:
    fh.write("more\n")
git("add", ".agents/LEDGER.md")
mint(); run("ALLOW", "kept", R, "git commit -m ledger")
git("reset", "-q")

print("--- the token IS spent on a write that would otherwise be refused ---")
git("add", "src.txt", check=False)
with open(os.path.join(R, "src.txt"), "a") as fh:
    fh.write("y\n")
git("add", "src.txt")
mint(); run("ALLOW", "spent", R, "git commit -m source")

print("--- and without a token that same write is refused ---")
run("BLOCK", "none", R, "git commit -m source")
run("BLOCK", "none", R, "git push origin main")

print("--- a token that does not apply is neither used nor honored ---")
mint("/some/other/repo"); run("BLOCK", "kept", R, "git commit -m source")

os.makedirs(os.path.dirname(CONSENT), exist_ok=True)
with open(CONSENT, "w") as fh:
    fh.write("expires=1\n")
run("BLOCK", "spent", R, "git commit -m source")
git("reset", "-q")




















MARKER = "0b8f6c1e-2d3a-4b5c-9d8e-7f6a5b4c3d2e"
DEPLOY_PROJECT = {"display_name": "fakesite.dev", "workspace": None,
                  "canonical_remote": "example.invalid:team/fakesite.dev"}
DEPLOY_SLUG = "project_fakesite_dev_deploy"
DEPLOY_DESCRIPTION = "fakesite.dev: a push here IS the deploy."
DEPLOY_BODY = (
    'fakesite.dev is a Next.js site behind an apex domain. Deploy is push-to-deploy:\n'
    'a push fires the post-receive hook, which rebuilds the app and restarts it.\n')


def store_reply(rid, params):
    'The three tools deploy_memory.find calls. resolve_project knows one project, by its\n    marker id, which is how a renamed checkout is still recognized.'
    tool, args = params.get("name"), params.get("arguments") or {}
    evidence = (params.get("_meta") or {}).get(store_mcp.EVIDENCE_META_KEY) or {}
    value, is_error = None, False
    if tool == "resolve_project":
        if evidence.get("marker_id") == MARKER:
            value = DEPLOY_PROJECT
        else:
            value, is_error = "No project found for path", True
    elif tool == "search_all":
        value = [{"slug": DEPLOY_SLUG, "description": DEPLOY_DESCRIPTION}]
    elif tool == "get_memory":
        value = {"slug": args.get("slug"), "body": DEPLOY_BODY if args.get("slug") == DEPLOY_SLUG else ""}
    else:
        value, is_error = "unexpected tool %s" % tool, True
    text = value if isinstance(value, str) else json.dumps(value)
    return fake.RPCReply(message={"jsonrpc": "2.0", "id": rid, "result": fake.text_result(text, is_error)})


SERVER = fake.serve(store_reply)

STORE_ENV = {k: v for k, v in fake.env_for(SERVER).items() if k != "HOME"}
STORE_ENV["AGENT_CONTEXT_ENV_FILE"] = os.path.join(ROOT, "no-env-file")





for d in ("fakesite.dev", "plainrepo", "site", "app", "renamed-checkout"):
    dpath = os.path.join(ROOT, d)
    os.makedirs(dpath, exist_ok=True)
    git("init", "-q", "-b", "main", ".", cwd=dpath)
    git("config", "user.email", "t@t", cwd=dpath)
    git("config", "user.name", "t", cwd=dpath)
    with open(os.path.join(dpath, "src.txt"), "w") as fh:
        fh.write("x\n")
    git("add", "-A", cwd=dpath)
    git("commit", "-qm", "init", cwd=dpath)
    
    git("remote", "add", "origin", "ssh://git@example.invalid/team/%s.git" % d, cwd=dpath)


def says(label, wd, cmd, mode, pat):
    global failed
    payload = json.dumps({"tool_name": "Bash", "cwd": wd, "tool_input": {"command": cmd}})
    env = {**os.environ, "XDG_STATE_HOME": STATE, **STORE_ENV}
    proc = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True,
                           text=True, env=env)
    out = (proc.stdout or "") + (proc.stderr or "")
    hit = "present" if pat in out else "absent"
    if hit == mode:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s (wanted %s, got %s)" % (label, mode, hit))
        failed = 1


print("--- policy: a push that DEPLOYS is refused, and says why ---")
says("deploy repo names the deploy", os.path.join(ROOT, "fakesite.dev"), "git push origin main",
     "present", "DEPLOYS it")
says("deploy repo names the memory", os.path.join(ROOT, "fakesite.dev"), "git push origin main",
     "present", "project_fakesite_dev_deploy")
os.makedirs(os.path.join(ROOT, "renamed-checkout", ".agents"), exist_ok=True)
with open(os.path.join(ROOT, "renamed-checkout", ".agents", "project-id"), "w") as fh:
    fh.write('id = "%s"\n' % MARKER)
says("a renamed checkout of the deploy repo is still a deploy",
     os.path.join(ROOT, "renamed-checkout"), "git push origin main", "present", "DEPLOYS it")
print("--- and the gate stays quiet everywhere else ---")
says("ordinary repo is not a deploy", os.path.join(ROOT, "plainrepo"), "git push origin main",
     "absent", "DEPLOYS it")

says("a repo named site is not example.invalid", os.path.join(ROOT, "site"), "git push origin main",
     "absent", "DEPLOYS it")
says("a repo named app is not example.invalid", os.path.join(ROOT, "app"), "git push origin main",
     "absent", "DEPLOYS it")
says("no cwd is not a deploy", "", "git push origin main", "absent", "DEPLOYS it")
says("deleting a remote branch deploys nothing", os.path.join(ROOT, "fakesite.dev"),
     "git push origin --delete old", "absent", "DEPLOYS it")

print("--- policy: a store that does not answer blocks the push ---")
fake.stop(SERVER)
says("unreachable store is refused, and says so", os.path.join(ROOT, "plainrepo"),
     "git push origin main", "present", "the store did not answer")
says("a repo with no remote needs no lookup", R, "git push origin main", "absent",
     "the store did not answer")

print("--- reads and non-git commands are never touched ---")
run("ALLOW", "none", R, "git log --oneline --grep=commit")
run("ALLOW", "none", R, "echo commit")

print()
print("ALL PASS" if failed == 0 else "SOME FAILED")
shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(failed)
