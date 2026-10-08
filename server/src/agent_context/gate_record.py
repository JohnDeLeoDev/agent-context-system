'A server/ tree is tested once (policy).\n\nBefore this, the full suite (~4 min) ran in three places that kept no memory of a\npass: the store\'s pre-commit hook, `store-wt-finish.py` at landing, and the\ndaemon\'s own `run_gate` before a self-redeploy. Each of the first two also carried\nits own copy of the pytest runner and the summary-line parser.\n\nThis module is the one place that:\n\n  - names what a gate verdict depends on (`gate_key`), wider than\n    `daemon._code_fingerprint()`\'s release identity: it adds `pyproject.toml`,\n    `uv.lock` and the target interpreter\'s major.minor, because a dependency or\n    interpreter change can flip a suite the code fingerprint alone calls unchanged;\n  - keeps a per-machine record of keys that already passed, in the state dir\n    (`paths.state_dir()`), so a second caller with the same key skips the suite\n    entirely;\n  - runs `python -m pytest -q` and parses its summary line, once, for every\n    caller that does not already have its own reason to run pytest differently\n    (the daemon\'s `run_gate` keeps its own runner — rerun-on-failure, faulthandler,\n    a service environment — but consults and records through this module\'s key and\n    record, by="daemon").\n\nStandard library only, importable by a plain script via\n`sys.path.insert(0, "<store>/server/src")` then `import agent_context.gate_record`.'
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

from . import paths

RECORD_NAME = "gate-passes.json"
_MAX_RECORDS = 200


def gate_files(server_dir: str | Path) -> list[Path]:
    'Every file a gate verdict for `server_dir` depends on for content: the\n    package\'s own modules plus its test suite. The one definition daemon\'s\n    `_gate_files()` also calls, so there is a single list of what "the code"\n    means to a gate.'
    server_dir = Path(server_dir)
    files = sorted((server_dir / "src" / "agent_context").glob("*.py"))
    tests = server_dir / "tests"
    if tests.is_dir():
        files += sorted(tests.glob("*.py"))
    return files


_PY_NAME = re.compile(r"[\"'/]([A-Za-z0-9_][A-Za-z0-9_.-]*\.py)[\"']")
_IMPORT = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
_TASK = re.compile(r"store_task\.(?:run|main_or_forward)\(\s*[\"']([A-Za-z0-9_.-]+)[\"']")


def global_test_inputs(server_dir: str | Path) -> list[Path]:
    'Server tests copy and execute some of those files (entity-root-pages.py,\n    context-audit-autorun.py, token-usage-collect.py), so a change to one can turn a\n    passing suite red without touching server/. Keyed on server/ alone, the recorded\n    pass let such a change ship: test_token_collector failed after token-usage-collect.py\n    changed. Only the files the tests name (a quoted `<name>.py`) and their local\n    imports count, so an unrelated script edit still reuses the record. Not part of\n    `gate_files`, which also decides when the daemon has newer code to deploy.'
    server_dir = Path(server_dir).resolve()
    roots = [server_dir.parent / "global" / d for d in ("scripts", "hooks")]
    by_name = {p.name: p for r in roots if r.is_dir() for p in r.glob("*.py")}
    tests = server_dir / "tests"
    wanted: set[str] = set()
    for t in (sorted(tests.glob("*.py")) if tests.is_dir() else []):
        with contextlib.suppress(OSError, UnicodeDecodeError):
            wanted |= {n for n in _PY_NAME.findall(t.read_text(encoding="utf-8"))
                       if n in by_name}
    seen: set[str] = set()
    while wanted - seen:
        name = sorted(wanted - seen)[0]
        seen.add(name)
        with contextlib.suppress(OSError, UnicodeDecodeError):
            text = by_name[name].read_text(encoding="utf-8")
            wanted |= {m + ".py" for m in _IMPORT.findall(text) + _TASK.findall(text)
                       if m + ".py" in by_name}
    return [by_name[n] for n in sorted(seen)]


