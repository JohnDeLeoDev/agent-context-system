#!/usr/bin/env python3
"hook-parity-run: does a hook event behave the same through hook-dispatch.py?\n\nFIXTURE. Built once under $TMPDIR: a local clone of the store with its remotes removed,\nthe store's global/hooks and global/scripts copied from --claude-dir without lsp-canary.py (a\nstale canary verdict would otherwise start a real language server), and a copy of the\nsettings file. home-settings-sync.py, run from the fixture scripts with HOME set to the\nfixture, writes the direct wiring to <fixture>/home/.claude/settings.json and the\ndispatched one to <fixture>/wired/. That state is kept as a pristine copy, and <fixture>/home\nand <fixture>/wired are restored from it before every side of every payload, so the path\nis the same on every run and no state carries over. The source settings file is never\nwritten, and neither is the live wiring.\n\nNEUTRALIZED. A few hooks act past HOME: orphan-sweep kills real processes and reaches\nother hosts, the context-audit and project-memory autoruns start `claude -p` sessions,\nthe usage collector leaves a background writer, and agent-event-notify pushes to user's\nphone. Their fixture copies (NEUTRALIZE) are stubs that record the call, so both sides\nstill select and run them and only their bodies are skipped. Payloads that carry\n_transcript get it written to <fixture>/tmp/transcript.jsonl before each side runs.\n\nTHE TWO SIDES. Direct: the selected hooks run in parallel, as Claude Code runs them, each\nthrough /bin/sh with the payload on stdin. Dispatched: the wired entries for the event\n(the dispatcher, plus any hook that stays direct), with HOOK_DISPATCH_REGISTRY pointing at\nthe fixture registry. Selection reuses hook-dispatch.select(); check A is what proves both\nsides select from the same groups. Each side gets HOME, TMPDIR and CLAUDE_PROJECT_DIR in\nthe fixture, no environment value naming the real home, and no PATH entry under it, so a\nhook cannot reach notify, op or a signing socket.\n\nCHECKS.\n  A  hook-registry.expand() of the dispatched wiring gives exactly the direct entries.\n  B  exit code. C  merged JSON. D  refusal lines and stderr. E  effects.\n  The expected dispatcher output is built from the direct outcomes by the merge rules of\n  the plan's criterion 4, written here apart from hook-dispatch.merge() so the two can\n  disagree. An output field outside those rules (suppressOutput, for one) that the\n  dispatcher does not carry is a mismatch. A hook that exits other than 0 or 2 directly\n  is a mismatch too: the dispatcher fails it open, which Claude Code does not.\n  Effects are the files under the fixture HOME that are new, changed or gone (outside the\n  clone's .git), plus the clone's git status, staged diff and new commit subjects.\n  Timestamps, epoch seconds, pids, commit hashes and the fixture path are normalized.\n\nLIVE ROOTS. Other sessions write the live state while this runs, so a before and after\nsnapshot would fail on their work. Every payload carries a session id unique to this run\ninstead, and a file under a live root, modified since the run began, that names one of\nthose ids or the fixture path fails the run.\n\nUsage:\n  hook-parity-run.py --event <Event> [--event <Event> ...]\n      [--settings PATH] [--claude-dir DIR] [--store DIR] [--live-root DIR ...]\n      [--payload FILE ...] [--no-builtin] [--runs N] [--json] [--keep]\n\n  --live-root replaces the defaults (~/.claude/state, ~/.local/state/agent-context and the\n  store). --payload takes a JSON object or list; each needs hook_event_name and tool_name.\n\nExit: 0 every check passed; 1 a mismatch or a live write; 2 a setup fault, no payloads,\nor an event with no payload set."

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
DEFAULT_STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(REAL_HOME, ".agent-context")
DEFAULT_LIVE = (hp.state_dir(REAL_HOME),
                os.path.join(os.environ.get("XDG_STATE_HOME")
                             or os.path.join(REAL_HOME, ".local", "state"), "agent-context"))
SESSION_PREFIX = "hook-parity-%d-" % os.getpid()




NEUTRALIZE_SH = ("#!/bin/sh\n# hook-parity-run: neutralized in the fixture; records the call.\n"
                 "cat >/dev/null\nmkdir -p \"$HOME/.parity-neutralized\"\n"
                 "echo \"$(basename \"$0\") $*\" >> \"$HOME/.parity-neutralized/calls.log\"\n"
                 "exit 0\n")
