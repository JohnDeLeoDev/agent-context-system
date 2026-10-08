#!/usr/bin/env python3
"Found while landing C11 prerequisite (a): a lock taken from a store worktree went to a\nper-host manifest, so it never reached `main`. A store worktree is a git worktree whose\ncommon git dir is the store's `.git`, whatever directory it lives in. A lock taken there\nis a record in that worktree's own global/test-locks/, lands with the branch, and binds\nedits inside the worktree.\n\nThe lock tool under test comes from TEST_LOCK_TOOL_UNDER_TEST (default: the store's).\nThe hooks and test-lock-consent.py read it through TEST_LOCK_TOOL.\n\nRun: python3 test-test-lock-worktree-records.py"
import datetime
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REAL_STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
SCRIPTS = os.path.join(REAL_STORE, "global", "scripts")
HOOKS = os.path.join(REAL_STORE, "global", "hooks")
LOCK_TOOL = os.environ.get("TEST_LOCK_TOOL_UNDER_TEST") or os.path.join(SCRIPTS, "test-lock.py")
CONSENT = os.path.join(SCRIPTS, "test-lock-consent.py")
EDIT_HOOK = os.environ.get("BATTERY_EDIT_HOOK") or os.path.join(HOOKS, "block-locked-test-edit.py")
GATE_HOOK = os.path.join(HOOKS, "locked-test-drift-gate.py")

TMP = os.path.realpath(tempfile.mkdtemp(prefix="test-lock-wt-"))
SIGN_KEY = os.path.join(TMP, "signkey")
STORE = os.path.join(TMP, "store")
WT_AGENTS = os.path.join(STORE, ".agents", "worktrees", "wa")
WT_AGENTS2 = os.path.join(STORE, ".agents", "worktrees", "wa2")
WT_CLAUDE = os.path.join(STORE, ".claude", "worktrees", "wc")
WT_ELSEWHERE = os.path.join(TMP, "elsewhere-wt")
ORIGINAL = "def test_x():\n    assert 0\n"
DRIFTED = "def test_x():\n    assert 1\n"
T1 = "global/scripts/test-w.py"
T2 = "global/scripts/test-w2.py"
T_US = "global/scripts/test_a.py"
T_SL = "global/scripts/test/a.py"
TRACKED = (T1, T2, T_US, T_SL)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:700]
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def env():
    e = dict(os.environ)
    e.update({
        "TEST_LOCK_STATE_DIR": os.path.join(TMP, "host", "locks"),
        "XDG_STATE_HOME": os.path.join(TMP, "host", "state"),
        "TEST_LOCK_STORE_ROOT": STORE,
        "AGENT_CONTEXT_STORE": STORE,
        "TEST_LOCK_TOOL": LOCK_TOOL,
        "SHELL_COMMAND_SCAN": os.path.join(SCRIPTS, "shell-command-scan.py"),
        "LOCKED_TEST_GATE_STATE_DIR": os.path.join(TMP, "host", "gate"),
        "VERIFICATION_CLAIM_JUDGE": os.path.join(SCRIPTS, "verification-claim-judge.py"),
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    return e


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True,
                          check=True, env=env())


def run(argv, cwd=None, stdin=None):
    return subprocess.run([sys.executable] + argv, input=stdin, capture_output=True,
                          text=True, env=env(), cwd=cwd or TMP, timeout=120)


def lock(*files, cwd=None):
    return run([LOCK_TOOL, "lock"] + list(files), cwd=cwd)


def status(path):
    return run([LOCK_TOOL, "status", path])


def hook(path, payload):
    return run([path], stdin=json.dumps(payload))


def write_payload(cwd, target, content=DRIFTED):
    return {"tool_name": "Write", "cwd": cwd,
            "tool_input": {"file_path": target, "content": content}}


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def expected_name(rel):
    return rel.replace("_", "_u").replace("/", "__") + ".json"


def rec_dir(root):
    return os.path.join(root, "global", "test-locks")


def records(root):
    out = {}
    for p in sorted(glob.glob(os.path.join(rec_dir(root), "*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
            out[os.path.basename(p)] = doc
        except (OSError, ValueError):
            out[os.path.basename(p)] = None
    return out


def host_manifests():
    d = os.path.join(TMP, "host", "locks")
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            pass
    return out


def tool_call(expr):
    'Evaluate `expr` with the lock tool loaded as `tl`, print its JSON.'
    code = ("import importlib.util, json, os, sys\n"
            "spec = importlib.util.spec_from_file_location('tl', %r)\n"
            "tl = importlib.util.module_from_spec(spec); spec.loader.exec_module(tl)\n"
            "print(json.dumps(%s, default=str))\n" % (LOCK_TOOL, expr))
    p = run(["-c", code])
    try:
        return json.loads(p.stdout)
    except ValueError:
        return "ERR rc=%s out=%s err=%s" % (p.returncode, p.stdout, p.stderr)


def transcript(name, *uses):
    stamp = datetime.datetime.now(datetime.timezone.utc)
    iso = stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    rows = [{"type": "user", "timestamp": iso, "message": {"role": "user", "content": "go"}}]
    for i, (tool, inp) in enumerate(uses):
        rows.append({"type": "assistant", "timestamp": iso, "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % i,
                                              "name": tool, "input": inp}]}})
        rows.append({"type": "user", "timestamp": iso, "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % i,
                                         "content": "ok"}]}})
    rows.append({"type": "assistant", "timestamp": iso,
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}})
    path = os.path.join(TMP, name + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def sign_config(repo):
    'Sign for real with a throwaway key: no ssh-agent, no unsigned fallback.'
    if not os.path.exists(SIGN_KEY):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", SIGN_KEY],
                       check=True, capture_output=True)
        
        
        os.chmod(SIGN_KEY, 0o600)
    for key, value in (("gpg.format", "ssh"), ("user.signingkey", SIGN_KEY),
                       ("commit.gpgsign", "true")):
        git(repo, "config", key, value)


