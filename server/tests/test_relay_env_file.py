"C9b: a relay started by a GUI harness (Claude Desktop, Xcode) has no shell, so it never sees the\nAGENT_CONTEXT_* variables the zsh and fish loaders export. The stdio relay therefore fills the three\nsettings it needs from ~/.config/agent-context/env when the environment lacks them.\n\nOnly HOST, TOKEN and PORT are read. The file on ls also holds OAuth secrets and the allowed-hosts\nlist; none of those may leak into a relay's environment."
import logging
import os
from pathlib import Path

import pytest

from agent_context import daemon, relay_env
from agent_context import server as S

HOST = "example.invalid"
TOKEN = "s3cret=token-with-equals"
KEYS = ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_TOKEN", "AGENT_CONTEXT_PORT", "AGENT_CONTEXT_MACHINE_ID")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*KEYS, "AGENT_CONTEXT_TRANSPORT", "AGENT_CONTEXT_NO_DAEMON"):
        monkeypatch.delenv(name, raising=False)


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".config" / "agent-context" / "env"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def _load(tmp_path: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    values = {} if env is None else env
    relay_env.load_relay_env(values, home=tmp_path)
    return values




def test_fills_the_three_settings_from_the_file(tmp_path: Path) -> None:
    _file(tmp_path, f"AGENT_CONTEXT_HOST={HOST}\nAGENT_CONTEXT_TOKEN={TOKEN}\nAGENT_CONTEXT_PORT=9000\n")
    assert _load(tmp_path) == {
        "AGENT_CONTEXT_HOST": HOST, "AGENT_CONTEXT_TOKEN": TOKEN, "AGENT_CONTEXT_PORT": "9000",
    }


def test_the_machine_uuid_pin_is_read(tmp_path: Path) -> None:
    'A WSL host pins its store uuid here: its /etc/machine-id changes on reboot.'
    _file(tmp_path, "AGENT_CONTEXT_MACHINE_ID=dd978531c97e4b21af43983ce503d558\n")
    assert _load(tmp_path) == {"AGENT_CONTEXT_MACHINE_ID": "dd978531c97e4b21af43983ce503d558"}


def test_the_environment_wins_over_the_file(tmp_path: Path) -> None:
    _file(tmp_path, f"AGENT_CONTEXT_HOST={HOST}\nAGENT_CONTEXT_TOKEN={TOKEN}\n")
    got = _load(tmp_path, {"AGENT_CONTEXT_HOST": "other.example"})
    assert got["AGENT_CONTEXT_HOST"] == "other.example"
    assert got["AGENT_CONTEXT_TOKEN"] == TOKEN


def test_an_empty_environment_value_counts_as_unset(tmp_path: Path) -> None:
    _file(tmp_path, f"AGENT_CONTEXT_HOST={HOST}\n")
    assert _load(tmp_path, {"AGENT_CONTEXT_HOST": ""})["AGENT_CONTEXT_HOST"] == HOST


def test_other_keys_are_never_loaded(tmp_path: Path) -> None:
    _file(tmp_path, (
        f"AGENT_CONTEXT_HOST={HOST}\nAGENT_CONTEXT_ALLOWED_HOSTS={HOST}\n"
        "AGENT_CONTEXT_OAUTH_PASSPHRASE=hunter2\nAGENT_CONTEXT_OAUTH_CLIENT_SECRET=x\nSOME_OTHER=1\n"
    ))
    assert set(_load(tmp_path)) == {"AGENT_CONTEXT_HOST"}


def test_a_value_with_equals_signs_is_kept_whole(tmp_path: Path) -> None:
    _file(tmp_path, f"AGENT_CONTEXT_TOKEN={TOKEN}\n")
    assert _load(tmp_path)["AGENT_CONTEXT_TOKEN"] == TOKEN


def test_one_layer_of_double_quotes_is_stripped(tmp_path: Path) -> None:
    _file(tmp_path, f'AGENT_CONTEXT_HOST="{HOST}"\n')
    assert _load(tmp_path)["AGENT_CONTEXT_HOST"] == HOST


def test_a_last_line_with_no_newline_is_read(tmp_path: Path) -> None:
    _file(tmp_path, f"AGENT_CONTEXT_TOKEN={TOKEN}\nAGENT_CONTEXT_HOST={HOST}")
    assert _load(tmp_path)["AGENT_CONTEXT_HOST"] == HOST


def test_comments_and_blank_lines_are_skipped(tmp_path: Path) -> None:
    _file(tmp_path, f"# AGENT_CONTEXT_HOST=commented.example\n\nAGENT_CONTEXT_HOST={HOST}\n")
    assert _load(tmp_path) == {"AGENT_CONTEXT_HOST": HOST}


def test_the_last_assignment_wins(tmp_path: Path) -> None:
    _file(tmp_path, "AGENT_CONTEXT_HOST=first.example\nAGENT_CONTEXT_HOST=second.example\n")
    assert _load(tmp_path)["AGENT_CONTEXT_HOST"] == "second.example"




def test_a_missing_file_changes_nothing(tmp_path: Path) -> None:
    assert _load(tmp_path, {"KEEP": "1"}) == {"KEEP": "1"}


def test_a_directory_where_the_file_should_be_changes_nothing(tmp_path: Path) -> None:
    (tmp_path / ".config" / "agent-context" / "env").mkdir(parents=True)
    assert _load(tmp_path) == {}


def test_an_unreadable_file_changes_nothing(tmp_path: Path) -> None:
    path = _file(tmp_path, f"AGENT_CONTEXT_HOST={HOST}\n")
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        assert _load(tmp_path) == {}
    finally:
        path.chmod(0o600)


def test_a_file_that_is_not_text_changes_nothing(tmp_path: Path) -> None:
    path = _file(tmp_path, "")
    path.write_bytes(b"\xff\xfe\x00AGENT_CONTEXT_HOST=x\n")
    assert _load(tmp_path) == {}


def test_the_path_can_be_overridden_by_the_environment(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text(f"AGENT_CONTEXT_HOST={HOST}\n")
    values = {"AGENT_CONTEXT_ENV_FILE": str(elsewhere)}
    relay_env.load_relay_env(values, home=tmp_path / "no-home")
    assert values["AGENT_CONTEXT_HOST"] == HOST


def test_values_are_never_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _file(tmp_path, f"AGENT_CONTEXT_TOKEN={TOKEN}\nAGENT_CONTEXT_HOST={HOST}\n")
    _load(tmp_path)
    assert TOKEN not in caplog.text




def _env_file_for_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "relay.env"
    path.write_text(f"AGENT_CONTEXT_HOST={HOST}\nAGENT_CONTEXT_TOKEN={TOKEN}\n")
    monkeypatch.setenv("AGENT_CONTEXT_ENV_FILE", str(path))


def test_the_stdio_relay_has_the_file_settings_before_it_picks_local_or_remote(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env_file_for_main(monkeypatch, tmp_path)
    seen: dict[str, str | None] = {}

    def ensure() -> str:
        seen["host"] = os.environ.get("AGENT_CONTEXT_HOST")
        seen["remote"] = str(daemon.is_remote())
        return f"https://{HOST}/mcp"

    monkeypatch.setattr(daemon, "ensure_daemon", ensure)
    monkeypatch.setattr(daemon, "run_bridge", lambda: None)
    monkeypatch.setattr(S, "_materialize_if_remote", lambda: True)
    S.main()
    assert seen == {"host": HOST, "remote": "True"}


def test_the_http_daemon_does_not_read_the_file(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env_file_for_main(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_CONTEXT_TRANSPORT", "http")
    monkeypatch.setattr(daemon, "_port_open", lambda: True)
    with pytest.raises(SystemExit):
        S.main()
    assert "AGENT_CONTEXT_HOST" not in os.environ
    assert S.mcp.settings.host == "127.0.0.1"




def test_the_suite_points_the_env_file_at_nothing_by_default() -> None:
    'conftest sets AGENT_CONTEXT_ENV_FILE to a path that does not exist, so a test that calls\n    main() cannot pick up the real ~/.config/agent-context/env of the machine running the gate\n    (a gate that fails on a migrated machine pins the whole fleet on its last build).'
    target = os.environ.get("AGENT_CONTEXT_ENV_FILE")
    assert target
    assert not Path(target).exists()
