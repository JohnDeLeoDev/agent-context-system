"These scripts scan the whole tree (invariants, prose sweeps, secret scans, byte hashes,\ntest-lock records, the janitor's compaction), so they are server-side logic by nature. The\ndaemon runs them here and nowhere else: `run_store_task` is their one entry point, and a\nscript started anywhere else forwards its argv to it (global/scripts/store_task.py). No\nclient process opens a store file, one MCP tool covers every script (the tool budget,\npolicy), and a relay machine can run them, which it could not before.\n\nA task runs `global/scripts/<task>.py` from the store root with the daemon's interpreter\nand AGENT_CONTEXT_SERVER_TASK=1 in its environment, which is how the script knows not to\nforward again. Every file the task changes is written by the daemon, so each one goes into\nthe write ledger and is committed like any MCP write (policy); the index is reloaded when a\ntask changed anything, so the next read sees it."
from __future__ import annotations

import contextlib
import hashlib
import os
import select
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass

from . import task_worker

SERVER_TASK_ENV = "AGENT_CONTEXT_SERVER_TASK"
MAX_OUTPUT = 1024 * 1024          
MAX_STDIN = 4 * 1024 * 1024


@dataclass(frozen=True)
class Task:
    timeout: int                  
    
    
    readonly: frozenset[str] = frozenset()



TASKS: dict[str, Task] = {
    "config-secret-scan": Task(300),
    "context-graph-search-bench": Task(900),
    "context-inventory-batch": Task(600),
    "context-inventory-build": Task(600),
    "context_budget": Task(120),
    "entity-root-pages": Task(300),
    "freshness-diff": Task(300),
    "hook-registration-probe": Task(120),
    "invariant-check": Task(600),
    "mcp-manifest": Task(30, readonly=frozenset({"read"})),
    "observation-coverage": Task(300),
    "plain-language-sweep": Task(300),
    "prose-sweep": Task(300),
    "scaffold-regression": Task(300),
    "store-compact": Task(300),
    "store-landed": Task(120),
    "store-move-docs": Task(600),
    "store-orphan-probe": Task(120),
    "store-reader-graph": Task(600),
    "store-state": Task(60),
    "test-lock": Task(300, readonly=frozenset({"status", "check"})),
    "test-lock-consent": Task(60),
}



_RUNNING: dict[str, threading.Lock] = {name: threading.Lock() for name in TASKS}


class TaskRefused(ValueError):
    'A request this tool will not run; the message is safe to return.'


