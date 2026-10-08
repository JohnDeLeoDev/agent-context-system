
"C6: a relay on a non-ls machine fetches the daemon's materialized bundle over its\nget_materialized MCP tool and writes it locally (policy/policy: one pathway, MCP).\n\nThe bundle is `{key: content}`; a key's first path segment picks where it lands, in the\nlayout the store's own projection (home-materialize.py, harness_paths.py) uses:\n\n    skills/ commands/ agents/  ->  ~/.agent-context/global/<prefix>/\n    docs/                      ->  not written: read over MCP. A file an older relay wrote\n                                   under ~/.agent-context/shared-docs/ is retired.\n    hooks/ scripts/            ->  ~/.agent-context/global/<prefix>/\n    manifests/                 ->  ~/.agent-context/global/  (mcp-servers.json, hooks-manifest.json)\n    root/                      ->  ~/.agent-context/  (AGENTS.md only: see ROOT_FILES)\n    projects/ workspaces/      ->  ~/.agent-context/<prefix>/  (files only, mode 0644, never run)\n\nNothing here is a git clone: the ls daemon is the store's only writer. `settings.json` is not\nin the bundle; it is a per-machine merge, so the fetched home-settings-sync.py builds it.\nThe fetched home-materialize.py then projects skills, commands and agents into each\nharness's own home. Files the old relay wrote under ~/.claude/{skills,commands,agents} are\nswept once the neutral copy exists.\nThe raw bundle is cached for the ls-down path (C8) and doubles as the record of what this\nrelay wrote, so pruning never touches a file a person put there.\n\nEvery failure degrades to a logged warning: the MCP bridge must start regardless."

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import anyio
import anyio.to_thread

from . import daemon
from .materialize import MANIFEST_FILES, ROOT_FILES
from .paths import write_atomic

log = logging.getLogger("agent-context")

_ROOTS: dict[str, tuple[str, ...]] = {
    "skills": (".agent-context", "global", "skills"),
    "commands": (".agent-context", "global", "commands"),
    "agents": (".agent-context", "global", "agents"),
    "docs": (".agent-context", "shared-docs"),
    "hooks": (".agent-context", "global", "hooks"),
    "scripts": (".agent-context", "global", "scripts"),
    "manifests": (".agent-context", "global"),
    "root": (".agent-context",),
    "projects": (".agent-context", "projects"),
    "workspaces": (".agent-context", "workspaces"),
}
_EXECUTABLE = frozenset({"hooks", "scripts"})
_SETTINGS_SCRIPT = ("scripts", "home-settings-sync.py")
_MATERIALIZE_SCRIPT = ("scripts", "home-materialize.py")
_LEGACY_PREFIXES = ("skills", "commands", "agents")
_MIRRORED = ("skills", "commands", "agents", "hooks", "scripts")  
_PROJECTED_PREFIXES = ("skills/", "commands/", "agents/", "root/")
_DOCS_PREFIX = "docs"  


class BundleError(Exception):
    'The bundle could not be fetched, parsed, or applied safely.'


class FetchTimeout(Exception):
    'get_materialized did not answer inside the timeout. Unlike every other fetch failure\n    this says nothing about whether ls is up: a healthy daemon under load can be slow to\n    answer, and treating that as ls-down would serve a relay with no tools.'




_RETRY_TIMEOUTS = (30.0, 60.0, 120.0)

_PROBE_TIMEOUT = 5.0


@dataclass
class ApplyResult:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    legacy_pruned: list[str] = field(default_factory=list)
    blocked: bool = False




_WARNED_CHECKOUTS: set[Path] = set()


def is_git_checkout(home: Path) -> bool:
    "True when `home`'s .agent-context is itself a git checkout (`.git` a dir in a normal\n    clone, a file in a worktree) rather than a relay-only projection target."
    return (home / ".agent-context" / ".git").exists()


def _refuse_checkout_write(home: Path) -> bool:
    "True (and, once per process per root, a warning) when a write here would land inside a\n    git checkout instead of a relay-only projection.\n\n    A host whose ~/.agent-context is a fleet checkout while AGENT_CONTEXT_HOST points at\n    ls has is_remote() true, so materializing the bundle would overwrite the clone\n    (file modes and command/skill frontmatter) and the host's autosync would commit the\n    damage."
    if not is_git_checkout(home):
        return False
    root = home / ".agent-context"
    if root not in _WARNED_CHECKOUTS:
        _WARNED_CHECKOUTS.add(root)
        log.warning("relay bundle skipped: %s is a git checkout; a relay-only host must not "
                    "hold a clone", root)
    return True