NEUTRALIZE_PY = ("import os, sys\n# hook-parity-run: neutralized in the fixture; records the call.\n"
                 "sys.stdin.read()\nd = os.path.join(os.path.expanduser('~'), '.parity-neutralized')\n"
                 "os.makedirs(d, exist_ok=True)\n"
                 "with open(os.path.join(d, 'calls.log'), 'a') as fh:\n"
                 "    fh.write('%s %s\\n' % (os.path.basename(__file__), ' '.join(sys.argv[1:])))\n")
NEUTRALIZE = ("hooks/orphan-sweep.py", "hooks/context-audit-autorun.py",
              "hooks/project-memory-verify-autorun.py", "hooks/token-usage-collect.py",
              "scripts/agent-event-notify.py")
SETTLE = 0.5            
LIVE_READ_LIMIT = 8 * 1024 * 1024
SKIP_LIVE_DIRS = {".venv", "node_modules", "objects", "worktrees", "__pycache__"}
CONTEXT_EVENTS = frozenset(("SessionStart", "UserPromptSubmit"))
RANK = {"allow": 1, "ask": 2, "defer": 3}
KNOWN_TOP = {"systemMessage", "continue", "stopReason", "decision", "reason",
             "hookSpecificOutput"}
KNOWN_SPECIFIC = {"hookEventName", "additionalContext", "permissionDecision",
                  "permissionDecisionReason", "updatedInput"}

ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
EPOCH = re.compile(r"\b1[5-9]\d{8}(?:\d{3})?(?:\.\d+)?\b")
PID = re.compile(r"(\bpid[\"']?\s*[=:]\s*)\d+", re.I)
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


class SetupFault(Exception):
    'Exit 2: the run could not be set up, so nothing was compared.'


def load(name, filename):
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HOOK_REGISTRY = load("hook_registry", "hook-registry.py")
DISPATCH = load("hook_dispatch", "hook-dispatch.py")




def _flat(groups):
    return [(g.get("matcher") or "", json.dumps(h, sort_keys=True))
            for g in groups or [] if isinstance(g, dict)
            for h in g.get("hooks") or [] if isinstance(h, dict)]


def registry_mismatch(direct_groups, wired_hooks, registry, event):
    'None when the dispatched wiring runs exactly the direct entries (matcher and the\n    whole entry); otherwise what differs. Order is not a mismatch: registry_order_note()\n    reports it.'
    guards = registry.get("hooks") if isinstance(registry, dict) else None
    if not isinstance(guards, dict) or event not in guards:
        return "the registry lists no %s guards" % event
    want = _flat(direct_groups)
    got = _flat(HOOK_REGISTRY.expand(wired_hooks, registry).get(event))
    if sorted(want) != sorted(got):
        return "direct only: %s; dispatched only: %s" % (
            [e for e in want if e not in got][:3], [e for e in got if e not in want][:3])
    return None


def registry_order_note(direct_groups, wired_hooks, registry, event):
    'registry order note.'
    guards = registry.get("hooks") if isinstance(registry, dict) else None
    if not isinstance(guards, dict) or event not in guards:
        return None
    separate = {e for e in _flat((wired_hooks or {}).get(event))
                if HOOK_REGISTRY.dispatched_event(json.loads(e[1]).get("command")) is None}
    want = [e for e in _flat(direct_groups) if e not in separate]
    got = [e for e in _flat(HOOK_REGISTRY.expand(wired_hooks, registry).get(event))
           if e not in separate]
    if want == got:
        return None

    def names(rows):
        return ", ".join(DISPATCH.guard_name(json.loads(r[1]).get("command") or "")
                         for r in rows)

    return "order differs: direct %s; dispatcher %s" % (names(want), names(got))




