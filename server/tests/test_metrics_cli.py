'The command line: `python -m agent_context.metrics <command>`.\n\nThe orchestrator emits a phase with one call. The other commands are collect, backfill,\nsummary and verify.'
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from metrics_fixtures import events, log_files

from agent_context import metrics

SRC = str(Path(__file__).resolve().parent.parent / "src")
PHASES = ["requested", "criteria_sent", "criteria_approved", "tests_written", "tests_locked",
          "impl_done", "committed", "review_start", "review_end", "landing_asked",
          "landing_approved", "landed"]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return dict(os.environ, PYTHONPATH=SRC, AGENT_CONTEXT_METRICS_DIR=str(tmp_path / "metrics"),
                CLAUDE_CODE_SESSION_ID="48cc5357-068b-4cad-9e29-66f754cd5732")


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "agent_context.metrics", *args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_phase_command_emits_one_phase_event(env: dict[str, str], tmp_path: Path) -> None:
    proc = _run(env, "phase", "metrics-collector", "tests_locked")
    assert proc.returncode == 0, proc.stderr
    row, = events(tmp_path / "metrics")
    assert row["type"] == "phase" and row["ref"] == "tests_locked"
    assert row["change"] == "metrics-collector" and row["session"] == "48cc5357"


@pytest.mark.parametrize("name", PHASES)
def test_every_documented_phase_name_is_accepted(env: dict[str, str], tmp_path: Path,
                                                 name: str) -> None:
    assert _run(env, "phase", "c1", name).returncode == 0
    assert events(tmp_path / "metrics")[0]["ref"] == name


def test_an_unknown_phase_name_is_refused_and_nothing_is_written(env: dict[str, str],
                                                                 tmp_path: Path) -> None:
    proc = _run(env, "phase", "c1", "went-for-lunch")
    assert proc.returncode == 2
    assert not log_files(tmp_path / "metrics")


def test_verify_command_exit_codes(env: dict[str, str], tmp_path: Path) -> None:
    _run(env, "phase", "c1", "requested")
    _run(env, "phase", "c1", "criteria_sent")
    assert _run(env, "verify").returncode == 0
    f = log_files(tmp_path / "metrics")[0]
    f.write_text(f.read_text().replace("criteria_sent", "landed"))
    proc = _run(env, "verify")
    assert proc.returncode == 1
    assert f.name in proc.stdout


def test_summary_command_prints_text_and_json(env: dict[str, str], tmp_path: Path) -> None:
    _run(env, "phase", "c1", "requested")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    text = _run(env, "summary", "--state", str(state))
    assert text.returncode == 0 and "c1" in text.stdout
    as_json = _run(env, "summary", "--state", str(state), "--json")
    assert json.loads(as_json.stdout)["changes"]["c1"]["lead_s"] == 0


def test_collect_command_runs_against_a_store(env: dict[str, str], tmp_path: Path) -> None:
    store = tmp_path / "store" / "machines" / "u1"
    store.mkdir(parents=True)
    (store / "daemon-status.json").write_text(
        json.dumps({"machine_id": "ls", "server_commit": "abcdef123"}))
    proc = _run(env, "collect", "--store", str(tmp_path / "store"),
                "--token-db", str(tmp_path / "none.db"), "--claims", str(tmp_path / "claims"))
    assert proc.returncode == 0, proc.stderr
    row, = events(tmp_path / "metrics")
    assert row["type"] == "adopt" and row["ref"] == "ls@abcdef1"


def test_the_module_imports_with_the_standard_library_only() -> None:
    'The gate scripts load metrics.py by path under the system python; nothing outside\n    the standard library may be imported at module level.'
    src = Path(metrics.__file__).read_text()
    import re
    imported = set(re.findall(r"^(?:from|import)\s+([A-Za-z_][\w]*)", src, re.MULTILINE))
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
