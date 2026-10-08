"C9b: settings for a stdio relay that no shell started.\n\nThe zsh and fish loaders export AGENT_CONTEXT_HOST, AGENT_CONTEXT_TOKEN and AGENT_CONTEXT_PORT from\n~/.config/agent-context/env, but a relay launched by a GUI harness (Claude Desktop, Xcode) inherits\nno shell environment. `load_relay_env` fills those three from the same file when the environment\nlacks them. The environment wins. Nothing else in the file is read (it also holds the OAuth secrets\nand the allowed-hosts list), and no value is logged.\n\nIt also reads AGENT_CONTEXT_MACHINE_ID, which pins machine.get_machine_uuid() on a host whose OS\nuuid is not stable: WSL writes a new /etc/machine-id on reboot, and ls refuses a relay whose uuid\nnames no machine row.\n\nThe token has one more source: `ls-local.env`, next to the env file, whose AGENT_CONTEXT_TOKEN line\n(and nothing else in it) beats the env file's token. It is used only when it is a regular file, not a\nsymlink, owned by the current user, with no group or world permission bits. Anything else is ignored\nwith a warning that names the path and the reason, never a value. A machine without the file\nbehaves as before and logs nothing."
import contextlib
import logging
import os
import stat
from collections.abc import MutableMapping
from pathlib import Path

log = logging.getLogger("agent-context")

KEYS = ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_TOKEN", "AGENT_CONTEXT_PORT", "AGENT_CONTEXT_MACHINE_ID")
LS_LOCAL_NAME = "ls-local.env"
TOKEN_KEY = "AGENT_CONTEXT_TOKEN"


def default_env_file(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / ".config" / "agent-context" / "env"


def _parse(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        found[key] = value
    return found


def _problem(st: os.stat_result) -> str | None:
    'Why a file with this lstat is not trusted to hold the ls-local token, or None.'
    if stat.S_ISLNK(st.st_mode):
        return "is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if st.st_uid != os.getuid():
        return "has another owner"
    if st.st_mode & 0o077:
        return "has group or world permissions"
    return None


def _ls_local_token(path: Path) -> str | None:
    'The token line of the ls-local file, or None when it is absent, refused or unusable.'
    try:
        before = path.lstat()
    except OSError:
        return None                          
    reason = _problem(before)
    if reason:
        log.warning("agent-context: ignoring %s: it %s", path, reason)
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        after = os.fstat(fd)
        if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev) or _problem(after):
            return None                      
        with os.fdopen(fd, "r", closefd=False) as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    return _parse(text).get(TOKEN_KEY) or None


def load_relay_env(env: MutableMapping[str, str], home: Path | None = None) -> None:
    "Set each of KEYS from the env files when `env` has no non-empty value for it. The token comes\n    from ls-local.env first, then the env file. A missing, unreadable or non-text file changes\n    nothing. `AGENT_CONTEXT_ENV_FILE` overrides the env file's path; ls-local.env follows it."
    override = env.get("AGENT_CONTEXT_ENV_FILE")
    path = Path(override) if override else default_env_file(home)
    values: dict[str, str] = {}
    with contextlib.suppress(OSError, UnicodeDecodeError):
        values = _parse(path.read_text())
    try:
        ls_local = path.with_name(LS_LOCAL_NAME)
    except ValueError:                       
        ls_local = None
    token = _ls_local_token(ls_local) if ls_local is not None else None
    if token:
        values[TOKEN_KEY] = token
    for key, value in values.items():
        if not env.get(key):
            env[key] = value
