#!/usr/bin/env python3
"Every blocked sample is driven through the three places a message can reach him: a\nwrite (plain-language-check), the main loop's final message (terse-output-gate) and a\nworker's final message (subagent-return-gate). Every allowed sample goes through the\nsame three and must pass all of them, because a false block in the main loop shows\nuser the message twice.\n\nHooks and scripts are read from the store checkout, not the ~/.claude projection, so\nthe battery tests the edit before it is materialized. PL_BLOCKLIST_STORE overrides it.\n\nUsage: test-plain-language-blocklist.py"
import json
import os
import subprocess
import sys
import tempfile

if any(a in ("-h", "--help") for a in sys.argv[1:]):
    print(__doc__)
    sys.exit(0)

STORE = os.environ.get("PL_BLOCKLIST_STORE") or os.path.expanduser("~/.agent-context")
G = os.path.join(STORE, "global")
HOOKS = os.path.join(G, "hooks")
SCRIPTS = os.path.join(G, "scripts")
TMP = tempfile.mkdtemp(prefix="pl-blocklist-")
SECTIONS = 12
_step = [0]


def progress(label):
    'One flushed line per section, so a background run shows where it is.'
    _step[0] += 1
    print("[%d/%d] %s" % (_step[0], SECTIONS, label), file=sys.stderr, flush=True)


ENV = dict(os.environ)
ENV.update({
    "PLAIN_LANGUAGE_WORDS": os.path.join(SCRIPTS, "plain-language-words.py"),
    "TERSE_JUDGE": os.path.join(SCRIPTS, "terse-judge.py"),
    "TERSE_GATE_STATE_DIR": os.path.join(TMP, "gate"),
    "TERSE_TELEMETRY": os.path.join(TMP, "telemetry.jsonl"),
    "TERSE_DEPTH_DIR": os.path.join(TMP, "depth"),
    "TERSE_CHECK_STATE_DIR": os.path.join(TMP, "check"),
    "SUBAGENT_LENGTH_STATE_DIR": os.path.join(TMP, "sublen"),
    "SUBAGENT_WORDS_STATE_DIR": os.path.join(TMP, "subwords"),
})

failures = []
passes = 0
_uniq = [0]


def check(name, ok, detail=""):
    global passes
    if ok:
        passes += 1
    else:
        failures.append("%s%s" % (name, (": " + detail) if detail else ""))


def uniq():
    _uniq[0] += 1
    return "s%d" % _uniq[0]


def run(path, stdin, env=None):
    e = dict(ENV)
    e.update(env or {})
    return subprocess.run([sys.executable, path], input=stdin, capture_output=True,
                          text=True, env=e, timeout=60)


def transcript(texts, prompt="do the thing"):
    'A real transcript: one prompt, then assistant text blocks with a tool call between.'
    path = os.path.join(TMP, uniq() + ".jsonl")
    rows = [{"type": "user", "message": {"content": prompt}}]
    for i, t in enumerate(texts):
        if i:
            rows.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "true"}}]}})
            rows.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "ok"}]}})
        rows.append({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}})
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def write_denied(text):
    p = run(os.path.join(HOOKS, "plain-language-check.py"), json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": os.path.join(TMP, "notes.md"),
                                              "content": text}}))
    return '"permissionDecision": "deny"' in (p.stdout or ""), p


def stop_blocked(text, active=False):
    payload = {"session_id": uniq(), "transcript_path": transcript([text]),
               "last_assistant_message": text, "cwd": TMP}
    if active:
        payload["stop_hook_active"] = True
    p = run(os.path.join(HOOKS, "terse-output-gate.py"), json.dumps(payload))
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else {}
    except ValueError:
        out = {}
    return out.get("decision") == "block", out.get("reason") or "", p


def worker_blocked(text, active=False, extra=None):
    payload = {"session_id": uniq(), "agent_id": "w", "last_assistant_message": text}
    if active:
        payload["stop_hook_active"] = True
    payload.update(extra or {})
    p = run(os.path.join(HOOKS, "subagent-return-gate.py"), json.dumps(payload))
    return p.returncode == 2, p.stderr or "", p