def builtin_payloads(event, store):
    '[(label, payload)] for an event, or None when this tool has no set for it.'
    def at(*parts):
        return os.path.join(store, *parts)
    if event == "PostToolUse":
        return [
            ("read", {"tool_name": "Read", "tool_input": {"file_path": at("README.md")},
                      "tool_response": {"type": "text", "file": {
                          "filePath": at("README.md"), "content": "x\n", "numLines": 1}}}),
            ("bash piped to head", {"tool_name": "Bash",
                                    "tool_input": {"command": "grep -rn needle %s | head -5" % store},
                                    "tool_response": {"stdout": "a\nb\n", "stderr": "",
                                                      "interrupted": False}}),
            ("edit under .agents/scripts", {"tool_name": "Edit", "tool_input": {
                "file_path": at(".agents", "scripts", "parity-probe.sh"),
                "old_string": "a", "new_string": "b"},
                "tool_response": {"filePath": at(".agents", "scripts", "parity-probe.sh")}}),
            ("edit under the store server", {"tool_name": "Edit", "tool_input": {
                "file_path": at("server", "src", "agent_context", "parity_probe.py"),
                "old_string": "a", "new_string": "b"},
                "tool_response": {"filePath": at("server", "src", "agent_context",
                                                 "parity_probe.py")}}),
            ("write a plain file", {"tool_name": "Write", "tool_input": {
                "file_path": at(".agents", "tmp", "parity-probe.txt"), "content": "x\n"},
                "tool_response": {"filePath": at(".agents", "tmp", "parity-probe.txt")}}),
            ("lsp answer", {"tool_name": "mcp__pyright-lsp__hover",
                            "tool_input": {"filePath": at("README.md"), "line": 1, "column": 1},
                            "tool_response": [{"type": "text",
                                               "text": "(function) def probe() -> None"}]}),
            ("lsp answer quoting a dead signature", {
                "tool_name": "mcp__pyright-lsp__definition",
                "tool_input": {"symbolName": "probe"},
                "tool_response": [{"type": "text",
                                   "text": "Symbol: probe\n12| or 'language server is down'"}]}),
            ("store write to a hook", {"tool_name": "mcp__agent-context__upsert_hook",
                                       "tool_input": {"name": "parity-probe",
                                                      "body": "#!/bin/sh\nexit 0\n"},
                                       "tool_response": [{"type": "text", "text": "{}"}]}),
            ("store read", {"tool_name": "mcp__agent-context__get_doc",
                            "tool_input": {"path": "README.md"},
                            "tool_response": [{"type": "text", "text": "{}"}]}),
            ("tool no matcher names", {"tool_name": "WebSearch",
                                       "tool_input": {"query": "parity"},
                                       "tool_response": {"results": []}}),
        ]
    if event == "PostToolUseFailure":
        return [
            ("lsp server down", {"tool_name": "mcp__pyright-lsp__definition",
                                 "tool_input": {"symbolName": "probe"},
                                 "error": "failed to get definition: language server is down; retry"}),
            ("lsp argument error", {"tool_name": "mcp__pyright-lsp__references",
                                    "tool_input": {}, "error": "symbolName must be a string"}),
            ("bash failure", {"tool_name": "Bash", "tool_input": {"command": "false"},
                              "error": "Exit code 1\nboom"}),
        ]

    def turn(*messages):
        'Transcript records, written to the fixture before each side runs.'
        rows = []
        for role, text in messages:
            content = text if role == "user" else [{"type": "text", "text": text}]
            rows.append({"type": role, "message": {"role": role, "content": content}})
        return rows

    if event == "UserPromptSubmit":
        return [
            ("prompt after a long unverified claim", {"prompt": "next step?", "_transcript": turn(
                ("user", "fix the parser"),
                ("assistant", "Fixed. All tests pass and the build is green. "
                              + "The change touches the parser module. " * 40))}),
            ("plain prompt", {"prompt": "status?",
                              "_transcript": turn(("user", "hi"), ("assistant", "ok"))}),
            ("slash command", {"prompt": "/resume-handoff", "_transcript": []}),
        ]
    if event == "Stop":
        return [
            ("turn ends on a prose question", {"stop_hook_active": False, "_transcript": turn(
                ("user", "do it"),
                ("assistant", "I found two options. Should I use the first one?"))}),
            ("turn ends on a result", {"stop_hook_active": False, "_transcript": turn(
                ("user", "do it"), ("assistant", "Done: the parser is fixed."))}),
            ("turn hands user a runnable step", {"stop_hook_active": False, "_transcript": turn(
                ("user", "is it green?"),
                ("assistant", "Run `npm test` and tell me what it prints."))}),
            ("stop hook already active", {"stop_hook_active": True, "_transcript": turn(
                ("user", "do it"), ("assistant", "Done."))}),
        ]
    if event == "SessionStart":
        return [("source %s" % source, {"source": source})
                for source in ("startup", "resume", "clear", "compact")]
    if event == "SessionEnd":
        return [("reason %s" % reason, {"reason": reason})
                for reason in ("clear", "logout", "prompt_input_exit", "other")]
    if event == "Notification":
        return [
            ("permission prompt", {"notification_type": "permission_prompt",
                                   "message": "Claude needs your permission to use Bash"}),
            ("idle prompt", {"notification_type": "idle_prompt",
                             "message": "Claude is waiting for your input"}),
        ]
    if event == "SubagentStop":
        return [("worker report", {"stop_hook_active": False, "agent_type": "worker-review",
                                   "_transcript": turn(("user", "review the diff"),
                                                       ("assistant", "No blocking defects."))})]
    if event == "PreCompact":
        return [("trigger %s" % trigger, {"trigger": trigger, "custom_instructions": ""})
                for trigger in ("manual", "auto")]
    return None