def _unsafe_shape(key: str) -> bool:
    'A key that could name a path outside the layout, whatever its prefix.'
    parts = PurePosixPath(key).parts
    return (not key or key.startswith("/") or "\\" in key or "\0" in key or key.endswith("/")
            or len(parts) < 2 or any(p in ("", ".", "..") for p in key.split("/")))


def _is_unknown_prefix(key: str) -> bool:
    'A well-formed key this relay has no layout for: a newer bundle than this relay.'
    return not _unsafe_shape(key) and PurePosixPath(key).parts[0] not in _ROOTS


def _split(key: str) -> tuple[str, tuple[str, ...]]:
    '(prefix, remaining parts) for a safe bundle key; BundleError otherwise.'
    parts = PurePosixPath(key).parts
    if _unsafe_shape(key) or parts[0] not in _ROOTS:
        raise BundleError(f"unsafe or unknown bundle key {key!r}")
    
    if parts[0] == "root" and (len(parts) != 2 or parts[1] not in ROOT_FILES):
        raise BundleError(f"unsafe or unknown bundle key {key!r}")
    return parts[0], parts[1:]


def target_for(key: str, home: Path) -> Path:
    prefix, rest = _split(key)
    return home.joinpath(*_ROOTS[prefix], *rest)







_UNKNOWN_TOOL = "Unknown tool: get_materialized"


async def _call_get_materialized(mcp_url: str, token: str) -> dict[str, str] | None:
    "The bundle via the daemon's get_materialized MCP tool. None signals an old daemon that\n    lacks the tool; any other problem raises BundleError, so fetch_bundle_mcp can log the two\n    differently."
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    from . import identity

    
    
    headers = {**identity.local_headers(), **({"Authorization": f"Bearer {token}"} if token else {})}
    async with (create_mcp_http_client(headers=headers) as http,
                streamable_http_client(mcp_url, http_client=http) as (read, write, _),
                ClientSession(read, write) as session):
        await session.initialize()
        res = await session.call_tool("get_materialized", {})
        text = getattr(res.content[0], "text", "null") if res.content else "null"
        if res.isError:
            if text == _UNKNOWN_TOOL:
                return None
            raise BundleError("get_materialized answered with an error")
        bundle = json.loads(text)
        if not _is_string_map(bundle):
            raise BundleError("get_materialized did not return a {path: content} map")
        return bundle


def fetch_bundle_mcp(mcp_url: str, token: str, timeout: float) -> dict[str, str] | None:
    "The bundle via get_materialized. None on an old daemon that lacks the tool: quiet, since\n    that is the expected shape of a mixed fleet during the policy/policy rollout. None on every\n    other failure too -- a daemon that has the tool but answers with a problem, an unreachable\n    endpoint, a timeout, a malformed answer -- but those are logged at WARNING (the error's\n    class only, no body, no token), since they are not the transitional case and are worth\n    seeing. The one exception is a timeout, which raises FetchTimeout after the same warning:\n    a slow daemon is not a down one, and materialize_on_start must tell the two apart."
    try:
        result = asyncio.run(asyncio.wait_for(_call_get_materialized(mcp_url, token), timeout))
    except TimeoutError as e:
        log.warning("agent-context: get_materialized over MCP failed (%s)", type(e).__name__)
        raise FetchTimeout from None
    except Exception as e:
        log.warning("agent-context: get_materialized over MCP failed (%s)", type(e).__name__)
        return None
    if result is None:
        log.info("agent-context: daemon has no get_materialized tool (policy/policy compat)")
    return result


def _is_docs_key(key: str) -> bool:
    return PurePosixPath(key).parts[:1] == (_DOCS_PREFIX,)


def _without_docs(bundle: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in bundle.items() if not _is_docs_key(key)}


def _is_string_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items())


def cache_path(home: Path) -> Path:
    return home / ".cache" / "agent-context" / "materialized.json"


def write_cache(bundle: dict[str, str], home: Path) -> None:
    write_atomic(cache_path(home), json.dumps(bundle).encode(), 0o600)


