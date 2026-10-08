#!/usr/bin/env python3
'store-wt-finish.py -- land the current agent-context store worktree onto `main`.\n\nEvery other repo here has a landing script; without this one, store branches\nwould be the only ones landed by hand.\n\nIt:\n  1. resolves the worktree and the main checkout (they are different trees, and\n     only the main checkout has server/.venv),\n  2. verifies the branch is committed and is not `main` itself,\n  3. confirms there is something to land,\n  4. gates on the fleet deploy gate when the branch touches server/: py_compile\n     plus the server suite, run from the main checkout\'s venv against the\n     worktree\'s code. store-precommit-gate.py\'s docstring says what a red\n     server commit costs,\n  5. rebases the branch onto `main` -- only after the gate passes, so a red suite\n     never rewrites your commits -- and prints the pre-rebase SHA,\n  6. fast-forwards `main` onto the rebased branch. No merge commit; `main` is\n     only ever fast-forwarded, never rewritten.\n\nIt does not push, deliberately. The daemon\'s sync loop owns pushing for this\nrepo (policy), and remote daemons self-redeploy from what lands -- the\nsame division release-server.py already follows.\n\nWorktree removal is the harness\'s job: after this succeeds, ExitWorktree(remove).\n\nUsage:  python3 ~/.agent-context/global/scripts/store-wt-finish.py [worktree-path]\n        SKIP_GATE=1 to land without the server gate (says so, loudly).\n\nReproduced shell bugs (see inline comments at each site):\n  - `NOT pushing` is worded differently between the two landing paths: a period\n    and capital "The" after a merge-obs-402 landing, an en-dash-joined lower\n    case "the" after an ordinary rebase landing. The original is inconsistent\n    between the two `echo` lines; this keeps both forms rather than unifying\n    them.\n  - `dirname` of an empty string is "." (bash builtin behavior): if both the\n    fast `--git-common-dir` resolution and its fallback come back empty, the\n    reported main checkout is "." rather than an empty string.\n  - the change-size-check.py call always reads $HOME/.agent-context/global/scripts, never the\n    resolved main checkout, and its exit status is discarded unconditionally.\n\nObservations guarded: #121, #192, #273.'

import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

EM_DASH = "—"
WARN_SIGN = "⚠"
ELLIPSIS = "…"


def _git_capture(args, cwd=None):
    
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _git_stdout_only(args, cwd=None):
    
    
    return subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE, text=True)


def _git_inherit(args, cwd=None):
    
    
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run(["git"] + args, cwd=cwd)


_METRICS = []


def _metrics_emit(main_checkout, top, event_type, **fields):
    "Same as store-precommit-gate.py's _metrics_emit, which documents it."
    try:
        if not _METRICS:
            sys.dont_write_bytecode = True
            found = None
            for root in (main_checkout, top):
                candidate = os.path.join(root, "server", "src", "agent_context", "metrics.py")
                if os.path.isfile(candidate):
                    found = candidate
                    break
            if found is None:
                _METRICS.append(None)
            else:
                import importlib.util
                spec = importlib.util.spec_from_file_location("agent_context_metrics", found)
                module = importlib.util.module_from_spec(spec)
                sys.modules["agent_context_metrics"] = module
                spec.loader.exec_module(module)
                _METRICS.append(module)
        if _METRICS[0] is not None:
            _METRICS[0].emit(event_type, **fields)
    except (Exception, SystemExit):
        pass


def _change_name(tree):
    'The worktree directory name when the tree sits under a `worktrees` directory.'
    parts = os.path.normpath(tree).split(os.sep)
    if "worktrees" in parts:
        i = len(parts) - 1 - parts[::-1].index("worktrees")
        if i + 1 < len(parts):
            return re.sub(r"[^A-Za-z0-9._-]", "-", parts[i + 1])[:64].lstrip("-.") or None
    return None


def _pytest_counts(output):
    "n, failed, skipped and reruns from pytest's last summary line, or {} without one."
    for line in reversed(output.splitlines()):
        if re.search(r"\bin \d+(\.\d+)?s\b", line) and re.search(
                r"\d+ (passed|failed|skipped|errors?|rerun)", line):
            found = {}
            for number, word in re.findall(r"(\d+) (passed|failed|skipped|errors?|rerun)", line):
                key = "failed" if word.startswith("error") else word
                found[key] = found.get(key, 0) + int(number)
            return {"n": found.get("passed", 0), "failed": found.get("failed", 0),
                    "skipped": found.get("skipped", 0), "reruns": found.get("rerun", 0)}
    return {}


_GATE_RECORD = []


