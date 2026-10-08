#!/usr/bin/env python3
"The fix: a lock on a store test is a record committed inside the store, at\nglobal/test-locks/<path>.json, and the records are the store's lock state on every\nhost. This battery simulates two hosts with two lock state dirs sharing one store.\n\nRun: python3 ~/.agent-context/global/scripts/test-test-lock-shared.py"
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
LOCK_TOOL = os.path.join(SCRIPTS, "test-lock.py")
CONSENT = os.path.join(SCRIPTS, "test-lock-consent.py")
EDIT_HOOK = os.path.join(HOOKS, "block-locked-test-edit.py")
GATE_HOOK = os.path.join(HOOKS, "locked-test-drift-gate.py")
SELF_GRANT = os.path.join(HOOKS, "block-consent-self-grant.py")

TMP = os.path.realpath(tempfile.mkdtemp(prefix="test-lock-shared-"))
STORE = os.path.join(TMP, "store")
TEST_X = os.path.join(STORE, "global", "scripts", "test-x.py")
APP = os.path.join(STORE, "global", "scripts", "app.py")
GUARD = os.path.join(STORE, "global", "hooks", "guard-x.py")
OTHER_TEST_X = os.path.join(STORE, "projects", "Other", "scripts", "test-x.py")
RECORDS = os.path.join(STORE, "global", "test-locks")
ORIGINAL = "def test_x():\n    assert 0\n"
DRIFTED = "def test_x():\n    assert 1\n"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:600]
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True,
                   check=True)


def env(host, store=STORE):
    e = dict(os.environ)
    e.update({
        "TEST_LOCK_STATE_DIR": os.path.join(TMP, "host-" + host, "locks"),
        "XDG_STATE_HOME": os.path.join(TMP, "host-" + host, "state"),
        "TEST_LOCK_STORE_ROOT": store,
        "AGENT_CONTEXT_STORE": store,
        "TEST_LOCK_TOOL": LOCK_TOOL,
        "SHELL_COMMAND_SCAN": os.path.join(SCRIPTS, "shell-command-scan.py"),
        "LOCKED_TEST_GATE_STATE_DIR": os.path.join(TMP, "host-" + host, "gate"),
        "VERIFICATION_CLAIM_JUDGE": os.path.join(SCRIPTS, "verification-claim-judge.py"),
    })
    return e


def run(argv, host, stdin=None, cwd=None, store=STORE):
    return subprocess.run([sys.executable] + argv, input=stdin, capture_output=True,
                          text=True, env=env(host, store), cwd=cwd or TMP, timeout=120)


