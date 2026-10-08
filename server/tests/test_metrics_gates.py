"Gate and landing scripts emit metrics (C5): store-precommit-gate.py, store-wt-finish.py\nand the daemon's self-deploy gate.\n\nEach emits gate_start and gate_end with duration, ok and test counts, and the daemon adds\ngate_cooloff_start when a gate fails. Exit codes, stdout and stderr stay as they were,\nand a missing or broken metrics module must change nothing."
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from metrics_fixtures import events, of_type, raw_lines

from agent_context import daemon, metrics

WT_ROOT = Path(__file__).resolve().parents[2]


def _main_checkout() -> Path:
    "The store's main checkout. Scripts under global/scripts are store entities: they\n    are edited through the store tools on the main checkout, never in a worktree, so the\n    scripts under test are the main checkout's."
    proc = subprocess.run(["git", "-C", str(WT_ROOT), "rev-parse", "--path-format=absolute",
                           "--git-common-dir"], capture_output=True, text=True, timeout=30)
    return Path(proc.stdout.strip()).parent if proc.returncode == 0 else WT_ROOT


_SCRIPTS = _main_checkout() / "global" / "scripts"
PRECOMMIT = Path(os.environ.get("STORE_PRECOMMIT_GATE") or _SCRIPTS / "store-precommit-gate.py")
WT_FINISH = Path(os.environ.get("STORE_WT_FINISH") or _SCRIPTS / "store-wt-finish.py")
METRICS_PY = Path(metrics.__file__).resolve()

FAKE_PYTHON = """#!/bin/sh
case "$*" in
  *py_compile*) exit "${FAKE_COMPILE_RC:-0}" ;;
  *compileall*) exit "${FAKE_COMPILE_RC:-0}" ;;
  *pytest*) printf '%s\\n' "${FAKE_PYTEST_OUT:-5 passed in 0.10s}"; exit "${FAKE_PYTEST_RC:-0}" ;;
esac
exit 0
"""


def _exec_ok(directory: Path) -> bool:
    probe = directory / "exec-probe"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(0o755)
    try:
        return subprocess.run([str(probe)], timeout=10).returncode == 0
    except OSError:
        return False


def _env(home: Path, mdir: Path, **extra: str) -> dict[str, str]:
    keep = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    return {**keep, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "AGENT_CONTEXT_METRICS_DIR": str(mdir), **extra}


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout.strip()


class Fixture:
    def __init__(self, tmp_path: Path, with_metrics: bool = True) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        if not _exec_ok(tmp_path):
            pytest.skip("tmp_path is on a noexec filesystem: the fake python cannot run")
        self.tmp = tmp_path
        self.store = tmp_path / "store"
        self.mdir = tmp_path / "metrics"
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.env = _env(self.home, self.mdir)
        (self.store / "server" / "src" / "agent_context").mkdir(parents=True)
        (self.store / "global").mkdir()
        (self.store / "global" / "note.md").write_text("seed\n")
        (self.store / ".gitignore").write_text("__pycache__/\n.venv/\n")
        if with_metrics:
            shutil.copy(METRICS_PY, self.store / "server" / "src" / "agent_context" / "metrics.py")
        (self.store / "server" / "src" / "agent_context" / "x.py").write_text("X = 1\n")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.store)], check=True,
                       env=self.env, timeout=60)
        _git(self.store, self.env, "add", "-A")
        _git(self.store, self.env, "commit", "-q", "-m", "seed")
        venv = self.store / "server" / ".venv" / "bin"
        venv.mkdir(parents=True)
        py = venv / "python"
        py.write_text(FAKE_PYTHON)
        py.chmod(0o755)

    def run(self, argv: list[str], cwd: Path, **extra: str) -> tuple[int, str, str]:
        env = dict(self.env, **extra)
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=env,
                              timeout=120)
        return proc.returncode, proc.stdout, proc.stderr




def _stage_server_change(repo: Path, env: dict[str, str]) -> None:
    (repo / "server" / "src" / "agent_context" / "x.py").write_text("X = 2\n")
    _git(repo, env, "add", "-A", "--", "server/src/agent_context/x.py")