_PY_VERSION_CACHE: dict[str, str] = {}


def _python_version(python: str) -> str | None:
    "`python`'s major.minor, queried once per interpreter path and cached. None if\n    `python` cannot be run or prints nothing parseable.\n\n    Every caller of `gate_key` must query the interpreter that will run pytest, not\n    its own: store-wt-finish.py runs under the system python3 while the suite it\n    gates runs under the server venv's python, so hashing `sys.version_info` of the\n    calling process gave two different keys for the same tree and target\n    interpreter."
    cached = _PY_VERSION_CACHE.get(python)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run([python, "-c", _PROBE], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.split()
    if not lines:
        return None
    _PY_VERSION_CACHE[python] = lines[0]
    _XDIST_CACHE[python] = lines[1:2] == ["xdist"]
    return lines[0]




_PROBE = ("import sys, importlib.util; print('%d.%d' % sys.version_info[:2]); "
          "print('xdist' if importlib.util.find_spec('xdist') else 'serial')")
_XDIST_CACHE: dict[str, bool] = {}


def parallel_args(python: str) -> list[str]:
    "This process's own interpreter is asked directly and never cached: the daemon\n    syncs its venv between computing the gate key and running the suite, so a cached\n    answer would miss an xdist that sync just installed."
    try:
        own = os.path.samefile(python, sys.executable)
    except OSError:
        own = False
    if own:
        import importlib
        import importlib.util
        importlib.invalidate_caches()
        found = importlib.util.find_spec("xdist") is not None
    else:
        _python_version(python)
        found = _XDIST_CACHE.get(python, False)
    return ["-n", "auto"] if found else []


def gate_key(server_dir: str | Path, python: str | None = None) -> str | None:
    "sha256 over everything a gate verdict for `server_dir` depends on: every\n    file from `gate_files` (name + bytes), `pyproject.toml`, `uv.lock`, and the\n    major.minor of `python` (the interpreter that will run pytest). None if any of\n    it cannot be read, or `python` cannot be queried.\n\n    `python=None` uses this process's own `sys.version_info`, for a caller that is\n    itself the interpreter running the suite (the daemon, the CLI). A caller that\n    launches a different interpreter as a subprocess must pass its path, or the key\n    will not match another caller gating the same tree with the same target\n    interpreter.\n\n    Wider than `daemon._code_fingerprint()`: a dependency bump (`uv.lock`) or an\n    interpreter change can turn a passing suite red without touching a single\n    package or test file."
    server_dir = Path(server_dir)
    h = hashlib.sha256()
    try:
        for f in gate_files(server_dir):
            h.update(f.name.encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
        for name in ("pyproject.toml", "uv.lock"):
            p = server_dir / name
            h.update(name.encode())
            h.update(b"\0")
            h.update(p.read_bytes() if p.is_file() else b"<missing>")
            h.update(b"\0")
        for f in global_test_inputs(server_dir):
            h.update(b"global/" + f.parent.name.encode() + b"/" + f.name.encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    except OSError:
        return None
    if python is None:
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
    else:
        version = _python_version(python)
        if version is None:
            return None
    h.update(version.encode())
    return h.hexdigest()


def _record_path() -> Path:
    return paths.state_dir() / RECORD_NAME


def _load_records() -> dict:
    try:
        with open(_record_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def has_passed(key: str) -> dict | None:
    'The recorded pass for `key`, or None. Never a recorded failure — failures\n    are not recorded at all.'
    rec = _load_records().get(key)
    return rec if isinstance(rec, dict) else None


def record_pass(key: str, counts: dict, by: str) -> None:
    'Record that `key` passed, capped to the newest `_MAX_RECORDS`. Written\n    atomically; a corrupt or missing file is treated as empty, not an error.'
    records = _load_records()
    records[key] = {"ts": time.time(), "counts": counts, "by": by}
    if len(records) > _MAX_RECORDS:
        newest = sorted(records.items(), key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0,
                        reverse=True)[:_MAX_RECORDS]
        records = dict(newest)
    paths.write_atomic(_record_path(), json.dumps(records))


def pytest_counts(output: str) -> dict:
    "n, failed, skipped and reruns from pytest's last summary line, or {}\n    without one. The one parser, replacing the copy each of store-precommit-gate.py\n    and store-wt-finish.py used to keep."
    import re
    for line in reversed(output.splitlines()):
        if re.search(r"\bin \d+(\.\d+)?s\b", line) and re.search(
                r"\d+ (passed|failed|skipped|errors?|rerun)", line):
            found: dict = {}
            for number, word in re.findall(r"(\d+) (passed|failed|skipped|errors?|rerun)", line):
                key = "failed" if word.startswith("error") else word
                found[key] = found.get(key, 0) + int(number)
            return {"n": found.get("passed", 0), "failed": found.get("failed", 0),
                    "skipped": found.get("skipped", 0), "reruns": found.get("rerun", 0)}
    return {}


def _run_teed(argv: list[str], cwd: str, env: dict | None) -> tuple[int, str]:
    "Run argv, passing its stdout through to ours as it arrives (store-wt-finish's\n    behavior). Returns (exit code, the last 8 KB of that output)."
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, env=env)
    assert proc.stdout is not None
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


def run_gate(server_dir: str | Path, python: str, *, env: dict | None = None,
            timeout: float | None = None, by: str, stream: bool = False,
            force: bool = False) -> tuple[bool, dict, str, bool]:
    "Run `python -m pytest -q` in `server_dir`, unless `gate_key(server_dir, python)`\n    already has a recorded pass. Then skip the suite and return that pass.\n\n    Returns (ok, counts, output, cached). `env` replaces the subprocess\n    environment when given (a caller sets PYTHONPATH there for a worktree).\n    `timeout` bounds the run in seconds; a timeout counts as a failure with\n    counts `{}`, never recorded. `stream=True` tees stdout live as it arrives\n    (store-wt-finish's behavior) instead of only capturing it; a timeout is not\n    enforced in that mode, matching the caller it replaces. `force=True` skips\n    the cached-pass check and always runs the suite (the CLI's --force).\n\n    A pass is recorded under `by` (precommit|wt-finish|daemon|cli); a failure\n    never is."
    key = gate_key(server_dir, python)
    if key is not None and not force:
        rec = has_passed(key)
        if rec is not None:
            return True, dict(rec.get("counts") or {}), "", True
    argv = [python, "-m", "pytest", "-q", *parallel_args(python)]
    if stream:
        rc, output = _run_teed(argv, str(server_dir), env)
    else:
        try:
            proc = subprocess.run(argv, cwd=str(server_dir), stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            out = e.output
            output = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            return False, {}, output, False
        rc = proc.returncode
        output = (proc.stdout or b"").decode("utf-8", "replace")
    ok = rc == 0
    counts = pytest_counts(output)
    if ok and key is not None:
        record_pass(key, counts, by)
    return ok, counts, output, False


def main(argv: list[str] | None = None) -> int:
    '`python -m agent_context.gate_record [--force] [<server_dir>]`.'
    argv = sys.argv[1:] if argv is None else argv
    force = "--force" in argv
    positional = [a for a in argv if a != "--force"]
    server_dir = Path(positional[0]) if positional else Path(__file__).resolve().parents[2]
    key = gate_key(server_dir, sys.executable)
    ok, counts, output, cached = run_gate(server_dir, sys.executable, by="cli", force=force)
    if cached:
        rec = has_passed(key) if key is not None else None
        print(f"gate: already passed {key[:12] if key else '?'} "
              f"({rec.get('ts') if rec else '?'}, by {rec.get('by') if rec else '?'})")
        return 0
    print(output)
    print(f"gate: {'passed' if ok else 'FAILED'} {counts}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