BLOCKED = [
    
    "The fix is actually in the parser.",
    "The sync is very slow on rp.",
    "Split the file in order to test it.",
    "The reason is a stale lockfile.",
    
    "This is genuinely broken on m4.",
    "Honestly, the cache is stale.",
    "It almost certainly failed on m4.",
    "The build most likely failed on m4.",
    "To be clear, the build passed.",
    
    "The real bug is in the parser.",
    "That is a real gap in coverage.",
    
    "It fails by design.",
    "In practice the queue drains.",
    "For the record, the build passed.",
    "Notably, the queue drains on restart.",
    "Going forward, the daemon retries.",
    
    "The daemon syncs on a five-minute cadence.",
    "That file is load-bearing.",
    "Run a sanity check first.",
    "The blast radius is one repo.",
    "The grep surfaced two docs.",
    "It will bite anyone who adds a package.",
    
    "Worth checking the lockfile next.",
    
    "Good, the tests pass.",
    "Perfect. The lockfile matches.",
    "Let me check the lockfile.",
    "Now let's read the parser.",
    "Given the effort budget, I will stop here.",
    
    "One note up front: the LSP is down.",
    "Two things you should know: the LSP is down.",
    "Keep in mind the cache is cold.",
    "Flagging one more hit in the parser.",
    
    "Say the word and I will land it.",
    "If you want it landed, I can do that.",
    "Happy to run the suite.",
    "Let me know if the build fails.",
    
    "As you asked, I did not run anything.",
    "I did not run the repair unasked.",
    
    "Confidence: high. Severity: low.",
    "The cause is read, not measured.",
    
    "Use a period rather than a comma.",
    "It subtracts instead of adding.",
    "The test is wrong, not the code.",
    "Not a bug, but a flake on CI.",
    "It matches exactly the old output.",
    "The parser is fully replaced.",
    "It removes the cache entirely.",
    "It compiles cleanly on m4.",
    "Match the header precisely.",
    "It drops the row silently.",
]

progress("blocked samples: writes, main and worker messages")
for s in BLOCKED:
    denied, p = write_denied(s)
    check("write denied: %r" % s, denied, (p.stdout or p.stderr)[:160])
    blocked, reason, p = stop_blocked(s)
    check("main final message blocked: %r" % s, blocked, (p.stdout or p.stderr)[:160])
    wblocked, err, p = worker_blocked(s)
    check("worker report blocked: %r" % s, wblocked, "rc=%s %s" % (p.returncode, err[:160]))


ALLOWED = [
    "Retry exactly once, then fail.",
    "Exactly 3 rows remain.",
    "Use the fully qualified name.",
    "The script resolves the real path of the symlink.",
    "The API surface is unchanged.",
    "rp has been silent for 57 hours.",
    "The parser checks the shape of the payload.",
    "Good: one component per file.",
    "Keep a day's worth of logs.",
    "The write is idempotent; the normal form is computed once.",
    "Done. Three call sites updated.",
    "Banned now: `actually` and `rather than`.",
    "Output:\n\n```\nLet me check the lockfile.\n```",
    "Let the queue drain, then retry.",
    "Not run: instrumented tests.",
    "Expected 200, got 401.",
    "Note: this compares by value.",
    "Severity is a column in the table.",
    "Whether or not the cache is warm, retry.",
    "The heads-up queue drains every minute.",
]

progress("allowed samples")
for s in ALLOWED:
    denied, p = write_denied(s)
    check("write allowed: %r" % s, not denied, (p.stdout or "")[:200])
    blocked, reason, p = stop_blocked(s)
    check("main final message allowed: %r" % s, not blocked, reason[:200])
    wblocked, err, p = worker_blocked(s)
    check("worker report allowed: %r" % s, not wblocked, err[:200])


SAMPLE = "The fix is actually in the parser."
progress("worker gate ordering")
wblocked, err, p = worker_blocked(SAMPLE, active=True)
check("worker rewrite with banned wording is sent back again", wblocked, err[:160])
capped = {"session_id": "cap-session", "agent_id": "cap"}
got = [worker_blocked(SAMPLE, active=True, extra=capped)[0] for _ in range(4)]
check("worker gate allows the report after 3 word blocks", got == [True, True, True, False],
      "got=%s" % got)
worker_blocked("Checked the lockfile.", extra=capped)
wblocked, err, p = worker_blocked(SAMPLE, active=True, extra=capped)
check("a clean report resets the worker's word-block count", wblocked, err[:160])
wblocked, err, p = worker_blocked(SAMPLE, extra={"session_id": "fixed-session"})
wblocked2, err2, p = worker_blocked(SAMPLE, extra={"session_id": "fixed-session"})
check("worker gate blocks a second report from the same worker", wblocked and wblocked2,
      "first=%s second=%s" % (wblocked, wblocked2))
wblocked, err, p = worker_blocked(
    "Actually, the call needed " + "gh" + "p_" + "b7Kq3nR8sT2vX5yZ1aC4dE" + " as the header.")