def setup():
    for rel in TRACKED:
        write(os.path.join(STORE, rel), ORIGINAL)
    git(STORE, "init", "-q", "-b", "main")
    sign_config(STORE)
    git(STORE, "add", "-A")
    git(STORE, "commit", "-q", "-m", "init")
    for path, branch in ((WT_AGENTS, "wa"), (WT_AGENTS2, "wa2"), (WT_CLAUDE, "wc"),
                         (WT_ELSEWHERE, "we")):
        git(STORE, "worktree", "add", "-q", "-b", branch, path)


def criterion_1():
    print("1. a lock in a store worktree is a record in that worktree")
    for label, wt in (("under .agents/worktrees", WT_AGENTS),
                      ("in an arbitrary directory", WT_ELSEWHERE)):
        target = os.path.join(wt, T1)
        p = lock(target)
        recs = records(wt)
        rec = recs.get(expected_name(T1))
        check("%s: lock writes global/test-locks/<name>.json in the worktree" % label,
              p.returncode == 0 and rec is not None
              and set(rec) == {"path", "sha256", "locked_at"} and rec["path"] == T1
              and rec["sha256"] == sha(target),
              "rc=%s out=%s err=%s recs=%s" % (p.returncode, p.stdout, p.stderr, recs))
        check("%s: nothing written to main's records or the host manifest" % label,
              not records(STORE) and not host_manifests(),
              "main=%s host=%s" % (records(STORE), host_manifests()))
        s = status(wt)
        check("%s: status in the worktree lists it as ok" % label,
              s.returncode == 0 and T1 in s.stdout and "ok" in s.stdout,
              "rc=%s out=%s err=%s" % (s.returncode, s.stdout, s.stderr))
        p = lock(target)
        check("%s: re-locking the unchanged file prints already" % label,
              p.returncode == 0 and "already" in p.stdout and T1 in p.stdout,
              "rc=%s out=%s err=%s" % (p.returncode, p.stdout, p.stderr))

    
    
    
    target = os.path.join(WT_CLAUDE, T1)
    p = lock(target)
    check(".claude/worktrees: lock does NOT write a shared record in the worktree",
          p.returncode == 0 and expected_name(T1) not in records(WT_CLAUDE),
          "rc=%s recs=%s" % (p.returncode, records(WT_CLAUDE)))
    check(".claude/worktrees: the lock lands in the host manifest instead",
          any(os.path.realpath(m.get("root", "")) == os.path.realpath(WT_CLAUDE)
              and T1 in m.get("files", {}) for m in host_manifests()),
          str(host_manifests()))