def gather_payloads(opts, fixture_store):
    found = []
    if not opts.no_builtin:
        for event in opts.event:
            items = builtin_payloads(event, fixture_store)
            if items is None:
                raise SetupFault("no payload set for %s; pass --payload with --no-builtin"
                                 % event)
            found += [(event, label, payload) for label, payload in items]
    for path in opts.payload or []:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            raise SetupFault("payload %s is unreadable: %s" % (path, exc))
        for payload in doc if isinstance(doc, list) else [doc]:
            event = payload.get("hook_event_name") if isinstance(payload, dict) else None
            if event not in opts.event or not payload.get("tool_name"):
                raise SetupFault("payload in %s needs hook_event_name (one of %s) and tool_name"
                                 % (path, ", ".join(opts.event)))
            found.append((event, os.path.basename(path), dict(payload)))
    if not found:
        raise SetupFault("no payloads to run")
    for n, (event, _label, payload) in enumerate(found, 1):
        payload.update({"hook_event_name": event, "session_id": SESSION_PREFIX + str(n),
                        "cwd": fixture_store, "permission_mode": "default"})
        payload.setdefault("transcript_path", "")
    return found




class Fixture:
    def __init__(self, root):
        self.root = root
        self.home = os.path.join(root, "home")
        self.wired = os.path.join(root, "wired")
        self.tmp = os.path.join(root, "tmp")
        self.pristine = os.path.join(root, "pristine")
        self.store = os.path.join(self.home, ".agent-context")
        self.manifest = {}
        self.head = ""

    def env(self, payload=None):
        env = {}
        for key, value in os.environ.items():
            if key == "PATH":
                value = os.pathsep.join(p for p in value.split(os.pathsep)
                                        if p and not (p == REAL_HOME
                                                      or p.startswith(REAL_HOME + os.sep)))
            elif REAL_HOME in value or key.startswith(("GIT_", "CLAUDE_", "XDG_")):
                continue
            elif key in ("AGENT_CONTEXT_STORE", "AGENT_CONTEXT_STATE_DIR",
                         "HOOK_DISPATCH_REGISTRY", "LSP_DOWN_STATE_DIR", "TEST_LOCK_STATE_DIR"):
                continue
            env[key] = value
        env.update(HOME=self.home, TMPDIR=self.tmp, CLAUDE_CODE_TMPDIR=self.tmp,
                   CLAUDE_PROJECT_DIR=self.store, GIT_CONFIG_NOSYSTEM="1")
        if payload:
            env["CLAUDE_SESSION_ID"] = payload["session_id"]
        return env


def run_checked(argv, env=None, cwd=None):
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd)
    if proc.returncode != 0:
        raise SetupFault("%s failed (exit %d): %s" % (" ".join(argv[:3]), proc.returncode,
                                                      (proc.stderr or proc.stdout).strip()[-300:]))
    return proc.stdout


def clone_tree(src, dst):
    argv = (["cp", "-cRp", src, dst] if sys.platform == "darwin"
            else ["cp", "-a", "--reflink=auto", src, dst])
    run_checked(argv)


