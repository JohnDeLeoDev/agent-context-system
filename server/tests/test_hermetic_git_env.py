"The hermetic-git fixture must close git's ENVIRONMENT config channels too.\n\n  * `git -c key=value <cmd>` exports GIT_CONFIG_PARAMETERS\n  * GIT_CONFIG_COUNT with GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n pairs\n\nSo a maintainer committing with `git -c commit.gpgsign=true` pushed signing into the\npre-commit gate's test subprocesses, which have no signing agent: the gate reported\n3 failed / 20 errors on a change that was fine, while the identical suite run from a\nshell moments earlier was 450 passed. A fixture that is hermetic against files and\nporous to the environment fails exactly when someone is being careful.\n\nThe second test is the real one: it re-runs the first test in a child pytest with the\ncontamination present, which is the only way to observe a fixture that is supposed to\nmake contamination invisible."
import os
import subprocess
import sys

import pytest

_TIMEOUT = 120
_INNER = "test_env_config_channels_are_cleared"

CHANNELS = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")


def _git(cwd, *args, env=None):
    return subprocess.run(("git", "-C", str(cwd), *args), capture_output=True,
                          text=True, timeout=_TIMEOUT, env=env)


def test_env_config_channels_are_cleared(tmp_path):
    'Inside a test, no env-borne git config survives — whatever the parent had.'
    for name in CHANNELS:
        assert name not in os.environ, f"{name} reached a test"
    assert not [k for k in os.environ
                if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]

    
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    got = _git(repo, "config", "--get", "commit.gpgsign")
    assert got.stdout.strip() == "", f"signing leaked into a test repo: {got.stdout!r}"


@pytest.mark.parametrize("contamination", [
    {"GIT_CONFIG_PARAMETERS": "'commit.gpgsign=true'"},
    {"GIT_CONFIG_COUNT": "1",
     "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "true"},
])
def test_inherited_git_config_does_not_reach_tests(contamination):
    'Re-run the check in a child pytest that DOES carry the contamination.'
    node = f"{os.path.abspath(__file__)}::{_INNER}"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
        capture_output=True, text=True, timeout=_TIMEOUT,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, **contamination},
    )
    assert proc.returncode == 0, (
        f"env-borne git config reached the test process:\n{proc.stdout}\n{proc.stderr}")
