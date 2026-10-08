'Shared pytest fixtures for the agent-context server suite.\n\nEvery test runs against an ISOLATED ContextStore rooted at a per-test tmp dir —\nthe live ~/.agent-context store is never touched.'
import os
import pathlib
import shutil
import sys
import tempfile
import time

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))






try:
    import anyio  
except ModuleNotFoundError:  
    raise SystemExit(
        "\nagent-context server tests are being run under the WRONG interpreter.\n"
        f"  using : {sys.executable}\n"
        "  needs : <repo>/server/.venv/bin/python\n\n"
        "Re-run from the repo root as:\n"
        "  server/.venv/bin/python -m pytest server/tests -q\n\n"
        "(`anyio` is missing, which is what daemon.py imports. The tests are fine.)\n"
    ) from None



from hermetic_guard import (  
    _hermetic_basetemp,
    _hermetic_guard,
    _no_external_network,
    pytest_sessionfinish,
)



from slow_test_ceiling import pytest_runtest_call  

from agent_context.store import ContextStore


def _exec_allowed(directory) -> bool:
    'Whether a file created in `directory` can be executed.'
    probe = pathlib.Path(directory) / ".exec-probe"
    probe.write_text("#!/bin/sh\ntrue\n")
    probe.chmod(0o755)
    try:
        return os.access(probe, os.X_OK)
    finally:
        probe.unlink()


def _sweep(base, max_age=6 * 3600) -> None:
    'Drop fallback directories left by a run that was killed before teardown.\n\n    s1 and s2 take the fallback branch on EVERY gate run, since their /tmp is\n    permanently noexec, so a leak there accumulates instead of being a one-off.'
    now = time.time()
    for entry in base.iterdir():
        try:
            if now - entry.stat().st_mtime > max_age:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