def criterion_2():
    print("2. inside the worktree the lock binds edits (worktree record only)")
    manifest_roots = tool_call(
        "[os.path.realpath(d.get('root', '')) for d in tl.manifests()]")
    check("main holds no record; the only manifest is .claude/worktrees' host manifest",
          not records(STORE) and manifest_roots == [os.path.realpath(WT_CLAUDE)],
          "%s %s" % (records(STORE), manifest_roots))
    for label, wt in (("under .agents/worktrees", WT_AGENTS), (".claude/worktrees", WT_CLAUDE)):
        target = os.path.join(wt, T1)
        hit = tool_call("[tl.locked_entry(%r)[1] if tl.locked_entry(%r) else None]"
                        % (target, target))
        check("%s: locked_entry returns the lock" % label, hit == [T1], hit)
        p = hook(EDIT_HOOK, write_payload(wt, target))
        check("%s: a Write to the locked file is refused" % label, p.returncode == 2,
              "rc=%s err=%s" % (p.returncode, p.stderr))
        p = hook(EDIT_HOOK, {"tool_name": "Bash", "cwd": wt,
                             "tool_input": {"command": "echo x > %s" % T1}})
        check("%s: a shell redirect into it is refused" % label, p.returncode == 2,
              "rc=%s err=%s" % (p.returncode, p.stderr))
        p = hook(EDIT_HOOK, write_payload(wt, os.path.join(wt, T2)))
        check("%s: an unlocked file in the same worktree is allowed" % label,
              p.returncode == 0, p.stderr)
    p = hook(EDIT_HOOK, write_payload(WT_AGENTS2, os.path.join(WT_AGENTS2, T1)))
    check("a sibling worktree without that record is not bound by it", p.returncode == 0,
          p.stderr)


def criterion_3():
    print("3. a record on main binds the same path in a worktree")
    p = lock(os.path.join(STORE, T2))
    check("lock on main writes a main record", p.returncode == 0
          and expected_name(T2) in records(STORE), "%s %s" % (p.stdout, p.stderr))
    p = hook(EDIT_HOOK, write_payload(WT_AGENTS2, os.path.join(WT_AGENTS2, T2)))
    check("under .agents/worktrees: a Write to the main-locked path is refused",
          p.returncode == 2, "rc=%s err=%s" % (p.returncode, p.stderr))
    
    
    
    p = hook(EDIT_HOOK, write_payload(WT_CLAUDE, os.path.join(WT_CLAUDE, T2)))
    check(".claude/worktrees: a Write to the SAME path main has locked is now allowed",
          p.returncode == 0, "rc=%s err=%s" % (p.returncode, p.stderr))
    check("a worktree edit to a path locked nowhere is allowed",
          hook(EDIT_HOOK, write_payload(WT_AGENTS2, os.path.join(WT_AGENTS2, T_US))).returncode
          == 0)


def criterion_4():
    print("4. after the branch lands, main shows the lock with the same hash")
    target = os.path.join(WT_AGENTS, T1)
    wanted = sha(target)
    git(WT_AGENTS, "add", "global/test-locks")
    git(WT_AGENTS, "commit", "-q", "-m", "lock")
    git(STORE, "merge", "-q", "--no-edit", "wa")
    s = status(STORE)
    rec = records(STORE).get(expected_name(T1))
    check("status on main lists the landed lock as ok",
          s.returncode == 0 and T1 in s.stdout and rec is not None
          and rec["sha256"] == wanted,
          "rc=%s out=%s err=%s rec=%s" % (s.returncode, s.stdout, s.stderr, rec))
    p = hook(EDIT_HOOK, write_payload(STORE, os.path.join(STORE, T1)))
    check("an edit to the landed lock on main is refused", p.returncode == 2, p.stderr)


def criterion_5():
    print("5. approved unlock removes a record that lives in a worktree")
    
    
    target = os.path.join(WT_AGENTS, T1)
    check("precondition: the worktree record exists",
          expected_name(T1) in records(WT_AGENTS), str(records(WT_AGENTS)))
    check("precondition: main's landed copy of the same record exists too",
          expected_name(T1) in records(STORE), str(records(STORE)))
    p = run([CONSENT, target])
    check("consent reports Unlocked", "Unlocked:" in p.stdout,
          "rc=%s out=%s err=%s" % (p.returncode, p.stdout, p.stderr))
    check("the worktree record is gone", expected_name(T1) not in records(WT_AGENTS),
          str(records(WT_AGENTS)))
    
    
    
    check("main's record for the SAME path (T1) is removed too",
          expected_name(T1) not in records(STORE), str(records(STORE)))
    check("main's record for the OTHER path (T2) is untouched",
          expected_name(T2) in records(STORE), str(records(STORE)))
    p = hook(EDIT_HOOK, write_payload(WT_AGENTS, target))
    check("the path in the worktree is no longer refused after the unlock",
          p.returncode == 0, "rc=%s err=%s" % (p.returncode, p.stderr))


