"A relay no shell started also reads the ls-local token, from ls-local.env next to the env file.\n\nPrecedence for AGENT_CONTEXT_TOKEN: a non-empty process environment value, then the token line of\nls-local.env, then the env file's token (today's behavior). The ls-local file is used only when it is a\nregular file owned by the current user with no group or world permission bits; anything else is ignored\nwith a warning that names the path and the reason and never a value. HOST and PORT come only from the\nenv file. A machine without the file behaves exactly as before and logs nothing."
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from agent_context import daemon, relay_env, relay_update
from agent_context import server as S

HOST = "example.invalid"
ENV_TOKEN = "acx_legacy_env_file_token"
LS_TOKEN = "acx_lslocal_0123456789abcdef"
KEYS = ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_TOKEN", "AGENT_CONTEXT_PORT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*KEYS, "AGENT_CONTEXT_TRANSPORT", "AGENT_CONTEXT_NO_DAEMON"):
        monkeypatch.delenv(name, raising=False)


def _dir(tmp_path: Path) -> Path:
    d = tmp_path / ".config" / "agent-context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env_file(tmp_path: Path, text: str = f"AGENT_CONTEXT_HOST={HOST}\nAGENT_CONTEXT_TOKEN={ENV_TOKEN}\n") -> Path:
    path = _dir(tmp_path) / "env"
    path.write_text(text)
    path.chmod(0o600)
    return path


def _ls_local(tmp_path: Path, text: str = f"AGENT_CONTEXT_TOKEN={LS_TOKEN}\n", mode: int = 0o600) -> Path:
    path = _dir(tmp_path) / "ls-local.env"
    path.write_text(text)
    path.chmod(mode)
    return path


def _load(tmp_path: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    values = {} if env is None else env
    relay_env.load_relay_env(values, home=tmp_path)
    return values




def test_the_ls_local_token_beats_the_env_file_token(tmp_path: Path) -> None:
    _env_file(tmp_path)
    _ls_local(tmp_path)
    assert _load(tmp_path)["AGENT_CONTEXT_TOKEN"] == LS_TOKEN


def test_a_process_environment_token_beats_both_files(tmp_path: Path) -> None:
    _env_file(tmp_path)
    _ls_local(tmp_path)
    assert _load(tmp_path, {"AGENT_CONTEXT_TOKEN": "from-the-process"})["AGENT_CONTEXT_TOKEN"] == \
        "from-the-process"


def test_an_empty_process_environment_token_does_not_win(tmp_path: Path) -> None:
    _env_file(tmp_path)
    _ls_local(tmp_path)
    assert _load(tmp_path, {"AGENT_CONTEXT_TOKEN": ""})["AGENT_CONTEXT_TOKEN"] == LS_TOKEN


def test_the_env_file_token_applies_when_there_is_no_ls_local_file(tmp_path: Path) -> None:
    _env_file(tmp_path)
    assert _load(tmp_path)["AGENT_CONTEXT_TOKEN"] == ENV_TOKEN


def test_the_ls_local_token_works_when_the_env_file_is_missing(tmp_path: Path) -> None:
    _ls_local(tmp_path)
    assert _load(tmp_path) == {"AGENT_CONTEXT_TOKEN": LS_TOKEN}


def test_the_last_token_assignment_in_the_ls_local_file_wins(tmp_path: Path) -> None:
    _ls_local(tmp_path, "AGENT_CONTEXT_TOKEN=first\nAGENT_CONTEXT_TOKEN=second\n")
    assert _load(tmp_path)["AGENT_CONTEXT_TOKEN"] == "second"


def test_a_quoted_and_a_commented_line_follow_the_env_file_grammar(tmp_path: Path) -> None:
    _ls_local(tmp_path, f'# AGENT_CONTEXT_TOKEN=commented\nAGENT_CONTEXT_TOKEN="{LS_TOKEN}"\n')
    assert _load(tmp_path)["AGENT_CONTEXT_TOKEN"] == LS_TOKEN




def test_only_the_token_line_of_the_ls_local_file_is_read(tmp_path: Path) -> None:
    _env_file(tmp_path, f"AGENT_CONTEXT_HOST={HOST}\nAGENT_CONTEXT_PORT=9000\n")
    _ls_local(tmp_path, (f"AGENT_CONTEXT_HOST=evil.example\nAGENT_CONTEXT_PORT=1\n"
                         f"AGENT_CONTEXT_TOKEN={LS_TOKEN}\nAGENT_CONTEXT_OAUTH_PASSPHRASE=hunter2\n"
                         "SOME_OTHER=1\n"))
    got = _load(tmp_path)
    assert got == {"AGENT_CONTEXT_HOST": HOST, "AGENT_CONTEXT_PORT": "9000",
                   "AGENT_CONTEXT_TOKEN": LS_TOKEN}


def test_host_and_port_lines_alone_in_the_ls_local_file_change_nothing(tmp_path: Path) -> None:
    _ls_local(tmp_path, "AGENT_CONTEXT_HOST=evil.example\nAGENT_CONTEXT_PORT=1\n")
    assert _load(tmp_path) == {}




@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o610])
def test_a_group_or_world_accessible_file_is_ignored_with_a_warning(
        tmp_path: Path, mode: int, caplog: pytest.LogCaptureFixture) -> None:
    _env_file(tmp_path)
    path = _ls_local(tmp_path, mode=mode)
    with caplog.at_level(logging.WARNING):
        got = _load(tmp_path)
    assert got["AGENT_CONTEXT_TOKEN"] == ENV_TOKEN
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert str(path) in warnings[0] and "permissions" in warnings[0]
    assert LS_TOKEN not in caplog.text