check("credential check runs before the word check", "live credential" in err, err[:160])
wblocked, err, p = worker_blocked("Actually, do you want me to drop the column?")
check("decision check runs before the word check", "DECISION NEEDED" in err, err[:160])
wblocked, err, p = worker_blocked("finding. " * 500)
check("worker length gate unchanged", wblocked and "budget" in err, err[:160])


progress("block reasons")
blocked, reason, p = stop_blocked(SAMPLE)
check("main block reason mentions backticks", "backtick" in reason, reason[:200])
check("main block reason says state the fact", "fact" in reason, reason[:200])
wblocked, err, p = worker_blocked(SAMPLE)
check("worker block reason mentions backticks", "backtick" in err, err[:200])
check("worker block reason names the hit", "actually" in err.lower(), err[:200])
blocked, reason, p = stop_blocked(SAMPLE, active=True)
check("main gate still stands down on stop_hook_active", not blocked, reason[:120])



progress("next-turn advisory")


def advisory(texts, told=None):
    session = uniq()
    t = transcript(texts)
    with open(t, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": "next"}}) + "\n")
    if told is not None:
        os.makedirs(ENV["TERSE_CHECK_STATE_DIR"], exist_ok=True)
        with open(os.path.join(ENV["TERSE_CHECK_STATE_DIR"], session), "w") as fh:
            fh.write(str(told))
    p = run(os.path.join(HOOKS, "terse-output-check.py"), json.dumps(
        {"session_id": session, "transcript_path": t, "prompt": "next"}))
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else {}
    except ValueError:
        out = {}
    return (out.get("hookSpecificOutput") or {}).get("additionalContext", "")


ctx = advisory(["Let me check the lockfile.", "Done."])
check("advisory names a hit from an intermediate message", "let me" in ctx.lower(), ctx[:200])
ctx = advisory(["Let me check the lockfile.", "Done."], told=5)
check("advisory for word hits has no session cap", "let me" in ctx.lower(), ctx[:200])
ctx = advisory(["Checked the lockfile.", "Done."])
check("advisory stays silent on a clean turn", ctx == "", ctx[:200])


progress("warn tier")
long_clean = " ".join(["the cache holds one key per workspace and"] * 5) + " stops."
p = run(os.path.join(HOOKS, "plain-language-check.py"), json.dumps(
    {"tool_name": "Write", "tool_input": {"file_path": os.path.join(TMP, "n.md"),
                                          "content": long_clean}}))
check("long clean sentence still warns, not denies",
      "systemMessage" in (p.stdout or "") and "deny" not in (p.stdout or ""), p.stdout[:200])


def src(*parts):
    try:
        with open(os.path.join(*parts), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""



progress("sources")
pre = src(HOOKS, "preflight-core-health.py")
check("preflight text drops 'first message'", "first message" not in pre)
check("preflight text drops 'OFFER to run it'", "OFFER to run" not in pre)
check("preflight text asks for the repair command in one line",
      "one line" in pre and "repair command" in pre)


skill = src(G, "skills", "test-first-delivery", "SKILL.md")
ralph = src(G, "docs", "archive", "context-inventory-ralph.md")
check("test-first-delivery drops measured/read/guessed", "guessed" not in skill)
check("test-first-delivery asks to name unverified claims", "did not verify" in skill)
check("context-inventory-ralph drops measured/read/guessed", "guessed" not in ralph)


progress("worker definitions")
for name in ("worker-device", "worker-explore", "worker-implement", "worker-ops",
             "worker-review"):
    body = src(G, "agents", name + ".md")
    check("%s bans narration between tool calls" % name, "between tool calls" in body)
    check("%s bans Confidence/Severity tags" % name, "`Confidence:`" in body)
    check("%s bans offers" % name, "No offers" in body)


progress("invariant on a fixture store")
fx = os.path.join(TMP, "store")
for d in ("global/hooks", "global/scripts", "global/instructions", "global/agents"):
    os.makedirs(os.path.join(fx, d), exist_ok=True)
with open(os.path.join(fx, "global/hooks/bad-hook.py"), "w") as fh:
    fh.write('"""Docstring prose is not a message: genuinely."""\n'
             'MSG = "This is genuinely broken, rather than slow."\n')
with open(os.path.join(fx, "global/hooks/ok-hook.py"), "w") as fh:
    fh.write('# a comment is not a message: genuinely\nMSG = "Retry exactly once."\n')
with open(os.path.join(fx, "global/instructions/rules.md"), "w") as fh:
    fh.write("# Rules\n\nLet me check the lockfile.\n")
with open(os.path.join(fx, "global/agents/worker-x.md"), "w") as fh:
    fh.write("Report the result. Quote `actually` in backticks.\n")
p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "invariant-check.py"), "--json"],
                   capture_output=True, text=True, timeout=120,
                   env=dict(ENV, AGENT_CONTEXT_STORE=fx))