def build(opts, root):
    if not os.path.isdir(os.path.join(opts.store, ".git")):
        raise SetupFault("store %s is not a git repository" % opts.store)
    try:
        with open(opts.settings, encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SetupFault("settings %s is unreadable: %s" % (opts.settings, exc))
    if not isinstance(settings, dict):
        raise SetupFault("settings %s is not a JSON object" % opts.settings)
    fx = Fixture(root)
    os.makedirs(fx.wired)
    os.makedirs(fx.tmp)
    run_checked(["git", "clone", "-q", opts.store, fx.store])
    for remote in run_checked(["git", "-C", fx.store, "remote"]).split():
        run_checked(["git", "-C", fx.store, "remote", "remove", remote])
    fx.head = run_checked(["git", "-C", fx.store, "rev-parse", "HEAD"]).strip()
    
    
    if opts.claude_dir:
        for sub in ("hooks", "scripts"):
            src = os.path.join(opts.claude_dir, sub)
            if not os.path.isdir(src):
                raise SetupFault("%s has no %s directory" % (opts.claude_dir, sub))
            shutil.copytree(src, os.path.join(fx.store, "global", sub), symlinks=True,
                            dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    canary = os.path.join(hp.scripts_dir(fx.home), "lsp-canary.py")
    if os.path.lexists(canary):
        os.remove(canary)
    for rel in NEUTRALIZE:
        target = os.path.join(fx.store, "global", rel)
        if os.path.lexists(target):
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(NEUTRALIZE_PY if rel.endswith(".py") else NEUTRALIZE_SH)
            os.chmod(target, 0o755)

    raw_hooks = settings.get("hooks")
    hooks = raw_hooks if isinstance(raw_hooks, dict) else {}
    already = {HOOK_REGISTRY.dispatched_event(h.get("command")) or event
               for event, groups in hooks.items() for g in groups or [] if isinstance(g, dict)
               for h in g.get("hooks") or [] if isinstance(h, dict)
               and HOOK_REGISTRY.dispatched_event(h.get("command")) is not None}
    direct_set = sorted(already - set(opts.event))
    wired_set = sorted(already | set(opts.event))
    sync = os.path.join(hp.scripts_dir(fx.home), "home-settings-sync.py")
    for target, events in ((hp.settings_file(fx.home), direct_set),
                           (os.path.join(fx.wired, "settings.json"), wired_set)):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(opts.settings, target)
        run_checked([sys.executable, sync, target, "--dispatch", ",".join(events)],
                    env=dict(fx.env(), HOOK_DISPATCH_REGISTRY=os.path.join(os.path.dirname(target),
                                                                          "hook-dispatch.json")))
    os.makedirs(fx.pristine)
    for name in ("home", "wired"):
        clone_tree(os.path.join(root, name), os.path.join(fx.pristine, name))
    fx.manifest = manifest(os.path.join(fx.pristine, "home"))
    return fx


def restore(fx):
    for name in ("home", "wired", "tmp"):
        shutil.rmtree(os.path.join(fx.root, name), ignore_errors=True)
    for name in ("home", "wired"):
        clone_tree(os.path.join(fx.pristine, name), os.path.join(fx.root, name))
    os.makedirs(fx.tmp)




def _walk(home):
    git_dir = os.path.join(home, ".agent-context", ".git")
    for dirpath, dirnames, filenames in os.walk(home):
        if dirpath == git_dir:
            dirnames[:] = []
            filenames = [f for f in filenames if f != "index"]
        for name in filenames:
            yield os.path.join(dirpath, name)


def manifest(home):
    out = {}
    for path in _walk(home):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        out[os.path.relpath(path, home)] = (st.st_size, st.st_mtime_ns)
    return out


def normalize(text, fx):
    for root in {os.path.realpath(fx.root), fx.root}:
        text = text.replace(root, "<FIXTURE>")
    text = ISO.sub("<TS>", text)
    text = EPOCH.sub("<EPOCH>", text)
    text = PID.sub(lambda m: m.group(1) + "<PID>", text)
    return SHA.sub("<SHA>", text)


def effects(fx):
    '{what: normalized digest} for everything the run changed in the fixture.'
    out = {}
    seen = set()
    
    code_cache = os.path.relpath(DISPATCH.code_cache_dir(fx.home), fx.home) + os.sep
    for path in _walk(fx.home):
        rel = os.path.relpath(path, fx.home)
        seen.add(rel)
        if rel.startswith(code_cache):
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if fx.manifest.get(rel) == (st.st_size, st.st_mtime_ns):
            continue
        try:
            if os.path.islink(path):
                data = "link:" + os.readlink(path)
            else:
                with open(path, "rb") as fh:
                    data = fh.read().decode("utf-8", "replace")
        except OSError as exc:
            data = "unreadable:%s" % exc.errno
        
        
        out["file " + normalize(rel, fx)] = hashlib.sha1(normalize(data, fx).encode()).hexdigest()
    for rel in fx.manifest:
        if rel not in seen:
            out["file " + normalize(rel, fx)] = "gone"
    git = ["git", "-C", fx.store]
    for label, argv in (("git status", git + ["status", "--porcelain=v1", "-uall"]),
                        ("git staged diff", git + ["diff", "--cached"]),
                        ("git new commits", git + ["log", "--format=%s", fx.head + "..HEAD"])):
        proc = subprocess.run(argv, capture_output=True, text=True, env=fx.env())
        out[label] = hashlib.sha1(normalize(proc.stdout + proc.stderr, fx).encode()).hexdigest()
    return out




def run_command(command, payload_text, env, timeout, cwd):
    outcome = {"name": DISPATCH.guard_name(command), "command": command, "code": 0,
               "out": "", "err": "", "fault": None}
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                cwd=cwd, start_new_session=True)
    except OSError as exc:
        outcome["fault"] = "could not start: %s" % exc
        return outcome
    try:
        out, err = proc.communicate(payload_text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        outcome["fault"] = "timed out after %gs" % timeout
        return outcome
    outcome.update(code=proc.returncode, out=out.decode("utf-8", "replace"),
                   err=err.decode("utf-8", "replace"))
    return outcome


def run_side(fx, event, payload, groups, extra_env=None):
    entries, problems = DISPATCH.select(groups, event, payload)
    env = fx.env(payload)
    env.update(extra_env or {})
    sent = {k: v for k, v in payload.items() if k != "_transcript"}
    if "_transcript" in payload:
        sent["transcript_path"] = os.path.join(fx.tmp, "transcript.jsonl")
        with open(sent["transcript_path"], "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(row) + "\n" for row in payload["_transcript"])
    text = json.dumps(sent)

    def one(entry):
        timeout = float(entry.get("timeout") or DISPATCH.DEFAULT_TIMEOUT)
        return run_command(entry["command"], text, env, timeout, fx.store)

    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(entries))) as pool:
        outcomes = list(pool.map(one, entries))
    return outcomes, problems, (time.perf_counter() - start) * 1000




def _parse(text):
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(stripped, strict=False)
            if isinstance(doc, dict):
                return doc, ""
        except ValueError:
            pass
    return {}, text or ""


def expected_dispatch(event, outcomes):
    "(exit code, JSON object, refusal lines, stderr chunks, notes) the dispatcher should\n    produce from these direct outcomes, by the plan's criterion 4 merge rules."
    refusals, contexts, messages, side, notes = [], [], [], [], []
    decision = decision_reason = updated = None
    halt, halt_reasons = False, []
    for o in outcomes:
        if o["fault"] or o["code"] not in (0, 2):
            notes.append("%s %s when run directly; the dispatcher fails it open instead"
                         % (o["name"], o["fault"] or "exits %d" % o["code"]))
            continue
        doc, plain = _parse(o["out"])
        specific = doc.get("hookSpecificOutput")
        specific = specific if isinstance(specific, dict) else {}
        
        
        if o["out"].strip():
            notes.append(("printed", doc.get("suppressOutput") is True))
        notes += [("unmodeled", key, doc[key], o["name"]) for key in doc
                  if key not in KNOWN_TOP and key != "suppressOutput"]
        notes += [("unmodeled", "hookSpecificOutput." + key, specific[key], o["name"])
                  for key in specific if key not in KNOWN_SPECIFIC]
        verdict = str(specific.get("permissionDecision") or "").lower()
        refused = o["code"] == 2
        reason = o["err"].strip() if refused else ""
        if verdict == "deny":
            refused = True
            reason = reason or str(specific.get("permissionDecisionReason") or "").strip()
        if str(doc.get("decision") or "").lower() == "block":
            refused = True
            reason = reason or str(doc.get("reason") or "").strip()
        if refused:
            refusals.append("[%s] %s" % (o["name"], reason or "refused without a reason"))
        else:
            if o["err"].strip():
                side.append(o["err"].strip())
            if verdict in RANK and (decision is None or RANK[verdict] > RANK[decision]):
                decision, decision_reason = verdict, specific.get("permissionDecisionReason")
            if updated is None and isinstance(specific.get("updatedInput"), dict):
                updated = specific["updatedInput"]
        context = specific.get("additionalContext")
        if isinstance(context, str) and context.strip():
            contexts.append(context.strip())
        if plain.strip():
            (contexts if event in CONTEXT_EVENTS else side).append(plain.strip())
        message = doc.get("systemMessage")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
        if doc.get("continue") is False:
            halt = True
            if doc.get("stopReason"):
                halt_reasons.append(str(doc["stopReason"]))
    specific = {}
    if not refusals:
        if decision:
            specific["permissionDecision"] = decision
            if decision_reason:
                specific["permissionDecisionReason"] = decision_reason
        if updated is not None:
            specific["updatedInput"] = updated
    if contexts:
        specific["additionalContext"] = "\n\n".join(contexts)
    result = {}
    if specific:
        result["hookSpecificOutput"] = dict({"hookEventName": event}, **specific)
    if messages:
        result["systemMessage"] = "\n\n".join(messages)
    if halt:
        result["continue"] = False
        if halt_reasons:
            result["stopReason"] = "\n\n".join(halt_reasons)
    return (2 if refusals else 0), result, refusals, side, notes


def _unordered(key, value):
    ' unordered.'
    def parts(text):
        return sorted(p.strip() for p in text.split("\n\n") if p.strip())
    if key in ("systemMessage", "stopReason") and isinstance(value, str):
        return parts(value)
    if key == "hookSpecificOutput" and isinstance(value, dict):
        context = value.get("additionalContext")
        return dict(value, additionalContext=parts(context)) if isinstance(context, str) else value
    return value


def compare(fx, event, direct, wired):
    "Mismatch lines between the direct outcomes and the dispatched side's."
    found = []
    stays = {o["command"]: o for o in wired if HOOK_REGISTRY.dispatched_event(o["command"]) is None}
    dispatcher = [o for o in wired if HOOK_REGISTRY.dispatched_event(o["command"]) is not None]
    for o in direct:
        twin = stays.get(o["command"])
        if twin is None:
            continue
        for key in ("code", "out", "err", "fault"):
            if normalize(str(o[key]), fx) != normalize(str(twin[key]), fx):
                found.append("%s (stays direct): %s differs" % (o["name"], key))
    merged = [o for o in direct if o["command"] not in stays]
    if not merged and not dispatcher:
        return found
    if len(dispatcher) != 1:
        return found + ["expected one dispatcher entry for %s, found %d"
                        % (event, len(dispatcher))]
    got = dispatcher[0]
    if got["fault"]:
        return found + ["the dispatcher %s" % got["fault"]]
    code, want, refusals, side, notes = expected_dispatch(event, merged)
    doc, plain = _parse(got["out"])
    if got["code"] != code:
        found.append("exit code: direct %d, dispatched %d" % (code, got["code"]))
    printed = [note[1] for note in notes if isinstance(note, tuple) and note[0] == "printed"]
    if printed and all(printed) and got["out"].strip():
        found.append("every direct hook that printed asked to suppress its output, but the "
                     "dispatcher printed: %s" % got["out"].strip()[:160])
    for note in notes:
        if isinstance(note, str):
            found.append(note)
            continue
        if note[0] == "printed":
            continue
        _kind, key, value, name = note
        top = key.split(".")[-1]
        carried = (doc.get("hookSpecificOutput") or {}) if key.startswith("hookSpecificOutput.") else doc
        if not isinstance(carried, dict) or carried.get(top) != value:
            found.append("%s from %s is not carried by the dispatcher" % (key, name))
    for key in sorted(set(want) | set(k for k in doc if k in KNOWN_TOP)):
        a = normalize(json.dumps(_unordered(key, want.get(key)), sort_keys=True), fx)
        b = normalize(json.dumps(_unordered(key, doc.get(key)), sort_keys=True), fx)
        if a != b:
            found.append("%s: direct %s, dispatched %s" % (key, a[:160], b[:160]))
    if plain.strip():
        found.append("dispatcher stdout is not JSON: %s" % plain.strip()[:160])
    err = normalize(got["err"], fx)
    for line in refusals:
        if normalize(line, fx) not in err:
            found.append("refusal missing from dispatcher stderr: %s" % line[:160])
    if not refusals:
        for chunk in side:
            if normalize(chunk, fx) not in err:
                found.append("stderr missing from dispatcher: %s" % chunk[:160])
    return found


def live_leaks(roots, since, fx):
    marks = [SESSION_PREFIX.encode(), fx.root.encode(), os.path.realpath(fx.root).encode()]
    hits = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_LIVE_DIRS]
            if os.path.realpath(dirpath).startswith(os.path.realpath(fx.root)):
                dirnames[:] = []
                continue
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if st.st_mtime < since:
                    continue
                if SESSION_PREFIX in path:
                    hits.append(path)
                    continue
                try:
                    with open(path, "rb") as fh:
                        data = fh.read(LIVE_READ_LIMIT)
                except OSError:
                    continue
                if any(mark in data for mark in marks):
                    hits.append(path)
    return sorted(hits)




