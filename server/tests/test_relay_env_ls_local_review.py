"Reviewer finding: an env-file override with no file name ('.', '/') must not raise.\n\nBefore the ls-local change `load_relay_env` read the override inside a try and returned on any\nfailure. Deriving ls-local.env with `path.with_name` raises ValueError for such a path, and the\nscheduled relay update calls the loader with no guard, so it would have crashed at startup."
from __future__ import annotations

from pathlib import Path

import pytest

from agent_context import relay_env


@pytest.mark.parametrize("override", [".", "/", "..", "./", "//"])
def test_an_override_with_no_file_name_changes_nothing_and_never_raises(
        override: str, tmp_path: Path) -> None:
    values = {"AGENT_CONTEXT_ENV_FILE": override}
    relay_env.load_relay_env(values, home=tmp_path)
    assert values == {"AGENT_CONTEXT_ENV_FILE": override}


def test_an_override_naming_a_directory_still_reads_ls_local_nowhere(tmp_path: Path) -> None:
    (tmp_path / "ls-local.env").write_text("AGENT_CONTEXT_TOKEN=abc\n")
    (tmp_path / "ls-local.env").chmod(0o600)
    values = {"AGENT_CONTEXT_ENV_FILE": str(tmp_path)}       
    relay_env.load_relay_env(values, home=tmp_path / "no-home")
    assert "AGENT_CONTEXT_TOKEN" not in values