def _tail(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    if len(text) > MAX_OUTPUT:
        return "[... %d characters cut ...]\n" % (len(text) - MAX_OUTPUT) + text[-MAX_OUTPUT:]
    return text


def _dirty(root: str) -> dict[str, str]:
    '{store-relative path: content digest or "-" when deleted} for every path git shows\n    as changed or untracked, server/ excluded (a task never writes code).'
    proc = subprocess.run(["git", "-C", root, "status", "--porcelain", "-z",
                           "--untracked-files=all", "--", ".", ":!server"],
                          capture_output=True, check=False, timeout=60)
    out: dict[str, str] = {}
    entries = proc.stdout.split(b"\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, rel = entry[:2].decode(), entry[3:].decode("utf-8", "surrogateescape")
        if status[0] in "RC":
            i += 1                
        out[rel] = _digest(os.path.join(root, rel))
    return out


def _digest(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return "-"


class WarmRunner:
    "The daemon's one task_worker process (policy), for read-only calls: it saves the\n    Python start and imports a cold run pays. Started on first use; a call it cannot\n    serve (not started, died, bad reply) returns None and runs cold. A call past its\n    timeout kills the worker and its child, and the next call starts a new one."

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def _worker(self) -> subprocess.Popen | None:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        try:
            self._proc = subprocess.Popen(
                [sys.executable, task_worker.__file__],  
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError:
            self._proc = None
        return self._proc

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)

    def run(self, script: str, args: list, cwd: str, env: dict, stdin: str | None,
            timeout: float) -> tuple[int, bytes, bytes] | None:
        with self._lock:
            proc = self._worker()
            if proc is None or proc.stdin is None or proc.stdout is None:
                return None
            try:
                task_worker.write_message(proc.stdin, {
                    "script": script, "args": args, "cwd": cwd, "env": env,
                    "stdin": stdin or ""})
                ready, _, _ = select.select([proc.stdout], [], [], timeout)
                if not ready:
                    self._kill()
                    name = os.path.basename(script)[:-3]
                    return 124, b"", f"\n{name}: stopped after {timeout}s\n".encode()
                reply = task_worker.read_message(proc.stdout)
            except (OSError, ValueError):
                self._kill()
                return None
            if not isinstance(reply, dict) or "exit" not in reply:
                self._kill()
                return None
            return (int(reply["exit"]), str(reply.get("stdout") or "").encode("utf-8"),
                    str(reply.get("stderr") or "").encode("utf-8"))


_WARM = WarmRunner()


def validate(task: str, args: list, stdin: str | None) -> Task:
    spec = TASKS.get(task)
    if spec is None:
        raise TaskRefused(f"unknown task {task!r}; known: {', '.join(sorted(TASKS))}")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise TaskRefused("args must be a list of strings")
    if any("\0" in a for a in args):
        raise TaskRefused("args must not contain NUL")
    if stdin is not None and len(stdin.encode("utf-8", "replace")) > MAX_STDIN:
        raise TaskRefused(f"stdin is over {MAX_STDIN} bytes")
    return spec


def run(store, task: str, args: list | None = None, stdin: str | None = None,
        cwd: str | None = None) -> dict:
    "Run one task; returns {task, exit, stdout, stderr, changed} or {error}.\n\n    `cwd` is the caller's directory. The task starts there when it exists on this host\n    (a caller on ls), so a relative path argument means what it meant to the caller;\n    otherwise it starts at the store root. It is also passed as AGENT_CONTEXT_CALLER_CWD."
    args = list(args or [])
    try:
        spec = validate(task, args, stdin)
    except TaskRefused as exc:
        return {"error": str(exc)}
    root = str(store.root)
    script = os.path.join(root, "global", "scripts", task + ".py")
    if not os.path.isfile(script):
        return {"error": f"task {task!r} has no script at global/scripts/{task}.py"}
    env = {**os.environ, SERVER_TASK_ENV: "1", "AGENT_CONTEXT_STORE": root,
           "PYTHONDONTWRITEBYTECODE": "1"}
    if cwd:
        env["AGENT_CONTEXT_CALLER_CWD"] = cwd
    lock = _RUNNING[task]
    if not lock.acquire(timeout=spec.timeout):
        return {"error": f"task {task!r} is already running and did not finish in time"}
    try:
        writes = not (args and args[0] in spec.readonly)
        before = _dirty(root) if writes else {}
        start = cwd if cwd and os.path.isdir(cwd) else root
        warm = None if writes else _WARM.run(script, args, start, env, stdin, spec.timeout)
        if warm is not None:
            code, out, err = warm
        else:
            try:
                proc = subprocess.run([sys.executable, script, *args], cwd=start, env=env,
                                      input=(stdin or "").encode("utf-8"),
                                      capture_output=True, timeout=spec.timeout, check=False)
                code, out, err = proc.returncode, proc.stdout, proc.stderr
            except subprocess.TimeoutExpired as exc:
                code = 124
                out = exc.stdout or b""
                err = (exc.stderr or b"") + (f"\n{task}: stopped after {spec.timeout}s\n").encode()
        after = _dirty(root) if writes else {}
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        if changed:
            store.record_task_writes(changed)
        return {"task": task, "exit": code, "stdout": _tail(out), "stderr": _tail(err),
                "changed": changed}
    finally:
        lock.release()