def parse_args(argv):
    parser = argparse.ArgumentParser(prog="hook-parity-run.py", add_help=True)
    parser.add_argument("--event", action="append", required=True)
    parser.add_argument("--settings", default=hp.settings_file(REAL_HOME))
    parser.add_argument("--claude-dir", default=None)
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.add_argument("--live-root", action="append")
    parser.add_argument("--payload", action="append")
    parser.add_argument("--no-builtin", action="store_true")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep", action="store_true")
    opts = parser.parse_args(argv)
    if opts.runs < 1:
        parser.error("--runs takes 1 or more")
    for key in ("settings", "claude_dir", "store"):
        if getattr(opts, key):
            setattr(opts, key, os.path.abspath(os.path.expanduser(getattr(opts, key))))
    opts.live_root = [os.path.abspath(os.path.expanduser(p)) for p in opts.live_root] \
        if opts.live_root else list(DEFAULT_LIVE) + [opts.store]
    return opts


def run(opts):
    since = time.time() - 1
    root = tempfile.mkdtemp(prefix="hook-parity-")
    report = {"events": opts.event, "fixture": None, "registry": {}, "payloads": [],
              "live": {"ok": True, "files": []}, "ok": False}
    try:
        payloads = gather_payloads(opts, os.path.join(root, "home", ".agent-context"))
        fx = build(opts, root)
        with open(hp.settings_file(os.path.join(fx.pristine, "home")),
                  encoding="utf-8") as fh:
            direct_hooks = (json.load(fh).get("hooks") or {})
        with open(os.path.join(fx.pristine, "wired", "settings.json"), encoding="utf-8") as fh:
            wired_hooks = (json.load(fh).get("hooks") or {})
        with open(os.path.join(fx.pristine, "wired", "hook-dispatch.json"),
                  encoding="utf-8") as fh:
            registry = json.load(fh)
        ok = True
        for event in opts.event:
            detail = registry_mismatch(direct_hooks.get(event), wired_hooks, registry, event)
            report["registry"][event] = {
                "ok": detail is None, "detail": detail,
                "note": registry_order_note(direct_hooks.get(event), wired_hooks, registry,
                                            event)}
            ok = ok and detail is None
        wired_env = {"HOOK_DISPATCH_REGISTRY": os.path.join(fx.wired, "hook-dispatch.json")}
        for n, (event, label, payload) in enumerate(payloads, 1):
            print("[%d/%d] %s %s started" % (n, len(payloads), event, label),
                  file=sys.stderr, flush=True)
            times = {"direct": [], "dispatched": []}
            restore(fx)
            direct, _problems, ms = run_side(fx, event, payload, direct_hooks.get(event))
            times["direct"].append(ms)
            time.sleep(SETTLE)
            direct_effects = effects(fx)
            restore(fx)
            wired, _problems, ms = run_side(fx, event, payload, wired_hooks.get(event), wired_env)
            times["dispatched"].append(ms)
            time.sleep(SETTLE)
            wired_effects = effects(fx)
            found = compare(fx, event, direct, wired)
            for key in sorted(set(direct_effects) | set(wired_effects)):
                a, b = direct_effects.get(key, "untouched"), wired_effects.get(key, "untouched")
                if a != b:
                    found.append("effect differs: %s (direct %s, dispatched %s)"
                                 % (key, "untouched" if a == "untouched" else a[:8],
                                    "untouched" if b == "untouched" else b[:8]))
            for _ in range(opts.runs - 1):
                restore(fx)
                times["direct"].append(run_side(fx, event, payload, direct_hooks.get(event))[2])
                restore(fx)
                times["dispatched"].append(
                    run_side(fx, event, payload, wired_hooks.get(event), wired_env)[2])
            report["payloads"].append({
                "event": event, "label": label, "tool": payload.get("tool_name"),
                "ok": not found, "mismatches": found,
                "direct_ms": round(statistics.median(times["direct"]), 1),
                "dispatched_ms": round(statistics.median(times["dispatched"]), 1)})
            ok = ok and not found
        leaks = live_leaks(opts.live_root, since, fx)
        report["live"] = {"ok": not leaks, "files": leaks}
        report["ok"] = ok and not leaks
        if opts.keep:
            report["fixture"] = root
        return report
    finally:
        if not opts.keep:
            shutil.rmtree(root, ignore_errors=True)


def print_human(report):
    for event, row in report["registry"].items():
        print("registry %-20s %s" % (event, "ok" if row["ok"] else "MISMATCH: " + row["detail"]))
        if row.get("note"):
            print("         note (not a failure): " + row["note"])
    for p in report["payloads"]:
        print("%s %-20s %-38s direct %7.1f ms  dispatched %7.1f ms"
              % ("PASS" if p["ok"] else "FAIL", p["event"], p["label"], p["direct_ms"],
                 p["dispatched_ms"]))
        for line in p["mismatches"]:
            print("     - " + line)
    live = report["live"]
    print("live roots: " + ("untouched" if live["ok"] else "WRITTEN: " + ", ".join(live["files"])))
    if report["fixture"]:
        print("fixture kept at " + report["fixture"])
    print("parity: " + ("PASS" if report["ok"] else "FAIL"))


def main(argv):
    opts = parse_args(argv[1:])
    try:
        report = run(opts)
    except SetupFault as exc:
        print("hook-parity-run: %s" % exc, file=sys.stderr)
        return 2
    if opts.json:
        print(json.dumps(report, indent=1))
    else:
        print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
