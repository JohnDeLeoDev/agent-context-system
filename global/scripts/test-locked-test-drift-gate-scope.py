#!/usr/bin/env python3
"Battery for locked-test-drift-gate's scope: only drift this session made stops its turn.\n\nA drifted locked file is this session's when a tool call anywhere in its transcript\nnamed the file, or when the session works in that checkout and the file changed after\nthe finished turn began. A transcript with no timestamps keeps the checkout scope.\n\nRun: python3 ~/.claude/scripts/test-locked-test-drift-gate-scope.py"
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")


def _pick(store_rel, home_rel):
    path = os.path.join(STORE, "global", store_rel)
    return path if os.path.exists(path) else os.path.expanduser(home_rel)


HOOK = os.environ.get("LOCKED_GATE_HOOK") or _pick("hooks/locked-test-drift-gate.py",
                                                   "~/.claude/hooks/locked-test-drift-gate.py")
SCRIPTS = _pick("scripts", "~/.claude/scripts")
TMP = os.path.realpath(tempfile.mkdtemp(prefix="drift-scope-"))
REPO = os.path.join(TMP, "r")
LOCKED = os.path.join(REPO, "tests", "test_a.py")
ORIGINAL = "def test_a():\n    assert 0\n"
DRIFTED = "def test_a():\n    assert 1\n"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    tail = "" if ok or not detail else "\n        " + str(detail)[:500]
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, tail))


def env():
    e = dict(os.environ)
    e.update({"TEST_LOCK_STATE_DIR": os.path.join(TMP, "locks"),
              "LOCKED_TEST_GATE_STATE_DIR": os.path.join(TMP, "gate"),
              "TEST_LOCK_TOOL": os.path.join(SCRIPTS, "test-lock.py"),
              "VERIFICATION_CLAIM_JUDGE": os.path.join(SCRIPTS, "verification-claim-judge.py")})
    return e


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def prompt(t, text="go"):
    return {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": text}}


def use(t, name, **inp):
    tid = "tu%d" % int(t * 1000)
    return [{"type": "assistant", "timestamp": iso(t),
             "message": {"role": "assistant",
                         "content": [{"type": "tool_use", "id": tid, "name": name,
                                      "input": inp}]}},
            {"type": "user", "timestamp": iso(t + 1),
             "message": {"role": "user",
                         "content": [{"type": "tool_result", "tool_use_id": tid,
                                      "content": "ok"}]}}]


def say(t, text="Done."):
    return {"type": "assistant", "timestamp": iso(t),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def turn(start, *tools):
    'One finished turn: a prompt at `start`, tool calls, then a closing message.'
    recs = [prompt(start)]
    t = start + 5
    for name, inp in tools:
        recs += use(t, name, **inp)
        t += 5
    recs.append(say(t))
    return recs


def run(case, records, cwd, active=False, timestamps=True):
    if not timestamps:
        records = [{k: v for k, v in r.items() if k != "timestamp"} for r in records]
    path = os.path.join(TMP, case + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    payload = {"hook_event_name": "Stop", "session_id": case, "transcript_path": path,
               "cwd": cwd, "stop_hook_active": active}
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, env=env())
    try:
        doc = json.loads(proc.stdout or "{}")
    except ValueError:
        doc = {}
    return doc.get("decision") == "block", proc


def main():
    os.makedirs(os.path.join(REPO, "tests"))
    subprocess.run(["git", "init", "-q", REPO], check=True, capture_output=True)
    write(LOCKED, ORIGINAL)
    write(os.path.join(REPO, "app.py"), "x = 1\n")
    subprocess.run([sys.executable, os.path.join(SCRIPTS, "test-lock.py"), "lock", LOCKED],
                   check=True, capture_output=True, text=True, env=env())
    now = time.time()
    print("hook under test: %s" % HOOK)

    def case(name, expect_block, mtime, records, cwd, **kw):
        write(LOCKED, DRIFTED if mtime is not None else ORIGINAL, mtime)
        blocked, proc = run(name, records, cwd, **kw)
        verb = "stops the turn" if expect_block else "lets the turn end"
        check("%s %s" % (name.replace("-", " "), verb), blocked == expect_block,
              proc.stdout + proc.stderr)

    
    case("older-drift-by-another-session", False, now - 600,
         turn(now - 60, ("Bash", {"command": "ls"})), REPO)
    case("older-drift-while-this-session-named-a-different-file", False, now - 600,
         turn(now - 60, ("Edit", {"file_path": os.path.join(REPO, "app.py"),
                                  "old_string": "1", "new_string": "2"})), REPO)
    
    case("drift-of-a-file-named-in-an-earlier-turn", True, now - 600,
         turn(now - 1000, ("Edit", {"file_path": LOCKED, "old_string": "0",
                                    "new_string": "1"}))
         + turn(now - 60, ("Bash", {"command": "ls"})), TMP)
    
    case("drift-during-this-turn-by-an-unnamed-route", True, now - 30,
         turn(now - 60, ("Bash", {"command": "python3 gen.py"})), REPO)
    case("drift-with-no-transcript-timestamps", True, now - 600,
         turn(now - 60, ("Bash", {"command": "ls"})), REPO, timestamps=False)
    case("drift-when-stop-hook-active", False, now - 30,
         turn(now - 60, ("Bash", {"command": "python3 gen.py"})), REPO, active=True)
    case("an-unchanged-lock", False, None,
         turn(now - 60, ("Bash", {"command": "python3 gen.py"})), REPO)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)