@pytest.fixture
def exec_capable_tmp_path(tmp_path):
    "`tmp_path`, or a directory under ~/.cache when tmp_path's mount forbids execution.\n\n    The fallback is probed too, and the tests SKIP when no directory can hold an\n    executable file. A skip says the machine cannot host the check; a silent\n    relocation to a second noexec directory would report it as a code failure."
    if _exec_allowed(tmp_path):
        yield tmp_path
        return
    try:
        base = pathlib.Path.home() / ".cache" / "agent-context-tests"
        base.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError) as exc:  
        pytest.skip(f"tmp_path forbids execution and no fallback under home is usable: {exc}")
    _sweep(base)
    alt = pathlib.Path(tempfile.mkdtemp(dir=base))
    if not _exec_allowed(alt):
        shutil.rmtree(alt, ignore_errors=True)
        pytest.skip(f"neither tmp_path nor {base} allows executing a file")
    try:
        yield alt
    finally:
        shutil.rmtree(alt, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    'A fresh ContextStore on an isolated temp root (never the live store).'
    root = tmp_path / "ctx"
    (root / "global").mkdir(parents=True)
    return ContextStore(root=str(root))


@pytest.fixture(autouse=True)
def _no_real_relay_env_file(monkeypatch, tmp_path):
    "Point the relay's env-file fallback at a file that does not exist.\n\n    `server.main()` fills AGENT_CONTEXT_HOST/TOKEN/PORT from ~/.config/agent-context/env when the\n    environment lacks them. Without this, a test that calls main() would read the real file of the\n    machine running it: on a migrated machine (whose file names the remote host) that flips the\n    local-mode tests to remote mode, and the deploy gate, which runs this suite on every machine,\n    would fail there and pin that machine on its last build."
    monkeypatch.setenv("AGENT_CONTEXT_ENV_FILE", str(tmp_path / "no-such-relay-env"))


@pytest.fixture(autouse=True)
def _no_real_daemon_host(monkeypatch):
    "AGENT_CONTEXT_HOST/PORT cleared for every test.\n\n    `daemon.mcp_url()` reads these straight from the environment, and a\n    machine whose shell sets them to its real relay host (the ls tailnet address, say) leaked\n    that host into `relay_materialize.refresh()`'s new get_materialized MCP call the moment a\n    test invoked it without overriding daemon.mcp_url itself: a real, non-loopback connection\n    from the suite, caught by the hermetic guard. A test of remote-mode behavior sets its own\n    value after this fixture runs, which wins."
    monkeypatch.delenv("AGENT_CONTEXT_HOST", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_PORT", raising=False)


@pytest.fixture(autouse=True)
def _no_real_local_daemon(monkeypatch):
    "refresh()'s get_materialized call never reaches the host's own daemon on :8765."
    from agent_context import relay_materialize as R
    real = R.fetch_bundle_mcp
    default = "http://127.0.0.1:8765/mcp"

    def guarded(url, token, timeout):
        return None if url == default else real(url, token, timeout)
    monkeypatch.setattr(R, "fetch_bundle_mcp", guarded)


@pytest.fixture(autouse=True)
def _no_real_token_table(monkeypatch, tmp_path):
    'Point the token table at a path that does not exist, for every test.\n\n    A daemon host has a real table at ~/.config/agent-context/tokens.json, and with it present\n    `token_table.auth_required()` is true: tests that expect "no auth configured" got 401 there and\n    blocked every commit on that host. A test that needs a table sets its own path.\n\n    The variable alone is not enough: a test or fixture that unsets it (a fixture teardown that\n    reloads the server does) would fall back to the real path. So `table_path` itself never\n    returns the real default while a test runs: with no override it names the missing file.'
    missing = tmp_path / "no-such-token-table.json"
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(missing))

    def table_path() -> pathlib.Path:
        override = os.environ.get("AGENT_CONTEXT_TOKEN_TABLE")
        return pathlib.Path(override) if override else missing

    from agent_context import token_table
    monkeypatch.setattr(token_table, "table_path", table_path)


@pytest.fixture(autouse=True)
def _no_real_relay_install(request, monkeypatch):
    "No test runs the machine's real relay installer: a bridge that sees a release header would\n    build a release under the real ~/.local/share and exec into it (policy). test_relay_swap\n    replaces `install` itself."
    if request.module.__name__.rpartition(".")[2] == "test_relay_swap":
        return
    from agent_context import relay_swap
    monkeypatch.setattr(relay_swap, "install", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_fsync_in_tests(monkeypatch):
    "Entity writes skip the fsync for the duration of the suite.\n\n    NOT a shortcut and not a weakened test: `_write_atomic`'s crash-safety is\n    `os.replace`, which still runs here — the fsync only buys durability across a\n    power loss, and every store in this suite is a `tmp_path` directory that is\n    deleted minutes later. There is nothing to survive.\n\n    Autouse and monkeypatched, so it cannot leak into a real store: it is scoped to\n    the test and reverted after, and nothing else in the fleet sets this variable."
    monkeypatch.setenv("AGENT_CONTEXT_FSYNC", "0")


@pytest.fixture(autouse=True)
def _no_gate_venv_sync(monkeypatch):
    "run_gate syncs the daemon's venv before its suite. A test that reaches run_gate must\n    never run a real `uv sync` on the venv this suite is running from; the tests of the sync\n    itself turn it back on with a faked subprocess."
    monkeypatch.setenv("AGENT_CONTEXT_GATE_SYNC", "0")


@pytest.fixture(autouse=True)
def _no_real_process_table(monkeypatch):
    "claims.owner asks whether a pid in a claim's chain still runs. A fixture's made-up pid\n    may be a real process on the machine running the suite, so no pid runs unless a test\n    says so."
    from agent_context import claims
    monkeypatch.setattr(claims, "_alive", lambda pid: False)


@pytest.fixture(autouse=True)
def _no_sync_loop(monkeypatch):
    "No test starts the server's sync loop or watchdog by accident."
    monkeypatch.setenv("AGENT_CONTEXT_NO_SYNC", "1")


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch):
    "No test may inherit the machine's git config — signing, hooks, credential helpers."
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(k, raising=False)
    
    
    
    for k in [k for k in os.environ if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _hermetic_state_dir(request, tmp_path, monkeypatch):
    "No test may write the MACHINE's daemon state.\n\n    Both the module and its importers are patched: `daemon`/`usage` bind\n    `_state_dir = paths.state_dir` at import, so patching `paths` alone misses them.\n    XDG_STATE_HOME covers subprocesses and shell-outs; the direct patches cover\n    macOS, where state_dir() ignores the env var entirely."
    
    
    
    if request.module.__name__.rpartition(".")[2] == "test_state_dir":
        return

    from agent_context import daemon as D
    from agent_context import paths as P
    from agent_context import usage as U

    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(P, "state_dir", lambda: d)
    monkeypatch.setattr(P, "log_dir", lambda: d)
    monkeypatch.setattr(D, "_state_dir", lambda: d)
    monkeypatch.setattr(D, "_log_dir", lambda: d)
    monkeypatch.setattr(U, "_state_dir", lambda: d)
    
    
    
    
    
    health = tmp_path / "health"
    monkeypatch.setattr(P, "health_dir", lambda: health)
    monkeypatch.setattr(D, "_health_dir", lambda: health)
    
    
    
    
    
    
    
    
    
    
    
    monkeypatch.setenv("AGENT_CONTEXT_STORE", str(tmp_path / "fallback-store"))
