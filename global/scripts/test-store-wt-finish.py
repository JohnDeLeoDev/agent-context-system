#!/usr/bin/env python3
'test-store-wt-finish.py: the two shapes store-wt-finish.py has to land.\n\nRun: SCRIPT=~/.agent-context/global/scripts/store-wt-finish.py python3 ~/.agent-context/global/scripts/test-store-wt-finish.py'

import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT = os.environ.get("SCRIPT") or os.path.join(
    os.environ.get("HOME", ""), ".agent-context", "global", "scripts", "store-wt-finish.py")

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def g(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)


def g_ok(repo, *args):
    proc = g(repo, *args)
    if proc.returncode != 0:
        sys.exit("fixture setup failed: git -C %s %s: %s" % (repo, " ".join(args), proc.stderr))
    return proc.stdout


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def signing_config(root):
    'Fixture commits are signed for real with a throwaway key: no unsigned commit, ever.'
    key = os.path.join(root, "fixture-signing-key")
    if not os.path.exists(key):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
        os.chmod(key, 0o600)  
    return [("gpg.format", "ssh"), ("gpg.ssh.program", "ssh-keygen"),
            ("user.signingkey", key), ("commit.gpgsign", "true")]


def setup(root):
    'Builds root/main (a store-like checkout) with an `ls` remote.'
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root)
    main = os.path.join(root, "main")
    lsgit = os.path.join(root, "ls.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", lsgit], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", main], check=True)
    for k, v in [("user.email", "t@example.com"), ("user.name", "t")] + signing_config(root):
        g_ok(main, "config", k, v)
    os.makedirs(os.path.join(main, "global"))
    write(os.path.join(main, "global", "note.md"), "seed\n")
    g_ok(main, "add", "-A")
    g_ok(main, "commit", "-q", "-m", "seed")
    g_ok(main, "remote", "add", "ls", lsgit)
    g_ok(main, "push", "-q", "ls", "main")
    return main, lsgit


def run_script(wt, main):
    env = dict(os.environ)
    env["AGENT_CONTEXT_STORE"] = main
    proc = subprocess.run([sys.executable, SCRIPT, wt], capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout + proc.stderr


ROOT = tempfile.mkdtemp(prefix="store-wt-finish-test.")

try:
    
    main, _lsgit = setup(ROOT)
    wt1 = os.path.join(ROOT, "wt1")
    g_ok(main, "worktree", "add", "-q", wt1, "-b", "feature")
    write(os.path.join(wt1, "global", "feature.md"), "a feature\n")
    g_ok(wt1, "add", "-A")
    g_ok(wt1, "commit", "-q", "-m", "a feature")

    rc, out = run_script(wt1, main)
    check("a feature branch lands", rc == 0, "exit %d: %s" % (rc, out))
    merges = g(main, "rev-list", "--count", "--merges", "HEAD").stdout.strip()
    check("a feature branch lands linearly", merges == "0", "merge count is %r" % merges)
    check("the file arrives on main", os.path.isfile(os.path.join(main, "global", "feature.md")))

    
    main, lsgit = setup(ROOT)
    
    other = os.path.join(ROOT, "other")
    subprocess.run(["git", "clone", "-q", lsgit, other], check=True)
    for k, v in [("user.email", "o@example.com"), ("user.name", "o")] + signing_config(ROOT):
        g_ok(other, "config", k, v)
    write(os.path.join(other, "global", "note.md"), "seed\ntheirs\n")
    g_ok(other, "add", "-A")
    g_ok(other, "commit", "-q", "-m", "their edit")
    g_ok(other, "push", "-q", "origin", "main")
    their_sha = g_ok(other, "rev-parse", "HEAD").strip()

    
    write(os.path.join(main, "global", "note.md"), "seed\nmine\n")
    g_ok(main, "add", "-A")
    g_ok(main, "commit", "-q", "-m", "my edit")
    g_ok(main, "fetch", "-q", "ls")
    wt2 = os.path.join(ROOT, "wt2")
    g_ok(main, "worktree", "add", "-q", wt2, "-b", "merge-ls")
    g(wt2, "merge", "--no-edit", "ls/main")  
    write(os.path.join(wt2, "global", "note.md"), "seed\nmine\ntheirs\n")
    g_ok(wt2, "add", "-A")
    g_ok(wt2, "commit", "-q", "--no-edit")

    rc, out = run_script(wt2, main)
    check("a reconciling branch lands", rc == 0, "exit %d: %s" % (rc, out))
    ancestor = g(main, "merge-base", "--is-ancestor", their_sha, "HEAD")
    check("the other machine's tip is an ancestor of main", ancestor.returncode == 0,
          "it is not, so the daemon conflicts again next cycle (policy)")
    check("the landing names its shape", "landing by MERGE" in out,
          "no MERGE line in the output: %s" % out)
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