try:
    report = json.loads(p.stdout)
except ValueError:
    report = {}
rec = [r for r in (report.get("failing") or []) if r.get("invariant") == "agent-read-text-plain"]
sites = {v.get("site") for r in rec for v in r.get("violations", [])}
check("invariant flags a hook message string", "bad-hook.py" in sites, "sites=%s rc=%s %s"
      % (sites, p.returncode, (p.stderr or "")[-200:]))
check("invariant flags an instruction", "rules.md" in sites, "sites=%s" % sites)
check("invariant ignores docstrings, comments and code spans",
      "ok-hook.py" not in sites and "worker-x.md" not in sites, "sites=%s" % sites)

progress("invariant on the store")
p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "invariant-check.py"), "--json"],
                   capture_output=True, text=True, timeout=120, env=ENV)
try:
    real = json.loads(p.stdout)
except ValueError:
    real = {}
listed = subprocess.run([sys.executable, os.path.join(SCRIPTS, "invariant-check.py"), "--list"],
                        capture_output=True, text=True, timeout=120, env=ENV).stdout
check("invariant agent-read-text-plain is registered", "agent-read-text-plain" in listed)
real_fail = [r for r in (real.get("failing") or []) if r.get("invariant") == "agent-read-text-plain"]
check("the store holds agent-read-text-plain", "agent-read-text-plain" in listed
      and not real_fail and "failing" in real, json.dumps(real_fail)[:300])


progress("word module")
words = os.path.join(SCRIPTS, "plain-language-words.py")
p = subprocess.run([sys.executable, words, "--check"], capture_output=True, text=True, env=ENV)
check("plain-language-words --check shows no drift", p.returncode == 0, p.stdout[:200])
brief = subprocess.run([sys.executable, words, "--brief"], capture_output=True, text=True,
                       env=ENV).stdout
for needle in ("rather than", "instead of", "actually", "Let me", "Confidence:", "say the word",
               "silently", "cadence"):
    check("brief carries %r" % needle, needle.lower() in brief.lower())
doc = src(G, "docs", "plain-language.md")
check("plain-language.md drops the warned soft tier", "**Soft, warned.**" not in doc)
check("plain-language.md lists the new blocked groups", "cadence" in doc and "Let me" in doc)


progress("agent-prose-scan")
root = os.path.join(TMP, "projects")
main_dir = os.path.join(root, "-Users-x-proj")
os.makedirs(os.path.join(main_dir, "sess", "subagents"), exist_ok=True)
eval_dir = os.path.join(root, "-Users-x--cache-tmp-eval-abc")
os.makedirs(eval_dir, exist_ok=True)


def jsonl(path, texts):
    with open(path, "w", encoding="utf-8") as fh:
        for t in texts:
            fh.write(json.dumps({"type": "assistant", "timestamp": "2026-09-15T00:00:00Z",
                                 "message": {"content": [{"type": "text", "text": t}]}}) + "\n")


jsonl(os.path.join(main_dir, "sess.jsonl"), ["Let me check the lockfile.", "Done."])
jsonl(os.path.join(main_dir, "sess", "subagents", "agent-1.jsonl"),
      ["Good, found it. The real bug is here."])
jsonl(os.path.join(eval_dir, "s.jsonl"), ["As you asked, I did not run anything."])
p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "agent-prose-scan.py"), "--root", root,
                    "--days", "30", "--json"], capture_output=True, text=True, env=ENV, timeout=120)
try:
    scan = json.loads(p.stdout)
except ValueError:
    scan = {}
check("agent-prose-scan runs", p.returncode == 0 and bool(scan), (p.stdout + p.stderr)[:200])
for kind, msgs, hits in (("main", 2, 1), ("subagent", 1, 2), ("eval", 1, 1)):
    k = scan.get(kind) or {}
    check("agent-prose-scan %s messages" % kind, k.get("messages") == msgs, json.dumps(k)[:200])
    check("agent-prose-scan %s hits" % kind, (k.get("hits") or 0) >= hits, json.dumps(k)[:200])

if failures:
    print("FAIL (%d failed, %d passed)" % (len(failures), passes))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ok: plain-language blocklist, %d checks pass" % passes)