def _load_gate_record(main_checkout, top):
    "As store-precommit-gate.py's _load_gate_record, except that the module is loaded\n    from whichever tree has it: the main checkout, else this branch, since a branch\n    that adds the module has it before it lands."
    if not _GATE_RECORD:
        found = None
        for root in (main_checkout, top):
            candidate = os.path.join(root, "server", "src", "agent_context", "gate_record.py")
            if os.path.isfile(candidate):
                found = os.path.join(root, "server", "src")
                break
        if found is None:
            _GATE_RECORD.append(None)
        else:
            if found not in sys.path:
                sys.path.insert(0, found)
            try:
                import importlib
                _GATE_RECORD.append(importlib.import_module("agent_context.gate_record"))
            except Exception:
                _GATE_RECORD.append(None)
    return _GATE_RECORD[0]


def _run_teed(argv, cwd):
    'Run argv, passing its stdout through to ours as it arrives. Returns (exit code, the\n    last 8 KB of that output).'
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE)
    fd = proc.stdout.fileno()
    tail = b""
    passing = True
    while True:
        if select.select([fd], [], [], 0.2)[0]:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            if passing:
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except (OSError, AttributeError, ValueError):
                    passing = False      
            tail = (tail + chunk)[-8192:]
        elif proc.poll() is not None:
            break                        
    proc.wait()
    return proc.returncode, tail.decode("utf-8", "replace")


def _poke_daemon():
    "Ask this host's daemon for a cycle now (policy). A poked cycle adopts landed server code\n    before its network half, so a landing reaches the daemon, and through it every relay, in\n    seconds. The daemon looks for this file once a second; a hook uses the same file."
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    path = os.path.join(base, "agent-context", "sync-requested")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        os.utime(path, None)
    except OSError:
        pass


def _emit_landed(main_checkout, top, change, touches_server):
    'One `landed` event. ref is the newest commit that touched server/ (what daemons\n    adopt), else the tip of main; ok says whether daemons are expected to adopt it.\n    Pokes the daemon too: every landing path calls this.'
    _poke_daemon()
    try:
        if touches_server:
            args = ["-C", main_checkout, "log", "-1", "--format=%h", "--abbrev=7", "main", "--", "server/"]
        else:
            args = ["-C", main_checkout, "rev-parse", "--short=7", "main"]
        ref = _git_stdout_only(args).stdout.strip()[:7]
        _metrics_emit(main_checkout, top, "landed", ref=ref, change=change, ok=touches_server)
    except Exception:
        pass


_UNMERGED_CODES = ("DD", "AU", "UD", "UA", "DU", "AA", "UU")
_LOCK_RECORD_PREFIX = "global" + os.sep + "test-locks" + os.sep


def _file_sha256(path):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _resolve_lock_record_conflict():
    "L4: a rebase conflict where every unmerged path is a lock\n    record (global/test-locks/*.json) is resolved without asking, by the record's own claim: keep\n    whichever side's sha256 matches the file it names in the current tree, drop the record when\n    neither side does (the lock-record-matches-file invariant then reports that drift). A\n    conflict that touches anything else is left exactly as before -- untouched, unresolved.\n\n    Why: a lock record is a claim about another file's content, never content contributed by a\n    person, so there is nothing here for a person to reconcile by hand. The usual case is a `DU`\n    from an approved unlock on one side crossing a re-lock on the other.\n\n    Returns True and stages the resolution (leaving the rebase mid-step, ready for\n    `git rebase --continue`) when every conflicted path qualified; False, with nothing touched,\n    otherwise."
    status = _git_stdout_only(["status", "--porcelain=v1"])
    conflicted = [line[3:] for line in status.stdout.splitlines() if line[:2] in _UNMERGED_CODES]
    if not conflicted or not all(p.startswith(_LOCK_RECORD_PREFIX) and p.endswith(".json")
                                 for p in conflicted):
        return False
    for rel in conflicted:
        candidates = []
        for stage in ("2", "3"):
            show = _git_capture(["show", ":%s:%s" % (stage, rel)])
            if show.returncode != 0:
                continue
            try:
                doc = json.loads(show.stdout)
            except ValueError:
                continue
            if isinstance(doc, dict) and isinstance(doc.get("path"), str):
                candidates.append((show.stdout, doc))
        target = candidates[0][1]["path"] if candidates else None
        current_sha = _file_sha256(target) if target else None
        match = next((body for body, doc in candidates if doc.get("sha256") == current_sha), None)
        if match is not None:
            with open(rel, "w", encoding="utf-8") as fh:
                fh.write(match)
            _git_capture(["add", "--", rel])
        else:
            _git_capture(["rm", "-f", "--", rel])
    return True