def read_cache(home: Path) -> dict[str, str] | None:
    try:
        cached = json.loads(cache_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cached if _is_string_map(cached) else None


def _identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _check_writable(key: str, path: Path, home: Path) -> None:
    'A directory at the target, or a plain file where a parent directory belongs, would\n    fail mid-bundle and leave half of it written; refuse it up front.'
    if path.is_dir():
        raise BundleError(f"{key!r}: a directory is in the way at {path}")
    for parent in path.parents:
        if parent == home:
            break
        if os.path.lexists(parent) and not parent.is_dir():
            raise BundleError(f"{key!r}: {parent} is a file, not a directory")


def apply_bundle(bundle: dict[str, str], home: Path) -> ApplyResult:
    "Write the bundle under `home`, then prune what an earlier run wrote and the daemon\n    has since dropped. Every key is checked before the first write.\n\n    A well-formed key with a prefix this relay does not know is skipped with one warning, so a\n    bundle from a newer daemon still applies everything this relay understands. An unsafe key\n    refuses the whole bundle. `home`'s `.agent-context` being a git checkout refuses the whole\n    bundle too, before any write: see _refuse_checkout_write."
    if _refuse_checkout_write(home):
        return ApplyResult(blocked=True)
    skipped = sorted(key for key in bundle if _is_unknown_prefix(key))
    if skipped:
        log.warning("agent-context: skipped %d bundle key(s) this relay does not know; run "
                    "agent-context-relay-install to update it: %s", len(skipped),
                    ", ".join(skipped))
    bundle = {key: value for key, value in bundle.items() if key not in skipped}
    for key in bundle:
        _split(key)  
    bundle = _without_docs(bundle)
    targets = {key: (_split(key)[0], target_for(key, home)) for key in bundle}
    for key, (_prefix, path) in targets.items():
        _check_writable(key, path, home)
    previous_all = read_cache(home) or {}
    previous = _without_docs(previous_all)
    result = ApplyResult()
    _retire_docs(previous_all, home)
    for key, (prefix, path) in targets.items():
        data = bundle[key].encode()
        mode = 0o755 if prefix in _EXECUTABLE else 0o644
        try:
            same = path.read_bytes() == data
        except OSError:
            same = False
        if same:
            if path.stat().st_mode & 0o777 != mode:
                os.chmod(path, mode)
            result.unchanged.append(key)
        else:
            write_atomic(path, data, mode)
            result.written.append(key)
    
    written_ids = {_identity(path) for _prefix, path in targets.values()}
    for key in previous.keys() - bundle.keys():
        try:
            prefix = _split(key)[0]
        except BundleError:
            continue  
        path = target_for(key, home)
        if not path.is_file() and not path.is_symlink():
            continue
        with contextlib.suppress(OSError):  
            if _identity(path) in written_ids:
                continue
        path.unlink()
        result.pruned.append(key)
        root = home.joinpath(*_ROOTS[prefix])
        for parent in path.parents:
            if parent == root or not parent.is_relative_to(root):
                break
            try:
                parent.rmdir()
            except OSError:
                break
    result.pruned.extend(_sweep_strays(bundle, home, written_ids))
    result.pruned.sort()
    result.skipped = skipped
    result.legacy_pruned = _sweep_legacy(previous, bundle, home)
    return result


def _sweep_strays(bundle: dict[str, str], home: Path,
                  written_ids: set[tuple[int, int]]) -> list[str]:
    "Remove files under a relay-owned root that the bundle does not hold, so each root is an\n    exact copy of the bundle. The prune in apply_bundle removes only what the cache says an\n    earlier run wrote, so a file from before that cache (an older relay, a former clone)\n    would stay for good. A root the\n    bundle has no key for is left alone, since an older daemon's bundle may lack it, and so is\n    Python's own `__pycache__`."
    swept: list[str] = []
    for prefix in _MIRRORED:
        if not any(key.startswith(prefix + "/") for key in bundle):
            continue
        root = home.joinpath(*_ROOTS[prefix])
        if not root.is_dir() or root.is_symlink():
            continue
        for path in sorted(root.rglob("*"), reverse=True):  
            rel = path.relative_to(root)
            if "__pycache__" in rel.parts:
                continue
            if path.is_dir() and not path.is_symlink():
                with contextlib.suppress(OSError):
                    path.rmdir()  
                continue
            key = f"{prefix}/{rel.as_posix()}"
            if key in bundle:
                continue
            with contextlib.suppress(OSError):  
                if _identity(path) in written_ids:
                    continue
            path.unlink()
            swept.append(key)
    return swept


def _retire_docs(previous: dict[str, str], home: Path) -> None:
    'Remove the docs an older relay wrote under shared-docs/: a regular file, reached without\n    crossing a symlink, whose bytes still equal the previous cache record. A hand-edited file\n    and one the cache never listed stay; emptied directories, shared-docs/ included, go.'
    root = home.joinpath(*_ROOTS[_DOCS_PREFIX])
    for key in sorted(previous):
        if not _is_docs_key(key):
            continue
        try:
            path = target_for(key, home)
        except BundleError:
            continue  
        try:
            if (_crosses_symlink(root, path)
                    or not path.is_file() or path.read_bytes() != previous[key].encode()):
                continue
            path.unlink()
        except OSError:
            continue
        for parent in path.parents:
            if not parent.is_relative_to(root):
                break
            try:
                parent.rmdir()
            except OSError:
                break


def _crosses_symlink(top: Path, path: Path) -> bool:
    'True when `path`, or any directory from `top` down to it (`top` included), is a\n    symlink.'
    return any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(top))