def test_owner_only_modes_are_accepted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    for mode in (0o600, 0o400):
        _ls_local(tmp_path, mode=mode)
        with caplog.at_level(logging.DEBUG):
            assert _load(tmp_path)["AGENT_CONTEXT_TOKEN"] == LS_TOKEN
        (_dir(tmp_path) / "ls-local.env").chmod(0o600)
    assert caplog.records == []


def test_a_symlink_is_ignored_with_a_warning(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _env_file(tmp_path)
    real = tmp_path / "real.env"
    real.write_text(f"AGENT_CONTEXT_TOKEN={LS_TOKEN}\n")
    real.chmod(0o600)
    link = _dir(tmp_path) / "ls-local.env"
    link.symlink_to(real)
    with caplog.at_level(logging.WARNING):
        got = _load(tmp_path)
    assert got["AGENT_CONTEXT_TOKEN"] == ENV_TOKEN
    assert any("symlink" in r.getMessage() and str(link) in r.getMessage() for r in caplog.records)
    assert LS_TOKEN not in caplog.text


def test_a_directory_is_ignored_with_a_warning(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _env_file(tmp_path)
    (_dir(tmp_path) / "ls-local.env").mkdir()
    with caplog.at_level(logging.WARNING):
        got = _load(tmp_path)
    assert got["AGENT_CONTEXT_TOKEN"] == ENV_TOKEN
    assert any("regular file" in r.getMessage() for r in caplog.records)


def test_a_file_owned_by_someone_else_is_ignored_with_a_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _env_file(tmp_path)
    path = _ls_local(tmp_path)
    uid = os.getuid()
    monkeypatch.setattr(relay_env.os, "getuid", lambda: uid + 1)
    with caplog.at_level(logging.WARNING):
        got = _load(tmp_path)
    assert got["AGENT_CONTEXT_TOKEN"] == ENV_TOKEN
    assert any("owner" in r.getMessage() and str(path) in r.getMessage() for r in caplog.records)
    assert LS_TOKEN not in caplog.text


def test_a_rejected_file_with_no_env_file_gives_no_token(tmp_path: Path) -> None:
    _ls_local(tmp_path, mode=0o644)
    assert _load(tmp_path) == {}




def test_an_unreadable_ls_local_file_falls_back_to_the_env_file(tmp_path: Path) -> None:
    _env_file(tmp_path)
    path = _ls_local(tmp_path)
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        assert _load(tmp_path)["AGENT_CONTEXT_TOKEN"] == ENV_TOKEN
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("content", [
    b"", b"\n\n", b"\xff\xfe\x00AGENT_CONTEXT_TOKEN=x\n", b"AGENT_CONTEXT_TOKEN\n",
    b"AGENT_CONTEXT_TOKEN=\n", b'AGENT_CONTEXT_TOKEN=""\n', b"# AGENT_CONTEXT_TOKEN=x\n",
    b"NOT_THE_KEY=abc\n"])
def test_an_empty_or_malformed_ls_local_file_falls_back_to_the_env_file(
        tmp_path: Path, content: bytes, caplog: pytest.LogCaptureFixture) -> None:
    _env_file(tmp_path)
    path = _dir(tmp_path) / "ls-local.env"
    path.write_bytes(content)
    path.chmod(0o600)
    with caplog.at_level(logging.WARNING):
        got = _load(tmp_path)
    assert got["AGENT_CONTEXT_TOKEN"] == ENV_TOKEN
    assert got["AGENT_CONTEXT_HOST"] == HOST




def test_the_token_never_reaches_a_log_line_on_any_path(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    caplog.set_level(logging.DEBUG)
    _env_file(tmp_path)
    ls = _ls_local(tmp_path)
    _load(tmp_path)                                    
    ls.chmod(0o644)
    _load(tmp_path)                                    
    ls.unlink()
    (_dir(tmp_path) / "ls-local.env").mkdir()
    _load(tmp_path)                                    
    (_dir(tmp_path) / "ls-local.env").rmdir()
    ls = _ls_local(tmp_path)
    uid = os.getuid()
    monkeypatch.setattr(relay_env.os, "getuid", lambda: uid + 1)
    _load(tmp_path)                                    
    assert LS_TOKEN not in caplog.text
    assert ENV_TOKEN not in caplog.text




def test_a_machine_without_the_ls_local_file_is_unchanged_and_silent(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _env_file(tmp_path)
    with caplog.at_level(logging.DEBUG):
        got = _load(tmp_path)
    assert got == {"AGENT_CONTEXT_HOST": HOST, "AGENT_CONTEXT_TOKEN": ENV_TOKEN}
    assert caplog.records == []




def test_an_ls_local_file_in_another_directory_is_never_consulted(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    env = other / "relay.env"
    env.write_text(f"AGENT_CONTEXT_HOST={HOST}\nAGENT_CONTEXT_TOKEN={ENV_TOKEN}\n")
    env.chmod(0o600)
    _ls_local(tmp_path)                                
    values = {"AGENT_CONTEXT_ENV_FILE": str(env)}
    relay_env.load_relay_env(values, home=tmp_path)
    assert values["AGENT_CONTEXT_TOKEN"] == ENV_TOKEN


def test_the_env_file_override_moves_the_ls_local_file_with_it(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    env = other / "relay.env"
    env.write_text(f"AGENT_CONTEXT_HOST={HOST}\nAGENT_CONTEXT_TOKEN={ENV_TOKEN}\n")
    env.chmod(0o600)
    ls = other / "ls-local.env"
    ls.write_text(f"AGENT_CONTEXT_TOKEN={LS_TOKEN}\n")
    ls.chmod(0o600)
    values = {"AGENT_CONTEXT_ENV_FILE": str(env)}
    relay_env.load_relay_env(values, home=tmp_path / "no-home")
    assert values["AGENT_CONTEXT_TOKEN"] == LS_TOKEN


def test_the_suite_never_reads_a_real_ls_local_file() -> None:
    'conftest points AGENT_CONTEXT_ENV_FILE at a missing file, and the ls-local file follows it.'
    target = os.environ.get("AGENT_CONTEXT_ENV_FILE")
    assert target
    assert not (Path(target).parent / "ls-local.env").exists()
    values = {"AGENT_CONTEXT_ENV_FILE": target}       
    relay_env.load_relay_env(values)
    assert values == {"AGENT_CONTEXT_ENV_FILE": target}




def test_the_stdio_relay_sees_the_ls_local_token(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / "relay.env"
    env.write_text(f"AGENT_CONTEXT_HOST={HOST}\nAGENT_CONTEXT_TOKEN={ENV_TOKEN}\n")
    env.chmod(0o600)
    ls = tmp_path / "ls-local.env"
    ls.write_text(f"AGENT_CONTEXT_TOKEN={LS_TOKEN}\n")
    ls.chmod(0o600)
    monkeypatch.setenv("AGENT_CONTEXT_ENV_FILE", str(env))
    seen: dict[str, str | None] = {}

    def ensure() -> str:
        seen["token"] = os.environ.get("AGENT_CONTEXT_TOKEN")
        return f"https://{HOST}/mcp"

    monkeypatch.setattr(daemon, "ensure_daemon", ensure)
    monkeypatch.setattr(daemon, "run_bridge", lambda: None)
    monkeypatch.setattr(S, "_materialize_if_remote", lambda: True)
    S.main()
    assert seen == {"token": LS_TOKEN}
