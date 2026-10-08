'C5 hardening: which AGENT_CONTEXT_HOST values are loopback, and which are unusable.\n\nFound by review of the C5 commit. Loopback is judged the way the OS does (case-insensitive\nname, trailing dot, any 127.0.0.0/8 or ::1 address). A remote value that cannot be a\ndestination (a scheme, a port, a path, the 0.0.0.0 bind address) fails fast naming the\nvariable, rather than producing a malformed URL that fails later.'
import pytest

from agent_context import daemon


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_PORT", "AGENT_CONTEXT_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("host", ["LOCALHOST", "Localhost", "localhost.", "::1", "127.0.0.2"])
def test_loopback_spellings_are_local(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", host)
    assert daemon.is_remote() is False


def test_ipv6_loopback_url_is_bracketed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", "::1")
    assert daemon.mcp_url() == "http://[::1]:8765/mcp"


@pytest.mark.parametrize("host", ["http://ls.example.net", "ls.example.net:8765",
                                  "ls.example.net/mcp", "0.0.0.0"])
def test_unusable_remote_host_fails_fast_naming_the_variable(
        monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", host)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "t")
    with pytest.raises(RuntimeError, match="AGENT_CONTEXT_HOST"):
        daemon.ensure_daemon()


def test_a_plain_remote_name_is_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_HOST", "example.invalid")
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "t")
    assert daemon.ensure_daemon() == "https://example.invalid/mcp"
