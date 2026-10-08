"The daemon's write ledger: the store files it changed and has not committed yet (policy).\n\nOne pathway (policy) covers user too: a file edited in Obsidian, by hand or by any process\nother than this daemon is not a store write, so the daemon must not commit it. Before\nthis ledger every commit ran `git add -A` over the whole tree and swept such edits in.\nNow a commit stages exactly the dirty paths the ledger holds; everything else dirty is\nreported as an outside edit (`outside_edits` in get_health) and left alone.\n\nWhat lands in the ledger: every `paths.write_atomic` under a store root (the one atomic\nwriter, so every entity, record, machine-row and upload write), every path handed to\n`ContextStore._arm_commit` (deletes and moves included), and every file a server task\nchanged (store_tasks). An entry leaves when a commit carries it, or when the path is no\nlonger dirty. The ledger is saved in the daemon's state dir, so a write made just before a\nrestart is still committed after it."
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

from . import paths

log = logging.getLogger("agent_context.write_ledger")


class WriteLedger:
    def __init__(self, root: str, state_file: str | None):
        self.root = os.path.realpath(str(root))
        self.state_file = state_file
        self._state_real = os.path.realpath(state_file) if state_file else None
        self._lock = threading.RLock()
        self._paths: set[str] = set()
        if state_file:
            try:
                with open(state_file, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    self._paths = {p for p in data if isinstance(p, str)}
            except (OSError, ValueError):
                pass

    def _save(self) -> None:
        if not self.state_file:
            return
        try:
            paths.write_atomic(self.state_file, json.dumps(sorted(self._paths)) + "\n")
        except OSError as exc:
            log.warning("write ledger: could not save %s: %s", self.state_file, exc)

    def add(self, rels) -> None:
        new = {r.replace(os.sep, "/") for r in rels if r and not r.startswith("..")}
        with self._lock:
            if new <= self._paths:
                return
            self._paths |= new
            self._save()

    def discard(self, rels) -> None:
        gone = set(rels)
        with self._lock:
            if not gone & self._paths:
                return
            self._paths -= gone
            self._save()

    def snapshot(self) -> set[str]:
        with self._lock:
            return set(self._paths)

    def relative(self, path: str) -> str | None:
        "`path` relative to this store's root, or None when it lies outside."
        real = os.path.realpath(str(path))
        if real == self.root or not real.startswith(self.root + os.sep):
            return None
        return os.path.relpath(real, self.root).replace(os.sep, "/")


_LEDGERS: dict[str, WriteLedger] = {}
_REGISTRY_LOCK = threading.Lock()


def state_file_for(root: str) -> str:
    digest = hashlib.sha256(os.path.realpath(str(root)).encode()).hexdigest()[:16]
    return str(paths.state_dir() / "write-ledger" / f"{digest}.json")


def register(root) -> WriteLedger:
    'The ledger for `root`, created once per root per process.'
    real = os.path.realpath(str(root))
    with _REGISTRY_LOCK:
        ledger = _LEDGERS.get(real)
        if ledger is None:
            ledger = WriteLedger(real, state_file_for(real))
            _LEDGERS[real] = ledger
        return ledger


def note_path(path: str) -> None:
    "paths.write_atomic's listener: record a write that landed under a store root. A\n    ledger's own state file is never a store write, wherever the state dir sits."
    real = os.path.realpath(str(path))
    ledgers = list(_LEDGERS.values())
    if any(real == ledger._state_real for ledger in ledgers):
        return
    for ledger in ledgers:
        rel = ledger.relative(path)
        if rel is not None:
            ledger.add([rel])
            return


paths.set_write_note(note_path)
