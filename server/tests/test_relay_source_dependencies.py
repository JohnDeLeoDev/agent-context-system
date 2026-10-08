'C9b: a relay installed from /relay-source resolves its dependencies from pyproject alone\n(`uv tool install` ignores uv.lock). Found in the first end-to-end run: `mcp[cli]>=1.9.0` resolved to\nmcp 2.2.0, where `mcp.server.fastmcp` no longer exists, so the installed relay could not import.\nThe fleet runs the locked mcp 1.x.'
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

SERVER_DIR = Path(__file__).resolve().parents[1]
LOCKED_MCP = "1.28.1"


def _mcp_requirement() -> Requirement:
    project = tomllib.loads((SERVER_DIR / "pyproject.toml").read_text())
    found = [Requirement(dep) for dep in project["project"]["dependencies"]]
    (mcp,) = [req for req in found if req.name == "mcp"]
    return mcp


def test_pyproject_excludes_mcp_2() -> None:
    assert not _mcp_requirement().specifier.contains("2.2.0")


def test_pyproject_still_allows_the_locked_mcp() -> None:
    assert _mcp_requirement().specifier.contains(LOCKED_MCP)


def test_pyproject_still_asks_for_the_cli_extra() -> None:
    assert "cli" in _mcp_requirement().extras


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_uv_lock_agrees_with_pyproject() -> None:
    result = subprocess.run(
        ["uv", "lock", "--check", "--offline"], cwd=SERVER_DIR, capture_output=True, text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