def _sweep_legacy(previous: dict[str, str], bundle: dict[str, str], home: Path) -> list[str]:
    "Remove what the old relay wrote under ~/.claude/{skills,commands,agents}: a regular\n    file whose bytes still equal the previous cache record, for a key the neutral root now\n    holds. Edited files, symlinks and person files stay.\n\n    A file reached through a symlink below ~/.claude is never the old relay's. home-materialize\n    makes ~/.claude/<prefix> a symlink to a generation directory it built from the neutral root\n    (swap_projection_dir), and a projected file can carry the same bytes as its bundle\n    entry. Following that link would delete every projected file on each relay start and\n    push, and a home-materialize run that did not finish afterward would leave\n    ~/.claude/agents empty."
    swept: list[str] = []
    claude = home / ".claude"
    for key in sorted(previous.keys() & bundle.keys()):
        prefix = PurePosixPath(key).parts[0]
        if prefix not in _LEGACY_PREFIXES:
            continue
        path = claude / key
        try:
            if (_crosses_symlink(claude / prefix, path) or not path.is_file()
                    or path.read_bytes() != previous[key].encode()):
                continue
            path.unlink()
        except OSError:
            continue
        swept.append(key)
        root = home / ".claude" / prefix
        for parent in path.parents:
            if parent == root or not parent.is_relative_to(root):
                break
            try:
                parent.rmdir()
            except OSError:
                break
    return swept


