#!/usr/bin/env python3
'store-precommit-gate.py — installed as the agent-context store\'s .git/hooks/pre-commit.\n\nWhy: the store\'s server/ tree is the fleet\'s deploy gate. Every daemon\nruns run_gate() (py_compile + pytest) before self-redeploying, so a commit that lands a\nred test stops every node in the fleet from redeploying -- including past that commit\'s\nown fix. release-server.py gates properly, but it is a convention, not a chokepoint:\nguard-git-write exempts the store entirely, so any session can `git commit -- server/`\nstraight past it.\n\nThis hook makes the gate unbypassable-by-accident: it runs on the only thing every\nroute shares, the commit itself.\n\nFast no-op when the commit touches no server/ path, since the daemon\'s commit on write\nnever stages server/.\n\nSame exit codes, stdout, stderr and effects as the shell original; shell quirks\n(word-splitting on staged file lists, glob-no-match falling back to a literal\npattern, "bound" doing nothing when neither timeout nor gtimeout is on PATH) are kept,\nnot fixed.\n\nObservations guarded: #121.'

import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple




SUITE_TIMEOUT_SECS = 600


def _capture(argv, cwd=None, discard_stderr=False, merge_stderr=False, env=None):
    
    'Run argv, return (stdout, returncode) with trailing newlines stripped, the way a\n    bash command substitution $(...) strips them. stderr is inherited unless told\n    otherwise, matching each call site in the original.'
    if discard_stderr:
        stderr = subprocess.DEVNULL
    elif merge_stderr:
        stderr = subprocess.STDOUT
    else:
        stderr = None
    try:
        proc = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=stderr, env=env)
    except OSError as exc:
        
        
        if not merge_stderr and not discard_stderr:
            sys.stderr.write("%s\n" % exc)
        return "", 1
    out = proc.stdout.decode("utf-8", "replace") if proc.stdout is not None else ""
    return out.rstrip("\n"), proc.returncode


def _dirname(path):
    
    'coreutils/bash dirname: dirname of "" or a path with no slash is ".".'
    if not path:
        return "."
    d = os.path.dirname(path)
    return d if d else "."


def _bound_prefix():
    
    '`bound() { command -v timeout ... elif gtimeout ... else run unbounded }`.'
    if shutil.which("timeout"):
        return ["timeout"]
    if shutil.which("gtimeout"):
        return ["gtimeout"]
    return None


def _printf_nl_pipe(text, n, mode):
    
    '`printf \'%s\\n\' "$text" | head -N` / `| tail -N`.'
    lines = (text + "\n").splitlines(True)
    return "".join(lines[:n] if mode == "head" else lines[-n:])


def _raw_tail(text, n):
    
    '`printf \'%s\' "$text" | tail -N` (no newline appended).'
    lines = text.splitlines(True)
    return "".join(lines[-n:])


_METRICS = []  


def _metrics_emit(store, tree, event_type, **fields):
    
    'Record one event in the metrics log. Loads server/src/agent_context/metrics.py by\n    path from the main checkout (else this tree), writes no bytecode, and ignores every\n    failure, so the gate behaves the same with or without it.'
    try:
        if not _METRICS:
            sys.dont_write_bytecode = True
            found = None
            for root in (store, tree):
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
    import re
    parts = os.path.normpath(tree).split(os.sep)
    if "worktrees" in parts:
        i = len(parts) - 1 - parts[::-1].index("worktrees")
        if i + 1 < len(parts):
            return re.sub(r"[^A-Za-z0-9._-]", "-", parts[i + 1])[:64].lstrip("-.") or None
    return None


def _pytest_counts(output):
    
    "n, failed, skipped and reruns from pytest's last summary line, or {} without one."
    import re
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


