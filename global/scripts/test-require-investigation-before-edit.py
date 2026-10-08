#!/usr/bin/env python3
'test-require-investigation-before-edit: cases for the investigation gate.\n\nRuns the shared payload-shaped cases from hook-test-cases.py through hook-test-run.py,\nthen the stateful sequences the shared runner cannot express: deny once per\n(session, file), denial condensing after the third, per-agent ledgers, fail-open on a\nbad ledger, and the per-call time bound.\n\nFully hermetic: HOME is redirected, so the ledger under\n$HOME/.local/state/agent-context/edit-gate/ is created in the fixture and the real one\nis never read or written. The fixture repo sits under ~/.local/share, outside every path\nthe gate exempts as scratch (.agents/tmp/, ~/.cache/, /tmp/).'
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS_DIR = os.path.join(os.path.dirname(HERE), "hooks")   
if not os.path.isfile(os.path.join(HOOKS_DIR, "require-investigation-before-edit.py")):
    HOOKS_DIR = os.path.expanduser("~/.agent-context/global/hooks")
HOOK = os.path.join(HOOKS_DIR, "require-investigation-before-edit.py")

proc = subprocess.run(
    [sys.executable, os.path.join(HERE, "hook-test-run.py"),
     "--hooks-dir", HOOKS_DIR, "--hook", "require-investigation-before-edit.py"] + sys.argv[1:])
shared_failed = 1 if proc.returncode != 0 else 0    

if not os.path.isfile(HOOK):
    print("missing: %s" % HOOK)
    sys.exit(1)

PID = os.getpid()
ROOT = os.path.expanduser("~/.cache/agent-context/investigation-gate-test.%d" % PID)
REPO = os.path.expanduser("~/.local/share/hook-test-fixtures/igate-seq-%d" % PID)
for path in (ROOT, REPO):
    shutil.rmtree(path, ignore_errors=True)
HOME_DIR = os.path.join(ROOT, "home")
os.makedirs(HOME_DIR)
os.makedirs(os.path.join(REPO, "src"))
FILES = [os.path.join(REPO, "src", "f%d.py" % i) for i in range(1, 8)]
for path in FILES:
    with open(path, "w") as fh:
        fh.write("x = 1\n")
KEY = os.path.join(ROOT, "signing-key")           
subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", KEY], check=True)
os.chmod(KEY, 0o600)                              
for step in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "one"]):
    subprocess.run(["git", "-C", REPO, "-c", "user.email=t@t", "-c", "user.name=t",
                    "-c", "gpg.format=ssh", "-c", "user.signingkey=" + KEY,
                    "-c", "commit.gpgsign=true"] + step, check=True)


def transcript(name, *records):
    path = os.path.join(ROOT, name + ".jsonl")
    with open(path, "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


USER = {"type": "user", "message": {"role": "user", "content": "change f1"}}
EMPTY = transcript("empty", USER)
EVIDENCE = transcript("evidence", USER, {"type": "assistant", "message": {
    "role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Grep",
                                      "input": {"pattern": "f1.py"}}]}})
failed = shared_failed


def call(path, sid, transcript_path=EMPTY, agent="", home=HOME_DIR, raw=None, tool="Edit"):
    'One hook call. Returns (rc, decision text, elapsed seconds).'
    if raw is None:
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": sid,
                   "tool_input": {"file_path": path, "old_string": "x = 1",
                                  "new_string": "x = 2"},
                   "cwd": REPO}
        if transcript_path is not None:
            payload["transcript_path"] = transcript_path
        if agent:
            payload["agent_id"] = agent
        raw = json.dumps(payload)
    started = time.time()
    done = subprocess.run([sys.executable, HOOK], input=raw, capture_output=True, text=True,
                          env={**os.environ, "HOME": home})
    took = time.time() - started
    text = (done.stdout or "") + (done.stderr or "")
    try:
        doc = json.loads(done.stdout)
        out = doc.get("hookSpecificOutput") or doc
        text = str(out.get("permissionDecisionReason") or text)
        denied = str(out.get("permissionDecision") or "").lower() == "deny"
    except (ValueError, AttributeError):
        denied = False
    return (2 if (done.returncode == 2 or denied) else done.returncode), text, took


def check(label, ok, detail=""):
    global failed
    print("  %s %s%s" % ("ok  " if ok else "FAIL", label, "" if ok else " :: " + detail))
    if not ok:
        failed = 1


def verdict(label, want, path, sid, **kw):
    rc, text, _ = call(path, sid, **kw)
    got = "DENY" if rc == 2 else ("ALLOW" if rc == 0 else "rc=%d" % rc)
    check("%-5s %s" % (want, label), got == want, "got %s" % got)
    return text


def ledger_file(sid):
    return os.path.join(HOME_DIR, ".local/state/agent-context/edit-gate", sid + ".json")


