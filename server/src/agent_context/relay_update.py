'Which release a relay runs, and the file it hands its session through when it switches.\n\nA relay moves onto a new release in place the moment ls serves one (relay_swap, policy), so\nthis module is bookkeeping:\n\n  - `start_etag`: the release this process runs, sent in its identity headers and compared with\n    the release the daemon names;\n  - the handover file a relay writes before it execs onto a new release and the next process\n    reads once.'
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("agent-context")


def etag_path(home: Path) -> Path:
    return home / ".local" / "share" / "agent-context" / "relay-source.etag"


RELAY_ETAG_MAX_LENGTH = 200


def valid_etag(value: str) -> str:
    '`value` when it is 1 to 200 printable ASCII characters (space to tilde), else "" (no etag).'
    if 0 < len(value) <= RELAY_ETAG_MAX_LENGTH and all(" " <= c <= "~" for c in value):
        return value
    return ""


def _installed_etag(home: Path) -> str:
    'The ETag the installer last stored, or "" when there is none or it is not a valid etag.'
    try:
        return valid_etag(etag_path(home).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return ""


_START_ETAGS: dict[Path, str] = {}
_START_ETAGS_LOCK = threading.Lock()


RELEASE_FILE = "agent-context-release"  


def _release_etag() -> str:
    'The ETag the installer wrote into the release directory this python runs from, or "".'
    try:
        return valid_etag((Path(sys.prefix) / RELEASE_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return ""


def start_etag(home: Path | None = None) -> str:
    'The ETag of the release this process runs, "" when unknown: its release directory\'s own\n    record (policy), else the ETag installed when this process first asked for `home`. Later calls\n    return the same value whatever the file holds now, so every header this relay sends names the\n    code it runs.'
    root = home if home is not None else Path.home()
    with _START_ETAGS_LOCK:
        if root not in _START_ETAGS:
            _START_ETAGS[root] = _release_etag() or _installed_etag(root)
        return _START_ETAGS[root]






def write_resume_file(state: dict) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    folder = base / "agent-context"
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="relay-swap-", suffix=".json", dir=folder)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    return Path(name)


def drop_resume_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def take_resume_file(variable: str, env: dict | None = None) -> dict | None:
    'The JSON object in the file `variable` names, popped from `env` and deleted once read;\n    None when there is none or it cannot be read.'
    environ = os.environ if env is None else env
    name = environ.pop(variable, None)
    if not name:
        return None
    try:
        data = json.loads(Path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("agent-context: relay swap state unreadable: %s", type(e).__name__)
        return None
    finally:
        Path(name).unlink(missing_ok=True)
    return data if isinstance(data, dict) else None