def hook(path, host, payload, store=STORE):
    return run([path], host, stdin=json.dumps(payload), store=store)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def records():
    out = {}
    for p in sorted(glob.glob(os.path.join(RECORDS, "*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
            out[doc.get("path")] = doc
        except (OSError, ValueError):
            out[os.path.basename(p)] = None
    return out


def mcp(tool, **tool_input):
    return {"tool_name": "mcp__agent-context__" + tool, "cwd": STORE, "tool_input": tool_input}


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


def main():
    for path, text in ((TEST_X, ORIGINAL), (APP, "x = 1\n"), (GUARD, "print(1)\n"),
                       (OTHER_TEST_X, ORIGINAL)):
        write(path, text)
    git(STORE, "init", "-q")

    print("store-wide locks")
    
    p = run([LOCK_TOOL, "lock", TEST_X, GUARD], "a")
    recs = records()
    check("locking a store test on host A writes a record in the store",
          p.returncode == 0 and "global/scripts/test-x.py" in recs
          and (recs["global/scripts/test-x.py"] or {}).get("sha256") == sha(TEST_X),
          "rc=%s out=%s err=%s records=%s" % (p.returncode, p.stdout, p.stderr, recs))
    check("one record file per lock",
          len(glob.glob(os.path.join(RECORDS, "*.json"))) == 2,
          str(os.listdir(RECORDS)) if os.path.isdir(RECORDS) else "no records dir")

    
    p = run([LOCK_TOOL, "status", STORE], "b")
    check("status on host B lists the lock from the record",
          "global/scripts/test-x.py" in p.stdout and "No locked tests" not in p.stdout,
          p.stdout + p.stderr)

    p = hook(EDIT_HOOK, "b", {"tool_name": "Write", "cwd": STORE,
                              "tool_input": {"file_path": TEST_X, "content": DRIFTED}})
    check("host B: a Write to the locked store test is refused", p.returncode == 2,
          "rc=%s err=%s" % (p.returncode, p.stderr))
    p = hook(EDIT_HOOK, "b", {"tool_name": "Bash", "cwd": STORE,
                              "tool_input": {"command": "echo x > global/scripts/test-x.py"}})
    check("host B: a shell redirect into it is refused", p.returncode == 2, p.stderr)

    
    
    
    
    p = hook(EDIT_HOOK, "b", mcp("edit_body", kind="script", key="test-x",
                                 old_string="0", new_string="1"))
    check("host B: edit_body on the locked script is left to the daemon", p.returncode == 0,
          p.stderr)
    p = hook(EDIT_HOOK, "b", mcp("bulk_edit", edits=[
        {"kind": "script", "key": "app", "replacements": [["1", "2"]]},
        {"kind": "script", "key": "test-x", "replacements": [["0", "1"]]}]))
    check("host B: a bulk_edit that touches it is left to the daemon", p.returncode == 0,
          p.stderr)
    p = hook(EDIT_HOOK, "b", mcp("upsert_script", name="test-x", script_body=DRIFTED))
    check("host B: upsert_script over it is left to the daemon", p.returncode == 0, p.stderr)
    p = hook(EDIT_HOOK, "b", mcp("upsert_hook", name="guard-x", script_body="print(2)\n"))
    check("host B: upsert_hook over a locked hook is left to the daemon", p.returncode == 0,
          p.stderr)
    p = hook(EDIT_HOOK, "b", mcp("edit_body", kind="script", key="test-x", project="Missing",
                                 old_string="0", new_string="1"))
    check("host B: a project-scoped edit_body is left to the daemon", p.returncode == 0,
          p.stderr)
    p = hook(EDIT_HOOK, "b", mcp("edit_body", kind="script", key="test-x", project="Other",
                                 old_string="0", new_string="1"))
    check("host B: the project's own unlocked script of the same name is allowed",
          p.returncode == 0, p.stderr)
    p = hook(EDIT_HOOK, "b", mcp("edit_body", kind="script", key="app",
                                 old_string="1", new_string="2"))
    check("host B: edit_body on an unlocked script is allowed", p.returncode == 0, p.stderr)
    p = hook(EDIT_HOOK, "b", mcp("edit_body", kind="doc", key="notes.md",
                                 old_string="a", new_string="b"))
    check("host B: edit_body on a doc is allowed", p.returncode == 0, p.stderr)

    
    write(TEST_X, DRIFTED)
    t = transcript("drift", ("Write", {"file_path": TEST_X, "content": DRIFTED}))
    p = hook(GATE_HOOK, "b", {"session_id": "shared-drift", "transcript_path": t,
                              "cwd": STORE})
    check("host B: the drift gate stops a turn that changed the locked store test",
          '"decision": "block"' in p.stdout and "test-x.py" in p.stdout,
          p.stdout + p.stderr)
    p = run([LOCK_TOOL, "lock", TEST_X], "b")
    check("host B: re-locking the changed store test is refused", p.returncode == 3,
          "rc=%s err=%s" % (p.returncode, p.stderr))
    write(TEST_X, ORIGINAL)

    
    rec_file = (glob.glob(os.path.join(RECORDS, "*.json")) or [os.path.join(RECORDS, "x.json")])[0]
    p = hook(SELF_GRANT, "b", {"tool_name": "Write", "cwd": STORE,
                               "tool_input": {"file_path": rec_file, "content": "{}"}})
    check("an agent Write to a lock record is refused", p.returncode == 2, p.stderr)
    p = hook(SELF_GRANT, "b", {"tool_name": "Bash", "cwd": STORE,
                               "tool_input": {"command": "rm %s" % rec_file}})
    check("an agent rm of a lock record is refused", p.returncode == 2, p.stderr)

    
    p = run([CONSENT, TEST_X], "b")
    check("unlock on host B reports the file unlocked", "Unlocked:" in p.stdout,
          p.stdout + p.stderr)
    check("unlock on host B removes the record from the store",
          "global/scripts/test-x.py" not in records() and "global/hooks/guard-x.py" in records(),
          str(records()))
    p = hook(EDIT_HOOK, "a", {"tool_name": "Write", "cwd": STORE,
                              "tool_input": {"file_path": TEST_X, "content": DRIFTED}})
    check("host A: after the unlock synced, the edit is allowed", p.returncode == 0, p.stderr)
    log = os.path.join(TMP, "host-b", "state", "agent-context", "test-lock-consent.log")
    check("the unlock is in the consent log",
          os.path.isfile(log) and "test-x.py" in open(log, encoding="utf-8").read())

    
    legacy = {"root": STORE, "files": {"global/scripts/app.py": {
        "sha256": sha(APP), "locked_at": "2026-09-14T00:00:00Z"}}}
    key = hashlib.sha1(STORE.encode("utf-8")).hexdigest()[:16]
    write(os.path.join(TMP, "host-a", "locks", key + ".json"), json.dumps(legacy))
    p = run([LOCK_TOOL, "lock", APP], "a")
    check("migration: locking a legacy store lock again publishes its record",
          p.returncode == 0 and (records().get("global/scripts/app.py") or {}).get("sha256")
          == legacy["files"]["global/scripts/app.py"]["sha256"],
          "rc=%s out=%s err=%s" % (p.returncode, p.stdout, p.stderr))
    legacy["files"]["global/scripts/test-x.py"] = {"sha256": "0" * 64,
                                                   "locked_at": "2026-09-14T00:00:00Z"}
    write(os.path.join(TMP, "host-a", "locks", key + ".json"), json.dumps(legacy))
    p = run([LOCK_TOOL, "lock", TEST_X], "a")
    check("migration: a legacy lock whose file changed is refused, not re-hashed",
          p.returncode == 3 and "global/scripts/test-x.py" not in records(),
          "rc=%s err=%s" % (p.returncode, p.stderr))
    
    leg = os.path.join(STORE, "global", "scripts", "test-leg.py")
    write(leg, ORIGINAL)
    host_d = os.path.join(TMP, "host-d", "locks", key + ".json")
    write(host_d, json.dumps({"root": STORE, "files": {"global/scripts/test-leg.py": {
        "sha256": sha(leg), "locked_at": "2026-09-14T00:00:00Z"}}}))
    p = run([LOCK_TOOL, "publish"], "d")
    check("publish turns every host-only store lock into a record and clears the host copy",
          p.returncode == 0 and "global/scripts/test-leg.py" in records()
          and not os.path.exists(host_d),
          "rc=%s out=%s err=%s" % (p.returncode, p.stdout, p.stderr))

    
    proj = os.path.join(TMP, "proj")
    write(os.path.join(proj, "tests", "test_p.py"), ORIGINAL)
    git(proj, "init", "-q")
    def listing():
        return sorted(os.listdir(RECORDS)) if os.path.isdir(RECORDS) else []

    before = listing()
    p = run([LOCK_TOOL, "lock", os.path.join(proj, "tests", "test_p.py")], "a")
    check("a project-repo lock writes no store record",
          p.returncode == 0 and listing() == before, p.stderr)
    p = hook(EDIT_HOOK, "a", {"tool_name": "Write", "cwd": proj, "tool_input": {
        "file_path": os.path.join(proj, "tests", "test_p.py"), "content": DRIFTED}})
    check("host A still refuses its own project-repo lock", p.returncode == 2, p.stderr)
    p = hook(EDIT_HOOK, "b", {"tool_name": "Write", "cwd": proj, "tool_input": {
        "file_path": os.path.join(proj, "tests", "test_p.py"), "content": DRIFTED}})
    check("host B is not bound by host A's project-repo lock", p.returncode == 0, p.stderr)
    shutil.rmtree(proj)
    p = hook(EDIT_HOOK, "a", {"tool_name": "Write", "cwd": TMP, "tool_input": {
        "file_path": os.path.join(proj, "tests", "test_p.py"), "content": DRIFTED}})
    check("removing a project checkout still clears its locks", p.returncode == 0, p.stderr)

    
    empty = os.path.join(TMP, "empty-store")
    write(os.path.join(empty, "global", "scripts", "test-y.py"), ORIGINAL)
    git(empty, "init", "-q")
    p = hook(EDIT_HOOK, "c", {"tool_name": "Write", "cwd": empty, "tool_input": {
        "file_path": os.path.join(empty, "global", "scripts", "test-y.py"), "content": ""}},
        store=empty)
    check("with no records and no manifests every write is allowed", p.returncode == 0,
          p.stderr)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = results.count(False)
    print("\n%d passed, %d failed" % (results.count(True), failed))
    sys.exit(1 if failed else 0)