def _load_gate_record(store):
    
    "agent_context.gate_record (policy): the shared per-machine gate-pass cache,\n    loaded from the store's server/src. None on an older checkout that predates the\n    module: the caller then runs pytest itself, the same as today, and\n    _pytest_counts above still parses the output."
    if not _GATE_RECORD:
        try:
            src = os.path.join(store, "server", "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            import importlib
            _GATE_RECORD.append(importlib.import_module("agent_context.gate_record"))
        except Exception:
            _GATE_RECORD.append(None)
    return _GATE_RECORD[0]


def main():
    
    sys.stdout.flush()
    sys.stderr.flush()
    tree, rc = _capture(["git", "rev-parse", "--show-toplevel"], discard_stderr=True)
    if rc != 0:
        return 0
    if not tree:
        return 0

    common, _rc = _capture(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], discard_stderr=True
    )
    if not common.startswith("/"):
        common = ""
        if os.path.isdir(tree):
            inner, inner_rc = _capture(["git", "rev-parse", "--git-common-dir"], cwd=tree, discard_stderr=True)
            if inner_rc == 0 and inner:
                candidate = inner if os.path.isabs(inner) else os.path.normpath(os.path.join(tree, inner))
                if os.path.isdir(candidate):
                    common = candidate

    store = _dirname(common)
    if not os.access(os.path.join(store, "server", ".venv", "bin", "python"), os.X_OK):
        store = tree  

    
    
    
    
    
    
    
    staged_py_out, _rc = _capture(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "global/*.py", "global/**/*.py"]
    )
    staged_py = staged_py_out.split()
    if staged_py:
        bad = ""
        for f in staged_py:
            fpath = os.path.join(tree, f)
            if not os.path.isfile(fpath):
                continue  
            err, py_rc = _capture([sys.executable, "-m", "py_compile", fpath], merge_stderr=True)
            if py_rc != 0:
                bad += "%s: %s\n" % (f, err)
        if bad:
            sys.stderr.write("pre-commit: BLOCKED — a staged global python file does not compile:\n")
            sys.stderr.write(_raw_tail(bad, 20))
            sys.stderr.write("            These project into ~/.claude on every machine, so a broken one\n")
            sys.stderr.write("            fails at SessionStart on hosts where nobody reads stderr.\n")
            sys.stderr.write("            Override (only if you know why): AGENT_CONTEXT_SKIP_GATE=1 git commit ...\n")
            if os.environ.get("AGENT_CONTEXT_SKIP_GATE", "0") != "1":
                return 1

    
    
    
    
    
    
    
    
    
    
    
    
    
    staged_glue_out, _rc = _capture(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "global/hooks/*", "global/scripts/*"],
        discard_stderr=True,
    )
    staged_glue = staged_glue_out.split()
    if staged_glue:
        inv = os.path.join(store, "global", "scripts", "invariant-check.py")
        if os.path.isfile(inv):
            paths = [os.path.join(tree, f) for f in staged_glue if os.path.isfile(os.path.join(tree, f))]
            if paths:
                out, inv_rc = _capture([sys.executable, inv, "--paths"] + paths, merge_stderr=True)
                if inv_rc != 0:
                    sys.stderr.write("pre-commit: ⚠ a staged file violates an invariant this store already paid for:\n")
                    sys.stderr.write(_printf_nl_pipe(out, 30, "head"))
                    sys.stderr.write("            Not blocking. Fix it, or say why it is exempt in the check's allow list.\n")

    
    server_out, _rc = _capture(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "server/"])
    if not any(server_out.splitlines()):
        return 0

    
    
    if os.environ.get("AGENT_CONTEXT_GATE_PASSED", "0") == "1":
        return 0

    if os.environ.get("AGENT_CONTEXT_SKIP_GATE", "0") == "1":
        sys.stderr.write("pre-commit: ⚠ GATE SKIPPED for server/ (AGENT_CONTEXT_SKIP_GATE=1).\n")
        sys.stderr.write("            If this code is red, every daemon in the fleet stops self-deploying.\n")
        return 0

    py = os.path.join(store, "server", ".venv", "bin", "python")
    if not os.access(py, os.X_OK):
        sys.stderr.write("pre-commit: BLOCKED — no server venv at %s, so the deploy gate cannot run.\n" % py)
        sys.stderr.write("            Fix:      (cd '%s/server' && uv sync && uv pip install pytest)\n" % store)
        sys.stderr.write("            Override: AGENT_CONTEXT_SKIP_GATE=1 git commit ...\n")
        return 1

    
    _out, quiet_rc = _capture(["git", "diff", "--quiet", "--", "server/"])
    if quiet_rc != 0:
        sys.stderr.write("pre-commit: note — server/ has unstaged changes; the gate runs against the working tree.\n")

    sys.stderr.write("pre-commit: server/ touched — running the fleet deploy gate…\n")
    sys.stdout.flush()
    sys.stderr.flush()

    change = _change_name(tree)
    gate_began = time.time()
    _metrics_emit(store, tree, "gate_start", ref="precommit", change=change)
    server_dir = os.path.join(tree, "server")
    agent_context_dir = os.path.join(server_dir, "src", "agent_context")
    if os.path.isdir(agent_context_dir):
        names = sorted(n for n in os.listdir(agent_context_dir) if n.endswith(".py"))
    else:
        names = []
    file_args = ["src/agent_context/%s" % n for n in names] if names else ["src/agent_context/*.py"]

    bound = _bound_prefix()
    py_compile_argv = (bound + ["60"] if bound else []) + [py, "-m", "py_compile"] + file_args
    out, py_compile_rc = _capture(py_compile_argv, cwd=server_dir, merge_stderr=True)
    if py_compile_rc != 0:
        _metrics_emit(store, tree, "gate_end", ref="precommit", change=change, ok=False,
                      dur_ms=int((time.time() - gate_began) * 1000))
        sys.stderr.write("pre-commit: BLOCKED — py_compile failed:\n")
        sys.stderr.write(_printf_nl_pipe(out, 20, "tail"))
        return 1

    
    
    
    pytest_env = dict(os.environ)
    
    
    
    
    
    for name in [n for n in pytest_env if n.startswith("GIT_")]:
        del pytest_env[name]
    pytest_env["PYTHONPATH"] = os.path.join(server_dir, "src")
    sys.stdout.flush()
    sys.stderr.flush()
    gate_record = _load_gate_record(store)
    if gate_record is not None:
        print("pre-commit: server suite: a recorded pass for this tree skips it, else it "
              "runs (up to %ds)" % SUITE_TIMEOUT_SECS, file=sys.stderr, flush=True)
        pytest_ok, counts, out, _cached = gate_record.run_gate(
            server_dir, py, env=pytest_env, timeout=SUITE_TIMEOUT_SECS, by="precommit")
        pytest_rc = 0 if pytest_ok else 1
    else:
        bound = _bound_prefix()
        pytest_argv = ((bound + [str(SUITE_TIMEOUT_SECS)] if bound else [])
                       + [py, "-m", "pytest", "-q"])
        out, pytest_rc = _capture(pytest_argv, cwd=server_dir, merge_stderr=True, env=pytest_env)
        counts = _pytest_counts(out)
    _metrics_emit(store, tree, "gate_end", ref="precommit", change=change, ok=pytest_rc == 0,
                  dur_ms=int((time.time() - gate_began) * 1000), **counts)
    if pytest_rc != 0:
        sys.stderr.write("pre-commit: BLOCKED — the server test suite is RED. Committing this would stop\n")
        sys.stderr.write("            every daemon in the fleet from self-deploying.\n")
        sys.stderr.write(_printf_nl_pipe(out, 25, "tail"))
        sys.stderr.write("            Override (only if you know why): AGENT_CONTEXT_SKIP_GATE=1 git commit ...\n")
        return 1

    sys.stderr.write("pre-commit: gate passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
