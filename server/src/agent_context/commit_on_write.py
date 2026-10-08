'Debounced commit and push after a store write.\n\nA write only changes the working tree. It is in history, and on the mirrors, only after a\ncommit and a push, and until this module the 5-minute sync loop did both. Each write now\narms a timer. One signed commit covers a burst of writes, and a second worker pushes it,\nso a write never waits on git and a hung mirror never delays the next commit.\n\nThe 5-minute loop stays as the safety net: whatever this defers (a refused signature, a\nmerge in progress, a dead mirror) it retries and reconciles.'
import logging
import threading
import time
from datetime import UTC, datetime

log = logging.getLogger("agent_context.commit_on_write")

_MESSAGE_PATHS = 8       
_PUSH_RETRY_SECS = 60.0


def _stamp():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _message(paths):
    names = sorted(paths)
    shown = ", ".join(names[:_MESSAGE_PATHS])
    more = f" +{len(names) - _MESSAGE_PATHS} more" if len(names) > _MESSAGE_PATHS else ""
    return f"write: {len(names)} file(s): {shown}{more}"


class CommitOnWrite:
    def __init__(self, store, *, debounce=3.0, cap=30.0, clock=time.monotonic, push=None,
                 push_retry_secs=_PUSH_RETRY_SECS):
        self.store = store
        self.debounce = debounce
        self.cap = cap
        self.clock = clock
        self.push = push
        self.push_retry_secs = push_retry_secs
        self._lock = threading.Lock()
        self._paths = set()
        self._first_at = None        
        self._last_at = None         
        self._retry_at = 0.0         
        self._unpushed = False
        self._next_push_at = 0.0
        self._push_failures = 0
        self._last_commit_at = None
        self._last_push_at = None
        self._last_commit_error = None
        self._last_push_error = None
        self._stop = threading.Event()
        self._push_wake = threading.Event()
        self._threads = []

    

    def note_write(self, paths=None):
        now = self.clock()
        with self._lock:
            self._paths.update(paths or [])
            if self._first_at is None:
                self._first_at = now
            self._last_at = now

    def _due(self, now):
        if self._first_at is None or now < self._retry_at:
            return False
        return now - self._last_at >= self.debounce or now - self._first_at >= self.cap

    

    def tick_commit(self):
        now = self.clock()
        with self._lock:
            if not self._due(now):
                return
            paths, first, last = self._paths, self._first_at, self._last_at
            self._paths, self._first_at, self._last_at = set(), None, None
        try:
            res = self.store.commit_local(_message(paths))
        except Exception as e:      
            res = {"commit_skipped": f"commit raised: {e}"}
        with self._lock:
            if "commit_skipped" in res:
                
                self._paths |= paths
                self._first_at = first if self._first_at is None else min(first, self._first_at)
                self._last_at = last if self._last_at is None else max(last, self._last_at)
                self._retry_at = now + self.debounce
                self._last_commit_error = res["commit_skipped"]
                log.warning("write-triggered commit deferred: %s", res["commit_skipped"])
                return
            self._last_commit_error = None
            if res.get("committed"):
                self._last_commit_at = _stamp()
                self._unpushed = True
                
                
                
                
                if self._push_failures == 0:
                    self._next_push_at = 0.0
        if res.get("committed"):
            self._push_wake.set()

    def tick_push(self):
        now = self.clock()
        with self._lock:
            if not self._unpushed or now < self._next_push_at:
                return
        try:
            res = (self.push or self.store.push_all)()
        except Exception as e:
            res = {"push": False, "push_error": {"push": str(e)}}
        with self._lock:
            if res.get("push"):
                self._unpushed = False
                self._push_failures = 0
                self._last_push_at = _stamp()
                self._last_push_error = None
                return
            
            self._push_failures += 1
            self._next_push_at = now + min(600.0, self.push_retry_secs
                                           * 2 ** min(self._push_failures - 1, 4))
            if res.get("push_error"):
                self._last_push_error = "; ".join(
                    f"{r}: {str(m).strip()[-120:]}" for r, m in res["push_error"].items())
            else:
                self._last_push_error = (res.get("push_deferred")
                                         or f"unsigned commit held: {res.get('unsigned_hold')}")
            log.warning("write-triggered push incomplete: %s", self._last_push_error)

    def tick(self):
        self.tick_commit()
        self.tick_push()

    

    def start(self):
        if self._threads:
            return
        self._stop.clear()
        interval = max(0.02, min(1.0, self.debounce / 4))

        def commit_loop():
            while not self._stop.wait(interval):
                self.tick_commit()

        def push_loop():
            while not self._stop.is_set():
                self._push_wake.wait(1.0)
                self._push_wake.clear()
                if not self._stop.is_set():
                    self.tick_push()

        self._threads = [threading.Thread(target=commit_loop, name="commit-on-write", daemon=True),
                         threading.Thread(target=push_loop, name="push-on-write", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        self._push_wake.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []

    def status(self):
        with self._lock:
            return {"pending": self._first_at is not None, "unpushed": self._unpushed,
                    "last_commit_at": self._last_commit_at, "last_push_at": self._last_push_at,
                    "last_commit_error": self._last_commit_error,
                    "last_push_error": self._last_push_error}