def criterion_6():
    print("6. errors and edge cases")
    target = os.path.join(WT_AGENTS2, T_US)
    p = lock(target)
    write(target, DRIFTED)
    p2 = lock(target)
    check("re-locking a changed file exits 3", p.returncode == 0 and p2.returncode == 3,
          "first=%s second rc=%s err=%s" % (p.returncode, p2.returncode, p2.stderr))
    write(target, ORIGINAL)
    p = lock(os.path.join(WT_AGENTS2, "global", "scripts", "nope.py"))
    check("a missing file exits 2", p.returncode == 2, "rc=%s err=%s" % (p.returncode, p.stderr))
    outside = os.path.join(TMP, "outside", "test-o.py")
    write(outside, ORIGINAL)
    p = lock(outside)
    check("a path outside any checkout exits 2", p.returncode == 2,
          "rc=%s err=%s" % (p.returncode, p.stderr))
    before = records(WT_AGENTS2)
    if os.path.isfile(os.path.expanduser("~/.local/bin/basedpyright")) or shutil.which("basedpyright"):
        bad = os.path.join(WT_AGENTS2, "global", "scripts", "test-bad.py")
        write(bad, "x: int = 'a'\n")
        p = lock(bad)
        check("a basedpyright error refuses the lock",
              p.returncode == 3 and records(WT_AGENTS2) == before,
              "rc=%s err=%s" % (p.returncode, p.stderr))
    else:
        print("  skip  basedpyright is not installed")

    corrupt = os.path.join(rec_dir(WT_AGENTS2), expected_name(T_SL))
    write(corrupt, "{not json")
    s = status(WT_AGENTS2)
    check("a corrupt worktree record is reported by status",
          "corrupt" in s.stdout and s.returncode == 1, "rc=%s out=%s" % (s.returncode, s.stdout))
    p = hook(EDIT_HOOK, write_payload(WT_AGENTS2, os.path.join(WT_AGENTS2, T_SL)))
    check("a corrupt worktree record locks nothing", p.returncode == 0, p.stderr)
    os.remove(corrupt)

    lock(os.path.join(WT_AGENTS2, T_SL))
    names = sorted(records(WT_AGENTS2))
    check("test_a.py and test/a.py get different record names in a worktree",
          expected_name(T_US) in names and expected_name(T_SL) in names
          and expected_name(T_US) != expected_name(T_SL), str(names))

    procs = [subprocess.Popen([sys.executable, LOCK_TOOL, "lock", os.path.join(wt, T2)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=env(), cwd=TMP) for wt in (WT_AGENTS2, WT_ELSEWHERE)]
    outs = [pr.communicate(timeout=120) for pr in procs]
    ok = True
    for wt in (WT_AGENTS2, WT_ELSEWHERE):
        rec = records(wt).get(expected_name(T2))
        ok = ok and rec is not None and rec.get("path") == T2 and rec.get("sha256") == sha(
            os.path.join(wt, T2))
    check("two worktrees locking the same file each keep a valid record",
          ok and all(pr.returncode == 0 for pr in procs),
          "rcs=%s outs=%s" % ([pr.returncode for pr in procs], outs))

    a = os.path.join(WT_AGENTS2, "global", "scripts", "test-w3.py")
    b = os.path.join(WT_ELSEWHERE, "global", "scripts", "test-w4.py")
    write(a, ORIGINAL)
    write(b, ORIGINAL)
    lock(a)
    lock(b)
    check("two worktrees locking different files write different names",
          expected_name("global/scripts/test-w3.py") in records(WT_AGENTS2)
          and expected_name("global/scripts/test-w4.py") in records(WT_ELSEWHERE)
          and expected_name("global/scripts/test-w4.py") not in records(WT_AGENTS2),
          "%s | %s" % (sorted(records(WT_AGENTS2)), sorted(records(WT_ELSEWHERE))))


def criterion_7():
    print("7. must not change")
    proj = os.path.join(TMP, "proj")
    write(os.path.join(proj, "tests", "test_p.py"), ORIGINAL)
    git(proj, "init", "-q", "-b", "main")
    sign_config(proj)
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", "init")
    proj_wt = os.path.join(proj, ".agents", "worktrees", "pw")
    git(proj, "worktree", "add", "-q", "-b", "pw", proj_wt)
    n_before = len(host_manifests())
    p = lock(os.path.join(proj, "tests", "test_p.py"))
    p2 = lock(os.path.join(proj_wt, "tests", "test_p.py"))
    check("a non-store repo and its worktree keep the per-host manifest",
          p.returncode == 0 and p2.returncode == 0 and len(host_manifests()) == n_before + 2
          and not os.path.isdir(rec_dir(proj)) and not os.path.isdir(rec_dir(proj_wt)),
          "rc=%s,%s manifests=%d->%d" % (p.returncode, p2.returncode, n_before,
                                          len(host_manifests())))
    check("a non-store worktree lock still binds edits there",
          hook(EDIT_HOOK, write_payload(proj_wt, os.path.join(proj_wt, "tests", "test_p.py"))
               ).returncode == 2)

    p = lock(os.path.join(STORE, T_SL))
    check("a main-checkout lock writes a main record, no host manifest for the store",
          p.returncode == 0 and expected_name(T_SL) in records(STORE)
          and all(os.path.realpath(m.get("root", "")) != STORE for m in host_manifests()),
          "%s %s" % (p.stderr, sorted(records(STORE))))

    roots = tool_call("[os.path.realpath(d['root']) for d in tl.manifests()]")
    wt_roots = {os.path.realpath(w) for w in (WT_AGENTS, WT_AGENTS2, WT_ELSEWHERE)}
    check("the drift gate's lock set (manifests) never includes a worktree record set",
          isinstance(roots, list) and not (set(roots) & wt_roots), str(roots))

    
    drift_target = os.path.join(WT_AGENTS2, T2)
    write(drift_target, DRIFTED)
    t = transcript("wt-drift", ("Write", {"file_path": drift_target, "content": DRIFTED}))
    p = hook(GATE_HOOK, {"session_id": "wt-drift", "transcript_path": t, "cwd": STORE})
    check("a drifted worktree lock does not block a turn in main",
          '"decision": "block"' not in p.stdout and (WT_AGENTS2 not in p.stdout),
          "rc=%s out=%s err=%s" % (p.returncode, p.stdout, p.stderr))
    write(drift_target, ORIGINAL)

    legacy = {"root": STORE, "files": {T_US: {"sha256": sha(os.path.join(STORE, T_US)),
                                              "locked_at": "2026-09-14T00:00:00Z"}}}
    key = hashlib.sha1(STORE.encode("utf-8")).hexdigest()[:16]
    write(os.path.join(TMP, "host", "locks", key + ".json"), json.dumps(legacy))
    p = run([LOCK_TOOL, "publish"])
    check("publish still turns a host-only store lock into a main record",
          p.returncode == 0 and expected_name(T_US) in records(STORE),
          "rc=%s out=%s err=%s" % (p.returncode, p.stdout, p.stderr))


def criterion_8():
    print("8. narrowing (2026-09-22, user): .claude/worktrees is no longer a store "
          "worktree for lock records")
    store_wt = {os.path.realpath(r) for r in tool_call("tl.store_worktrees()")}
    check(".claude/worktrees is excluded from store_worktrees()",
          os.path.realpath(WT_CLAUDE) not in store_wt, str(sorted(store_wt)))
    check(".agents/worktrees siblings are still recognized",
          {os.path.realpath(WT_AGENTS2), os.path.realpath(WT_ELSEWHERE)} <= store_wt,
          str(sorted(store_wt)))
    wt_record_roots = tool_call(
        "[os.path.realpath(d['root']) for d in tl.worktree_records()]")
    check("worktree_records() never includes .claude/worktrees",
          os.path.realpath(WT_CLAUDE) not in wt_record_roots, str(wt_record_roots))
    check("containing_worktree() does not guess .claude/worktrees for an untracked path",
          tool_call("tl.containing_worktree(%r)"
                    % os.path.join(WT_CLAUDE, "global", "scripts", "untracked.py"))
          is None,
          tool_call("tl.containing_worktree(%r)"
                    % os.path.join(WT_CLAUDE, "global", "scripts", "untracked.py")))
    check("containing_worktree() still finds an untracked path under .agents/worktrees",
          tool_call("tl.containing_worktree(%r)"
                    % os.path.join(WT_AGENTS2, "global", "scripts", "untracked.py"))
          == os.path.realpath(WT_AGENTS2),
          tool_call("tl.containing_worktree(%r)"
                    % os.path.join(WT_AGENTS2, "global", "scripts", "untracked.py")))


def main():
    setup()
    for step in (criterion_1, criterion_2, criterion_3, criterion_4, criterion_5,
                 criterion_6, criterion_7, criterion_8):
        try:
            step()
        except subprocess.CalledProcessError as exc:
            check("%s ran to the end" % step.__name__, False,
                  "%s: %s" % (exc.cmd, (exc.stderr or "").strip()))


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = results.count(False)
    print("\n%d passed, %d failed" % (results.count(True), failed))
    sys.exit(1 if failed else 0)