def main(argv):
    
    
    
    
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    unknown = [a for a in argv if a.startswith("-")]
    if unknown:
        print("store-wt-finish: unknown flag %s" % unknown[0], file=sys.stderr)
        return 2
    wt = argv[0] if argv else os.environ.get("PWD", os.getcwd())

    try:
        os.chdir(wt)
    except OSError:
        print("store-wt-finish: no such path: %s" % wt, file=sys.stderr)
        return 1

    top_proc = _git_capture(["rev-parse", "--show-toplevel"])
    if top_proc.returncode != 0:
        print("store-wt-finish: %s is not a git repository" % wt, file=sys.stderr)
        return 1
    top = top_proc.stdout.strip()

    
    common_proc = _git_capture(["rev-parse", "--path-format=absolute", "--git-common-dir"])
    common = common_proc.stdout.strip() if common_proc.returncode == 0 else ""
    if not common.startswith("/"):
        fallback_proc = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                                        stdout=subprocess.PIPE, text=True)
        common = (os.path.normpath(os.path.join(os.getcwd(), fallback_proc.stdout.strip()))
                  if fallback_proc.returncode == 0 else "")

    
    main_checkout = os.path.dirname(common) if common else "."

    
    
    
    expected = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(
        os.environ.get("HOME", ""), ".agent-context")
    if main_checkout != expected:
        print("store-wt-finish: this script is for the agent-context store only", file=sys.stderr)
        print("                 (resolved main checkout: %s)" % main_checkout, file=sys.stderr)
        return 1

    branch_proc = _git_stdout_only(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_proc.stdout.strip()
    if branch == "main":
        print("store-wt-finish: already on main %s nothing to land" % EM_DASH, file=sys.stderr)
        return 1

    
    
    status_proc = _git_stdout_only(["status", "--porcelain"])
    if status_proc.stdout.strip():
        print("store-wt-finish: worktree has uncommitted changes %s commit them first:" % EM_DASH,
              file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        subprocess.run(["git", "status", "--short"], stdout=sys.stderr)
        return 1

    
    log_proc = _git_capture(["log", "--oneline", "main..%s" % branch])
    if not log_proc.stdout.strip():
        print("store-wt-finish: %s has no commits main does not already have" % branch,
              file=sys.stderr)
        return 1
    count_proc = _git_stdout_only(["rev-list", "--count", "main..%s" % branch])
    print("store-wt-finish: landing %s commit(s) from %s" % (count_proc.stdout.strip(), branch))

    
    
    
    
    
    change_size_check = os.path.join(hp.scripts_dir(), "change-size-check.py")
    if os.path.isfile(change_size_check):
        sys.stdout.flush()
        sys.stderr.flush()
        subprocess.run([sys.executable, change_size_check, "main", os.getcwd()])

    
    diff_proc = _git_stdout_only(["diff", "--name-only", "main..%s" % branch, "--", "server/"])
    touches_server = bool(diff_proc.stdout.strip())
    change = _change_name(top) or re.sub(r"[^A-Za-z0-9._-]", "-", branch)[:64]
    if diff_proc.stdout.strip():
        if os.environ.get("SKIP_GATE", "0") == "1":
            print("store-wt-finish: %s SERVER GATE SKIPPED (SKIP_GATE=1)." % WARN_SIGN,
                  file=sys.stderr)
            print("                 If this code is red, every daemon in the fleet stops self-deploying.",
                  file=sys.stderr)
        else:
            py = os.path.join(main_checkout, "server", ".venv", "bin", "python")
            if not os.access(py, os.X_OK):
                
                
                print("store-wt-finish: BLOCKED %s no server venv at %s, so the deploy gate cannot run."
                      % (EM_DASH, py), file=sys.stderr)
                print("                 Fix: (cd '%s/server' && uv sync && uv pip install pytest)"
                      % main_checkout, file=sys.stderr)
                return 1
            print("store-wt-finish: branch touches server/ %s running the fleet deploy gate%s"
                  % (EM_DASH, ELLIPSIS))
            server_dir = os.path.join(top, "server")
            sys.stdout.flush()
            sys.stderr.flush()
            gate_ok = False
            gate_counts = {}
            gate_began = time.time()
            _metrics_emit(main_checkout, top, "gate_start", ref="wt-finish", change=change)
            gate_record = _load_gate_record(main_checkout, top)
            try:
                compileall_proc = subprocess.run([py, "-m", "compileall", "-q", "src"],
                                                  cwd=server_dir, stdout=subprocess.DEVNULL)
                if compileall_proc.returncode == 0:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    if gate_record is not None:
                        gate_ok, gate_counts, _pytest_out, _cached = gate_record.run_gate(
                            server_dir, py, by="wt-finish", stream=True)
                    else:
                        pytest_rc, pytest_out = _run_teed([py, "-m", "pytest", "-q"], server_dir)
                        gate_ok = pytest_rc == 0
                        gate_counts = _pytest_counts(pytest_out)
            except OSError:
                gate_ok = False
            _metrics_emit(main_checkout, top, "gate_end", ref="wt-finish", change=change, ok=gate_ok,
                          dur_ms=int((time.time() - gate_began) * 1000), **gate_counts)
            if not gate_ok:
                print("store-wt-finish: GATE FAILED %s nothing landed." % EM_DASH, file=sys.stderr)
                return 1
            print("store-wt-finish: gate passed.")

    
    
    
    integrates = []
    refs_proc = _git_capture(["for-each-ref", "--format=%(refname:short)", "refs/remotes"])
    if refs_proc.returncode == 0:
        for ref in refs_proc.stdout.splitlines():
            if not re.search(r"/(main|master)$", ref):
                continue
            carried = subprocess.run(["git", "merge-base", "--is-ancestor", ref, branch],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if carried.returncode != 0:
                continue  
            has_it = subprocess.run(["git", "merge-base", "--is-ancestor", ref, "main"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if has_it.returncode == 0:
                continue  
            integrates.append(ref)

    if integrates:
        print("store-wt-finish: branch carries%s, landing by MERGE rather than rebase (policy)."
              % "".join(" %s" % r for r in integrates))
        merge_proc = _git_inherit(["-C", main_checkout, "merge", "--no-ff", "--no-edit", branch])
        if merge_proc.returncode != 0:
            print("store-wt-finish: the merge into main conflicted, which means main moved", file=sys.stderr)
            print("                 under you. Resolve in %s and commit there." % main_checkout,
                  file=sys.stderr)
            return 1
        
        
        for ref in integrates:
            check_proc = _git_inherit(["-C", main_checkout, "merge-base", "--is-ancestor", ref, "HEAD"])
            if check_proc.returncode != 0:
                print("store-wt-finish: landed, but %s is still not an ancestor of main." % ref,
                      file=sys.stderr)
                print("                 The daemon will conflict again. Do not report this fixed.",
                      file=sys.stderr)
                return 1
        landed_proc = _git_stdout_only(["-C", main_checkout, "rev-parse", "--short", "HEAD"])
        landed_sha = landed_proc.stdout.strip()
        _emit_landed(main_checkout, top, change, touches_server)
        print("store-wt-finish: landed %s onto main at %s (merge commit)" % (branch, landed_sha))
        print("store-wt-finish: NOT pushing. The daemon's sync loop does that.")
        print("store-wt-finish: now remove the worktree (ExitWorktree action=remove).")
        print("store-wt-finish: before reporting this as shipped, confirm a mirror has it:")
        print("                 python3 ~/.agent-context/global/scripts/store-landed.py %s" % landed_sha)
        return 0

    
    
    pre_proc = _git_stdout_only(["rev-parse", "HEAD"])
    pre = pre_proc.stdout.strip()
    print("store-wt-finish: pre-rebase HEAD was %s" % pre)
    rebase_proc = _git_inherit(["rebase", "main"])
    while rebase_proc.returncode != 0:
        if not _resolve_lock_record_conflict():
            print("store-wt-finish: rebase hit a conflict. Resolve it in %s, then re-run." % wt,
                  file=sys.stderr)
            print("                 The pre-rebase commits are still at %s." % pre, file=sys.stderr)
            return 1
        print("store-wt-finish: lock-record conflict resolved by sha256, continuing the rebase")
        rebase_proc = _git_inherit(["rebase", "--continue"])

    
    ff_proc = _git_inherit(["-C", main_checkout, "merge", "--ff-only", branch])
    if ff_proc.returncode != 0:
        print("store-wt-finish: main could not be fast-forwarded %s it moved under us." % EM_DASH,
              file=sys.stderr)
        print("                 Re-run: the rebase above will replay onto the new tip.", file=sys.stderr)
        return 1

    landed_proc = _git_stdout_only(["-C", main_checkout, "rev-parse", "--short", "HEAD"])
    landed_sha = landed_proc.stdout.strip()
    _emit_landed(main_checkout, top, change, touches_server)
    print("store-wt-finish: landed %s onto main at %s" % (branch, landed_sha))
    print("store-wt-finish: NOT pushing %s the daemon's sync loop does that." % EM_DASH)
    print("store-wt-finish: now remove the worktree (ExitWorktree action=remove).")
    
    
    
    print("store-wt-finish: before reporting this as shipped, confirm a mirror has it:")
    print("                 python3 ~/.agent-context/global/scripts/store-landed.py %s" % landed_sha)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