def _run_home_script(home: Path, script_name: tuple[str, str], purpose: str,
                     timeout: float) -> bool:
    'Run a fetched script with HOME=`home`. A warning, never a raise, on any failure.'
    script = home.joinpath(*_ROOTS[script_name[0]], script_name[1])
    if not script.is_file():
        log.warning("agent-context: %s was not materialized; %s skipped", script, purpose)
        return False
    env = {k: v for k, v in os.environ.items() if k != "AGENT_CONTEXT_STORE"}
    env["HOME"] = str(home)
    try:
        done = subprocess.run(
            [sys.executable, str(script)], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("agent-context: %s did not finish (%s)", purpose, type(e).__name__)
        return False
    if done.returncode != 0:
        log.warning("agent-context: %s exited %d: %s", purpose, done.returncode,
                    (done.stderr or "").strip()[-300:])
        return False
    return True


def run_settings_sync(home: Path, timeout: float = 10.0) -> bool:
    "Run the fetched home-settings-sync.py against `home`. It merges the managed hooks,\n    env and permissions into ~/.claude/settings.json and keeps the machine's own keys."
    return _run_home_script(home, _SETTINGS_SCRIPT, "settings sync", timeout)


def run_home_materialize(home: Path, timeout: float = 10.0) -> bool:
    "Run the fetched home-materialize.py against `home`. It projects the neutral skills,\n    commands and agents into each harness's own home."
    return _run_home_script(home, _MATERIALIZE_SCRIPT, "home-materialize", timeout)


def refresh(home: Path | None = None, timeout: float = 10.0) -> ApplyResult | None:
    'Fetch the bundle, write it, cache it. The result, or None after a logged failure.\n\n    Never raises; a failed fetch leaves every local file and the cache untouched.'
    try:
        return _refresh(home, timeout)
    except FetchTimeout:
        log.warning("agent-context: materialize from %s failed: no bundle from get_materialized",
                    daemon.mcp_url())
        return None


def _refresh(home: Path | None, timeout: float) -> ApplyResult | None:
    'refresh(), except that a fetch timeout raises FetchTimeout instead of returning None.'
    token = os.environ.get("AGENT_CONTEXT_TOKEN")
    if not token:
        log.warning("agent-context: AGENT_CONTEXT_TOKEN is not set; skipping materialize")
        return None
    root = home if home is not None else Path.home()
    url = daemon.mcp_url()
    bundle = fetch_bundle_mcp(url, token, timeout)
    if bundle is None:
        log.warning("agent-context: materialize from %s failed: no bundle from get_materialized",
                    url)
        return None
    try:
        result = apply_bundle(bundle, root)
        if not result.blocked:
            
            
            write_cache(_without_docs({k: v for k, v in bundle.items()
                                       if k not in result.skipped}), root)
    except (BundleError, OSError) as e:
        log.warning("agent-context: materialize from %s failed: %s", url, e)
        return None
    log.info("agent-context: materialized %d written, %d unchanged, %d pruned",
             len(result.written), len(result.unchanged), len(result.pruned))
    return result


def materialize_on_start(home: Path | None = None, timeout: float = 10.0) -> bool:
    "Fetch the bundle, write it, cache it, sync settings. True when ls answered, False only\n    when the fetch failed: the caller serves the degraded, no-tools relay on False. A blocked\n    apply (a checkout root) writes and syncs nothing but still returns True, since ls is up.\n\n    A fetch that times out is not taken as ls down: if the daemon answers a plain\n    HTTP probe, this returns True so the session gets its tools, and the fetch is retried in\n    the background with longer timeouts. The files an earlier session wrote stay in place\n    meanwhile.\n\n    Never raises. Worst case is about twice `timeout` (fetch, then the settings run), which\n    stays inside the client's 30 s `initialize` budget at the default."
    root = home if home is not None else Path.home()
    try:
        result = _refresh(root, timeout)
    except FetchTimeout:
        url = daemon.mcp_url()
        if not daemon.daemon_answers(url, _PROBE_TIMEOUT):
            log.warning("agent-context: materialize from %s failed: get_materialized timed "
                        "out and the daemon does not answer", url)
            return False
        log.warning("agent-context: get_materialized timed out but %s answers; serving tools "
                    "and retrying the materialize in the background", url)
        _retry_in_background(root)
        return True
    if result is None:
        return False
    if not result.blocked:
        _sync_settings_and_harnesses(root, timeout)
    return True


def _sync_settings_and_harnesses(root: Path, timeout: float) -> None:
    if not run_settings_sync(root, timeout):
        log.warning("agent-context: files are materialized but settings.json was not synced")
    run_home_materialize(root, timeout)


def _retry_materialize(root: Path, timeouts: Sequence[float] = _RETRY_TIMEOUTS) -> bool:
    "The start-time materialize again, once per timeout, until a fetch does not time out.\n    True when a bundle was applied. Any failure other than a timeout ends the retries: that\n    daemon answered, and the refresher's next reconnect or push fetches again."
    for timeout in timeouts:
        try:
            result = _refresh(root, timeout)
        except FetchTimeout:
            continue
        if result is None:
            return False
        if not result.blocked:
            _sync_settings_and_harnesses(root, timeout)
        log.info("agent-context: background materialize succeeded after a start-time timeout")
        return True
    log.warning("agent-context: background materialize gave up after %d timeouts; the files "
                "from the last session stay in place", len(timeouts))
    return False


def _retry_in_background(root: Path) -> None:
    def work() -> None:
        try:
            _retry_materialize(root)
        except Exception as e:
            log.warning("agent-context: background materialize failed: %s", e)
    threading.Thread(target=work, daemon=True, name="relay-materialize-retry").start()


def apply_cached(home: Path | None = None, timeout: float = 10.0) -> bool:
    'Apply the bundle an earlier session cached, for a start with ls unreachable.\n\n    True when the cached bundle was applied. Never raises, never fetches, and leaves the\n    cache file alone: it still describes what this relay last wrote.'
    root = home if home is not None else Path.home()
    bundle = read_cache(root)
    if bundle is None:
        log.warning("agent-context: ls is unreachable and there is no cached bundle at %s; "
                    "nothing materialized", cache_path(root))
        return False
    try:
        result = apply_bundle(bundle, root)
    except (BundleError, OSError) as e:
        log.warning("agent-context: ls is unreachable and the cached bundle could not be "
                    "applied: %s", e)
        return False
    if result.blocked:
        return False
    log.warning("agent-context: ls is unreachable; materialized from the cached bundle at %s",
                cache_path(root))
    if not run_settings_sync(root, timeout):
        log.warning("agent-context: cached files are materialized but settings.json was "
                    "not synced")
    run_home_materialize(root, timeout)
    return True


_BUNDLE_STORE_DIRS = frozenset(("hooks", "scripts", "skills", "commands", "agents"))
_PROJECT_BUNDLE_DIRS = frozenset(
    ("commands", "skills", "agents", "scripts", "hooks", "parity", "ship", "project.toml"))
_WORKSPACE_BUNDLE_DIRS = frozenset(("skills", "commands", "workspace.toml"))
_SETTINGS_PREFIXES = ("hooks/", "scripts/")


def _path_affects_bundle(path: str) -> bool:
    '`path` is store-relative, as `content_changed` carries it; bundle keys drop `global/`.'
    parts = PurePosixPath(path).parts
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    if len(parts) == 2:
        return parts[0] == "global" and parts[1] in MANIFEST_FILES
    if len(parts) < 3:
        return False
    if parts[0] == "global":
        return parts[1] in _BUNDLE_STORE_DIRS
    if parts[0] == "workspaces":
        return parts[2] in _WORKSPACE_BUNDLE_DIRS
    return parts[0] == "projects" and parts[2] in _PROJECT_BUNDLE_DIRS


def affects_bundle(paths: Sequence[str]) -> bool:
    'True when a pushed store path can change what `/materialized` returns.'
    return any(_path_affects_bundle(p) for p in paths)


def needs_home_materialize(result: ApplyResult) -> bool:
    'The projection reads skills, commands and agents, so a change to one re-runs it.'
    return any(key.startswith(_PROJECTED_PREFIXES) for key in (*result.written, *result.pruned))


def needs_settings_sync(result: ApplyResult) -> bool:
    'settings.json is built from hooks and scripts, so only they force a re-sync.'
    return any(key.startswith(_SETTINGS_PREFIXES) for key in (*result.written, *result.pruned))


_REFRESH_LOCK = threading.Lock()


def refresh_now(home: Path, timeout: float) -> bool:
    'The files, the cache, then the settings re-sync and projection they call for. True\n    when ls answered. Kept whole so a cancelled task cannot leave new hooks with a stale\n    settings.json.'
    with _REFRESH_LOCK:
        result = refresh(home, timeout)
        if result is None:
            return False
        if needs_settings_sync(result) and not run_settings_sync(home, timeout):
            log.warning("agent-context: files were refreshed but settings.json was not synced")
        if needs_home_materialize(result):
            run_home_materialize(home, timeout)
        return True


def _refresh_and_sync(home: Path, timeout: float) -> None:
    'One ContentRefresher worker call.'
    refresh_now(home, timeout)


class ContentRefresher:
    'Turns `content_changed` pushes into one debounced re-fetch of the bundle.\n\n    `notify` is called on the event loop; `run` is the long-lived task that does the work.\n    The blocking fetch and file writes run in a worker thread that a cancel waits for, so\n    it is bounded by the two timeouts.'

    def __init__(self, home: Path | None = None, debounce: float = 1.0,
                 timeout: float = 10.0) -> None:
        self.home = home if home is not None else Path.home()
        self.debounce = debounce
        self.timeout = timeout
        self._wake = anyio.Event()

    def notify(self, paths: Sequence[str]) -> None:
        if affects_bundle(paths):
            self._wake.set()

    def request(self) -> None:
        'Ask for a re-fetch with no pushed path to filter on: a reconnect follows an\n        outage, and pushes sent during it are lost.'
        self._wake.set()

    async def run(self) -> None:
        while True:
            await self._wake.wait()
            await anyio.sleep(self.debounce)  
            self._wake = anyio.Event()
            try:
                await anyio.to_thread.run_sync(_refresh_and_sync, self.home, self.timeout)
            except Exception as e:  
                log.warning("agent-context: content refresh failed: %s", e)
