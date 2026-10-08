'The ceiling lives in slow_test_ceiling.py and is loaded for the whole suite from conftest.py.\nEach case here runs a two-test file under a fresh pytest with the ceiling lowered, so the\ncheck costs a fraction of a second and waits on nothing real.'
import os
import subprocess
import sys
from pathlib import Path

import slow_test_ceiling

TESTS = Path(__file__).parent

SAMPLE = """
import time


def test_quick():
    assert True


def test_waits():
    time.sleep(0.4)


def test_fails_for_its_own_reason():
    time.sleep(0.4)
    assert False, "its own failure"
"""


def _run(tmp_path: Path, ceiling: str) -> subprocess.CompletedProcess:
    sample = tmp_path / "test_sample.py"
    sample.write_text(SAMPLE)
    env = {**os.environ, "AGENT_CONTEXT_TEST_CEILING": ceiling, "PYTHONPATH": str(TESTS)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "slow_test_ceiling", "-p", "no:cacheprovider",
         "--rootdir", str(tmp_path), "-c", os.devnull, str(sample)],
        capture_output=True, text=True, timeout=60, env=env, cwd=tmp_path)


def test_the_suite_loads_the_ceiling_and_it_is_thirty_seconds() -> None:
    assert slow_test_ceiling.CEILING_SECONDS == 30.0
    assert "slow_test_ceiling" in (TESTS / "conftest.py").read_text()


def test_a_test_past_the_ceiling_fails_with_its_time(tmp_path: Path) -> None:
    done = _run(tmp_path, "0.1")
    out = done.stdout
    assert done.returncode == 1, out + done.stderr
    assert "1 passed" in out and "2 failed" in out
    assert "test_waits ran for 0." in out and "the ceiling for one test is 0.1 s (policy)" in out
    
    assert "its own failure" in out


def test_under_the_ceiling_nothing_changes(tmp_path: Path) -> None:
    done = _run(tmp_path, "5")
    assert "2 passed" in done.stdout and "1 failed" in done.stdout, done.stdout + done.stderr
    assert "policy" not in done.stdout