print("--- criterion 3: deny once per (session, file) ---")
sid = "once-%d" % PID
verdict("first edit of f1 with no search", "DENY", FILES[0], sid)
check("the denial wrote the ledger file", os.path.isfile(ledger_file(sid)), ledger_file(sid))
verdict("retry of f1 after the denial", "ALLOW", FILES[0], sid)
verdict("a later edit of f1", "ALLOW", FILES[0], sid)
verdict("a different untouched file f2", "DENY", FILES[1], sid)
verdict("retry of f2", "ALLOW", FILES[1], sid)
verdict("a new session denies f1 again", "DENY", FILES[0], "once2-%d" % PID)

print("--- criterion 2 + 3: evidence still allows the first edit, no denial recorded ---")
sid = "evid-%d" % PID
verdict("f1 with a grep naming f1.py", "ALLOW", FILES[0], sid, transcript_path=EVIDENCE)
verdict("f2 in the same transcript has no evidence", "DENY", FILES[1], sid,
        transcript_path=EVIDENCE)

print("--- criterion 5: three full denials, then one line ---")
sid = "condense-%d" % PID
for n in (1, 2, 3):
    text = verdict("denial %d carries the fact list" % n, "DENY", FILES[n - 1], sid)
    low = text.lower()
    check("denial %d names importers, public symbols, schema, instruction" % n,
          all(k in low for k in ("importers", "public symbols", "schema", "instruction")),
          text[:120])
for n in (4, 5):
    text = verdict("denial %d" % n, "DENY", FILES[n - 1], sid)
    check("denial %d is one line naming its count" % n,
          len(text.strip().splitlines()) == 1 and ("denial %d" % n) in text.lower(),
          repr(text[:160]))
    check("denial %d has no fact list" % n,
          not any(k in text.lower() for k in ("importers", "public symbols", "schema")),
          text[:160])
verdict("retry of the 4th-denied file stays allowed", "ALLOW", FILES[3], sid)
verdict("retry of the 5th-denied file stays allowed", "ALLOW", FILES[4], sid)

print("--- criterion 7: a subagent has its own deny-once ledger, evidence skipped ---")
sid = "agent-%d" % PID
verdict("agent A first edit of f1, transcript HAS evidence", "DENY", FILES[0], sid,
        transcript_path=EVIDENCE, agent="agent-A")
verdict("agent A retry of f1", "ALLOW", FILES[0], sid, transcript_path=EVIDENCE,
        agent="agent-A")
verdict("agent B first edit of f1 is independent of A", "DENY", FILES[0], sid,
        transcript_path=EVIDENCE, agent="agent-B")
verdict("orchestrator ledger for f1 is independent of the agents", "DENY", FILES[0], sid,
        transcript_path=EMPTY)
verdict("orchestrator retry of f1", "ALLOW", FILES[0], sid, transcript_path=EMPTY)
verdict("agent A first edit of f2", "DENY", FILES[1], sid, transcript_path=EVIDENCE,
        agent="agent-A")

print("--- criterion 6: fail open ---")
sid = "open-%d" % PID
verdict("control: same call with a good ledger denies", "DENY", FILES[5], sid)
for label, raw in (("malformed JSON on stdin", "{not json"), ("empty stdin", ""),
                   ("stdin JSON that is a list", "[1, 2]")):
    verdict(label, "ALLOW", FILES[6], sid, raw=raw)
verdict("transcript_path absent from the payload", "ALLOW", FILES[6], sid,
        transcript_path=None)
verdict("transcript_path names a missing file", "ALLOW", FILES[6], sid,
        transcript_path=os.path.join(ROOT, "missing.jsonl"))
verdict("transcript_path names a directory (unreadable)", "ALLOW", FILES[6], sid,
        transcript_path=ROOT)
BAD_HOME = os.path.join(ROOT, "bad-home")
os.makedirs(BAD_HOME)
with open(os.path.join(BAD_HOME, ".local"), "w") as fh:        
    fh.write("blocks makedirs\n")
rc, text, _ = call(FILES[0], "unwritable-%d" % PID, home=BAD_HOME)
check("ALLOW with an unwritable ledger dir, exit 0", rc == 0, "rc=%d %s" % (rc, text[:100]))

print("--- criterion 6: runtime, 20 calls averaged, bound 1.0 s ---")
times = []
for i in range(20):
    times.append(call(FILES[i % 7], "timing-%d-%d" % (PID, i // 7), transcript_path=EVIDENCE)[2])
mean = sum(times) / len(times)
check("mean %.3f s per call" % mean, mean < 1.0, "mean %.3f s" % mean)

shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(REPO, ignore_errors=True)
print()
print("ALL PASS" if failed == 0 else "FAILURES")
sys.exit(failed)
