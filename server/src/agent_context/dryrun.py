"Run a store writer without touching the store: `dry_run=True` on every writer tool.\n\nThe writer runs for real, guards included, against a throwaway copy of the store's files\n(no remotes, no daemon callbacks, no commit timer). The files that copy gained, lost or\nchanged are what the live store would have changed. The live store is never written.\n\nA copy, because the writers do not use one write path: entities go through the store, audit\nrecords through `paths.write_atomic`, machine rows through the session module, project markers\ninto a checkout. Running the real code on a copy needs no knowledge of any of them. The copy\ntakes about 0.1 s for a 90 MB store.\n\nThe copy carries the git metadata (refs, index, config) and reads the live object database\nthrough `objects/info/alternates`, so the guards that ask git a question (the stale-write\nguard, the audit id floor read from every ref) answer as they would for the real write, and\nanything git writes lands in the copy."
from __future__ import annotations

import contextvars
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

_ACTIVE: contextvars.ContextVar[Any] = contextvars.ContextVar("agent_context_dry_run_store",
                                                             default=None)



_TOP_LEVEL_SKIPPED = {".git", ".agents", "server"}

_GIT_SKIPPED = {"objects", "hooks", "worktrees", "modules", "lfs"}


def active_store() -> Any:
    'The throwaway store a dry run is using in this context, or None.'
    return _ACTIVE.get()


def active() -> bool:
    'True while a dry run is in progress in this context.'
    return _ACTIVE.get() is not None


def _files(root: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root:
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def _copy_ignore(live_root: str) -> Callable[[str, list[str]], set[str]]:
    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {n for n in names if n == "__pycache__"}
        if os.path.abspath(directory) == os.path.abspath(live_root):
            skipped |= _TOP_LEVEL_SKIPPED & set(names)
        return skipped
    return ignore


def _copy_git(live_root: Path, root: Path) -> None:
    live_git = live_root / ".git"
    if not live_git.is_dir():
        return
    git = root / ".git"
    shutil.copytree(live_git, git, symlinks=True,
                    ignore=lambda _d, names: _GIT_SKIPPED & set(names))
    (git / "objects" / "info").mkdir(parents=True)
    (git / "objects" / "info" / "alternates").write_text(f"{live_git / 'objects'}\n")


def _same_bytes(live: Path, copy: Path) -> bool:
    try:
        return live.read_bytes() == copy.read_bytes()
    except OSError:
        return False


def _dumps(result: Any, changes: list[dict[str, str]]) -> str:
    return json.dumps({"dry_run": True, "would_change": changes, "result": result},
                      separators=(",", ":"), default=str)


def run(store: Any, call: Callable[[], str]) -> str:
    'JSON `{dry_run: true, would_change: [{path, action}], result}` for `call()` run against\n    a throwaway copy of `store`. `call` reaches the copy through `server._get_conn()`.'
    from .store import ContextStore
    live = Path(store.root)
    scratch = Path(tempfile.mkdtemp(prefix="agent-context-dry-run-"))
    try:
        root = scratch / "store"
        try:
            shutil.copytree(live, root, symlinks=True, ignore=_copy_ignore(str(live)))
            _copy_git(live, root)
        except (OSError, shutil.Error) as exc:
            return _dumps({"error": f"dry run could not copy the store: {exc}"}, [])
        before = _files(root)
        from . import write_guard
        token = _ACTIVE.set(ContextStore(root=str(root)))
        dry = write_guard.DRY_RUN.set(True)
        try:
            raw = call()
        finally:
            write_guard.DRY_RUN.reset(dry)
            _ACTIVE.reset(token)
            write_guard.unregister_root(root)
        after = _files(root)
        changes: list[dict[str, str]] = []
        for rel in sorted(before.keys() | after.keys()):
            if rel not in before:
                changes.append({"path": rel, "action": "create"})
            elif rel not in after:
                changes.append({"path": rel, "action": "delete"})
            elif before[rel] != after[rel] and not _same_bytes(live / rel, root / rel):
                changes.append({"path": rel, "action": "update"})
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            result = raw
        return _dumps(result, changes)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