def test_precommit_emits_start_and_end_with_counts_on_a_red_suite(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    _stage_server_change(fx.store, fx.env)
    rc, _out, err = fx.run([sys.executable, str(PRECOMMIT)], fx.store,
                           FAKE_PYTEST_RC="1", FAKE_PYTEST_OUT="1 failed, 3 passed, 2 skipped in 1.23s",
                           CLAUDE_CODE_SESSION_ID="48cc5357-068b-4cad-9e29-66f754cd5732")
    assert rc == 1 and "the server test suite is RED" in err
    start, = of_type(fx.mdir, "gate_start")
    end, = of_type(fx.mdir, "gate_end")
    assert start["ref"] == end["ref"] == "precommit"
    assert start["session"] == end["session"] == "48cc5357"
    assert end["ok"] is False
    assert (end["n"], end["failed"], end["skipped"]) == (3, 1, 2)
    assert isinstance(end["dur_ms"], int) and end["dur_ms"] >= 0
    assert "change" not in end                     


def test_precommit_emits_ok_true_and_reruns_on_a_green_suite(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    _stage_server_change(fx.store, fx.env)
    rc, _out, err = fx.run([sys.executable, str(PRECOMMIT)], fx.store,
                           FAKE_PYTEST_OUT="5 passed, 1 rerun in 0.10s")
    assert rc == 0 and "gate passed." in err
    end, = of_type(fx.mdir, "gate_end")
    assert end["ok"] is True and end["n"] == 5 and end["reruns"] == 1


def test_precommit_records_a_compile_failure_as_a_failed_gate(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    _stage_server_change(fx.store, fx.env)
    rc, _out, err = fx.run([sys.executable, str(PRECOMMIT)], fx.store, FAKE_COMPILE_RC="1")
    assert rc == 1 and "py_compile failed" in err
    end, = of_type(fx.mdir, "gate_end")
    assert end["ok"] is False and "n" not in end


def test_precommit_names_the_worktree_as_the_change(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    wt = fx.store / ".agents" / "worktrees" / "feat"
    _git(fx.store, fx.env, "worktree", "add", "-q", "-b", "feat", str(wt))
    _stage_server_change(wt, fx.env)
    rc, _o, _e = fx.run([sys.executable, str(PRECOMMIT)], wt)
    assert rc == 0
    end, = of_type(fx.mdir, "gate_end")
    assert end["change"] == "feat"


def test_precommit_with_no_server_change_emits_nothing(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    (fx.store / "global" / "note.md").write_text("changed\n")
    _git(fx.store, fx.env, "add", "-A")
    rc, _o, _e = fx.run([sys.executable, str(PRECOMMIT)], fx.store)
    assert rc == 0
    assert raw_lines(fx.mdir) == []
    _stage_server_change(fx.store, fx.env)                 
    fx.run([sys.executable, str(PRECOMMIT)], fx.store)
    assert len(of_type(fx.mdir, "gate_end")) == 1


def test_precommit_output_and_exit_code_are_identical_without_the_metrics_module(
        tmp_path: Path) -> None:
    with_m = Fixture(tmp_path / "a")
    without = Fixture(tmp_path / "b", with_metrics=False)
    results = []
    for fx in (with_m, without):
        _stage_server_change(fx.store, fx.env)
        results.append(fx.run([sys.executable, str(PRECOMMIT)], fx.store,
                              FAKE_PYTEST_RC="1", FAKE_PYTEST_OUT="1 failed, 3 passed in 1s"))
    assert results[0] == results[1]
    assert len(of_type(with_m.mdir, "gate_end")) == 1      
    assert raw_lines(without.mdir) == []


def test_precommit_survives_a_broken_metrics_module(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    (fx.store / "server" / "src" / "agent_context" / "metrics.py").write_text(
        "raise RuntimeError('broken module')\n")
    _stage_server_change(fx.store, fx.env)
    rc, _out, err = fx.run([sys.executable, str(PRECOMMIT)], fx.store)
    assert rc == 0 and "gate passed." in err




def _wt_setup(tmp_path: Path, with_metrics: bool = True) -> tuple[Fixture, Path]:
    fx = Fixture(tmp_path, with_metrics)
    wt = tmp_path / "worktrees" / "feat"
    _git(fx.store, fx.env, "worktree", "add", "-q", "-b", "feat", str(wt))
    return fx, wt


def _commit_server(wt: Path, env: dict[str, str]) -> None:
    (wt / "server" / "src" / "agent_context" / "x.py").write_text("X = 3\n")
    _git(wt, env, "add", "-A")
    _git(wt, env, "commit", "-q", "-m", "server change")


def _finish(fx: Fixture, wt: Path, **extra: str) -> tuple[int, str, str]:
    return fx.run([sys.executable, str(WT_FINISH), str(wt)], wt,
                  AGENT_CONTEXT_STORE=str(fx.store), **extra)


def test_wt_finish_emits_gate_events_and_a_landed_event_for_a_server_change(tmp_path: Path) -> None:
    fx, wt = _wt_setup(tmp_path)
    _commit_server(wt, fx.env)
    rc, out, err = _finish(fx, wt, FAKE_PYTEST_OUT="4 passed, 1 skipped in 0.50s",
                           CLAUDE_CODE_SESSION_ID="8fce50e0-7232-4397-9363-546bcbcf22e9")
    assert rc == 0, err
    assert "4 passed, 1 skipped in 0.50s" in out           
    assert "gate passed." in out
    end, = of_type(fx.mdir, "gate_end")
    assert end["ref"] == "wt-finish" and end["ok"] is True
    assert (end["n"], end["skipped"]) == (4, 1)
    assert end["change"] == "feat" and end["session"] == "8fce50e0"
    assert len(of_type(fx.mdir, "gate_start")) == 1
    landed, = of_type(fx.mdir, "landed")
    sha = _git(fx.store, fx.env, "log", "-1", "--format=%h", "--abbrev=7", "main", "--", "server/")
    assert landed["ref"] == sha and re.fullmatch(r"[0-9a-f]{7}", sha)
    assert landed["ok"] is True and landed["change"] == "feat"


def test_wt_finish_records_a_red_gate_and_lands_nothing(tmp_path: Path) -> None:
    fx, wt = _wt_setup(tmp_path)
    _commit_server(wt, fx.env)
    rc, _out, err = _finish(fx, wt, FAKE_PYTEST_RC="1", FAKE_PYTEST_OUT="2 failed, 3 passed in 1s")
    assert rc == 1 and "GATE FAILED" in err
    end, = of_type(fx.mdir, "gate_end")
    assert end["ok"] is False and end["failed"] == 2 and end["n"] == 3
    assert of_type(fx.mdir, "landed") == []


def test_wt_finish_landing_without_server_changes_expects_no_adoption(tmp_path: Path) -> None:
    fx, wt = _wt_setup(tmp_path)
    (wt / "global" / "note.md").write_text("docs only\n")
    _git(wt, fx.env, "add", "-A")
    _git(wt, fx.env, "commit", "-q", "-m", "docs")
    rc, _out, err = _finish(fx, wt)
    assert rc == 0, err
    assert of_type(fx.mdir, "gate_end") == []
    landed, = of_type(fx.mdir, "landed")
    assert landed["ok"] is False
    assert landed["ref"] == _git(fx.store, fx.env, "rev-parse", "--short=7", "main")


def test_wt_finish_output_and_exit_code_are_identical_without_the_metrics_module(
        tmp_path: Path) -> None:
    results = []
    for name, with_metrics in (("a", True), ("b", False)):
        fx, wt = _wt_setup(tmp_path / name, with_metrics)
        _commit_server(wt, fx.env)
        rc, out, err = _finish(fx, wt, FAKE_PYTEST_OUT="4 passed in 0.5s")
        norm = lambda s: re.sub(r"[0-9a-f]{7,40}", "SHA", s)          
        results.append((rc, norm(out), norm(err)))
    assert results[0] == results[1]
    assert len(of_type(tmp_path / "a" / "metrics", "landed")) == 1     




@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    calls: dict[str, object] = {"exec": 0, "gate": 0, "notify": [], "gate_result": (True, "ok")}

    def fake_gate(timeout: int = 120) -> tuple[bool, str]:
        calls["gate"] = int(str(calls["gate"])) + 1
        return calls["gate_result"]  

    def fake_exec() -> None:
        calls["exec"] = int(str(calls["exec"])) + 1

    monkeypatch.setattr(daemon, "_do_exec", fake_exec)
    monkeypatch.setattr(daemon, "run_gate", fake_gate)
    monkeypatch.setattr(daemon, "_notify", lambda msg: None)
    monkeypatch.setattr(daemon, "_server_tree_dirty", lambda: False)
    monkeypatch.setattr(daemon, "_ATTEMPTED_VERSIONS", {})
    monkeypatch.setattr(daemon, "_STARTED_VERSION", 100.0)
    monkeypatch.setattr(daemon, "_code_version", lambda: 200.0)
    monkeypatch.setattr(daemon, "_STARTED_FINGERPRINT", "booted")
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "on-disk")
    monkeypatch.delenv("AGENT_CONTEXT_SELF_DEPLOY", raising=False)
    monkeypatch.setenv("AGENT_CONTEXT_METRICS_DIR", str(tmp_path / "metrics"))
    return calls


def test_daemon_gate_failure_emits_start_end_and_the_cooling_off_window(
        wired: dict[str, object], tmp_path: Path) -> None:
    wired["gate_result"] = (False, "pytest failed")
    daemon.maybe_self_redeploy()
    mdir = tmp_path / "metrics"
    assert len(of_type(mdir, "gate_start")) == 1
    end, = of_type(mdir, "gate_end")
    assert end["ref"] == "daemon" and end["ok"] is False
    assert isinstance(end["dur_ms"], int)
    cool, = of_type(mdir, "gate_cooloff_start")
    assert cool["dur_ms"] == int(daemon._GATE_RETRY_SECS * 1000)
    assert wired["exec"] == 0


def test_daemon_gate_pass_emits_no_cooling_off(wired: dict[str, object], tmp_path: Path) -> None:
    daemon.maybe_self_redeploy()
    mdir = tmp_path / "metrics"
    end, = of_type(mdir, "gate_end")
    assert end["ok"] is True
    assert of_type(mdir, "gate_cooloff_start") == []
    assert wired["exec"] == 1


def test_a_build_that_is_cooling_off_emits_nothing_more(wired: dict[str, object],
                                                        tmp_path: Path) -> None:
    wired["gate_result"] = (False, "boom")
    daemon.maybe_self_redeploy()
    n = len(events(tmp_path / "metrics"))
    assert n == 3                                          
    daemon.maybe_self_redeploy()
    daemon.maybe_self_redeploy()
    assert wired["gate"] == 1
    assert len(events(tmp_path / "metrics")) == n


def test_an_emit_that_raises_changes_no_daemon_decision(wired: dict[str, object],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("emit exploded")
    monkeypatch.setattr(metrics, "emit", boom)
    daemon.maybe_self_redeploy()
    assert wired["exec"] == 1
    assert wired["gate"] == 1
