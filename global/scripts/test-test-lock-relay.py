#!/usr/bin/env python3
'A relay machine\'s ~/.agent-context is a projection with no lock records: every lock on a\nstore test is a record in the daemon\'s store on ls. Before this, the laptop\'s approval hook\nasked its own empty projection, read every store test as unlocked and refused the unlock\nuser wanted; and the daemon named the test by ls\'s path, /home/user/.agent-context/<key>,\nwhich no other host can resolve. These pin the four rules the fix was approved under:\n\n  1. The daemon is the only authority on a relay: a lock question goes to it, and when it\n     cannot answer, check, the approval hook and the unlock refuse. Nothing reads an\n     unanswered question as "not locked".\n  2. Another host\'s spelling maps only onto a file inside this store: a `..` key, a store\n     directory that exists on this host, and a symlink that leads out are not mapped.\n  3. A mapping can only find MORE locks, never fewer: every spelling of a locked test\n     reads as locked.\n  4. No spelling unlocks a test without user\'s Approve: the unlock still goes through\n     test-lock-consent, which on a relay forwards to the daemon and fails the grant when\n     the daemon cannot run it.\n\nRun: python3 test-test-lock-relay.py'
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(os.path.dirname(HERE), "hooks")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, "" if ok else " :: " + detail))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Relay:
    'A fixture store that the loaded test-lock takes for the live store of a relay\n    machine (no .git), plus a daemon stand-in that answers from a second fixture.'

    def __init__(self):
        base = os.path.realpath(tempfile.mkdtemp(prefix="tlr-", dir=os.path.expanduser("~/.cache")))
        self.base = base
        self.store = os.path.join(base, "home", ".agent-context")
        self.daemon = os.path.join(base, "daemon", ".agent-context")
        for root in (self.store, self.daemon):
            os.makedirs(os.path.join(root, "global", "scripts"))
        self.key = "global/scripts/test-locked.py"
        
        self.foreign = os.path.join(base, "foreign", ".agent-context", self.key)
        for root in (self.store, self.daemon):
            with open(os.path.join(root, self.key), "w") as fh:
                fh.write("assert True\n")
        self.tl = load(os.path.join(HERE, "test-lock.py"), "test_lock_relay_%d" % id(self))
        st = self.tl.store_task
        self.saved = (st.live_store, st.forward, st.in_server, os.environ.get("TEST_LOCK_STORE_ROOT"))
        st.live_store = lambda: self.store
        st.in_server = lambda: False
        os.environ["TEST_LOCK_STORE_ROOT"] = self.store
        self.calls = []
        self.answer = {"exit": 2, "stdout": "%s\t2026-10-01T00:00:00Z\n" % self.key, "stderr": ""}
        self.unreachable = False

        def forward(task, argv, stdin=None, deadline=None):
            self.calls.append((task, list(argv)))
            if self.unreachable:
                raise st.store_mcp.StoreUnreachable("the daemon did not answer")
            return dict(self.answer)
        st.forward = forward

    def close(self):
        st = self.tl.store_task
        st.live_store, st.forward, st.in_server, env = self.saved
        if env is None:
            os.environ.pop("TEST_LOCK_STORE_ROOT", None)
        else:
            os.environ["TEST_LOCK_STORE_ROOT"] = env
        shutil.rmtree(self.base, ignore_errors=True)


def test_mapping():
    print("another host's spelling")
    r = Relay()
    try:
        tl, store = r.tl, r.store
        foreign = "/home/nobody-%d/.agent-context/%s" % (os.getpid(), r.key)
        check("a store path in another host's home maps onto this store",
              tl.local_spelling(foreign) == os.path.join(store, r.key), tl.local_spelling(foreign))
        check("a path with no store directory in it is left alone",
              tl.local_spelling("/home/nobody/notes/x.py") == "/home/nobody/notes/x.py")
        here = os.path.join(r.daemon, r.key)
        check("a store directory that exists on this host keeps its own meaning",
              tl.local_spelling(here) == here, tl.local_spelling(here))
        for crafted in ("/home/nobody/.agent-context/../../etc/passwd",
                        "/home/nobody/.agent-context/global/../../../etc/passwd",
                        "/home/nobody/.agent-context/",
                        "/home/nobody/.agent-context/a/.agent-context/../../../x"):
            got = tl.local_spelling(os.path.normpath(crafted))
            inside = got.startswith(store + os.sep)
            check("%s never maps outside the store" % crafted,
                  not inside or os.path.realpath(got).startswith(store + os.sep), got)
        outside = os.path.join(r.base, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "x.py"), "w") as fh:
            fh.write("x\n")
        os.symlink(outside, os.path.join(store, "global", "out"))
        via = "/home/nobody/.agent-context/global/out/x.py"
        check("a symlink inside the store that leads out names no store key",
              tl.canonical_key(via) is None and not tl.in_live_store(via),
              "%r %r" % (tl.canonical_key(via), tl.in_live_store(via)))
    finally:
        r.close()


def test_daemon_is_the_authority():
    print("the daemon answers on a relay")
    r = Relay()
    try:
        tl = r.tl
        local = os.path.join(r.store, r.key)
        foreign = r.foreign
        for name, path in (("this host's spelling", local), ("another host's spelling", foreign)):
            r.calls.clear()
            got = tl.is_locked(path)
            check("%s of a locked test reads as locked, from the daemon" % name,
                  got is True and r.calls and r.calls[0][0] == "test-lock", "%r %r" % (got, r.calls))
        r.answer = {"exit": 0, "stdout": "", "stderr": ""}
        check("the daemon's 'not locked' is the answer", tl.is_locked(local) is False)
        r.answer = {"exit": 3, "stdout": "", "stderr": "store unreachable"}
        try:
            tl.is_locked(local)
            check("an exit other than 0 or 2 refuses", False, "returned instead of raising")
        except Exception:
            check("an exit other than 0 or 2 refuses", True)
        r.unreachable = True
        try:
            tl.is_locked(local)
            check("an unreachable daemon refuses, never reads as unlocked", False, "returned")
        except tl.store_task.store_mcp.StoreUnreachable:
            check("an unreachable daemon refuses, never reads as unlocked", True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = tl.main(["test-lock", "check", local])
        check("`test-lock check` exits 3 when the daemon cannot answer", code == 3, "exit %r" % code)
    finally:
        r.close()


def test_approval_hook_refuses_without_an_answer():
    print("the approval hook")
    r = Relay()
    fake = None
    try:
        aq = load(os.path.join(HOOKS, "approval-question.py"), "approval_question_relay")
        target = os.path.join(r.store, r.key)
        fake = os.path.join(r.base, "fake-test-lock.py")
        for mode, want_refusal in (("locked", False), ("unlocked", True), ("raise", True), ("old", True)):
            body = {"locked": "def is_locked(p):\n    return True\n",
                    "unlocked": "def is_locked(p):\n    return False\n",
                    "raise": "def is_locked(p):\n    raise OSError('daemon unreachable')\n",
                    "old": "def locked_entry(p):\n    return (object(), p)\n"}[mode]
            with open(fake, "w") as fh:
                fh.write(body)
            aq.LOCK_TOOL = fake
            why = aq.target_problem("test-unlock", target)
            check("is_locked %s -> %s" % (mode, "refused" if want_refusal else "accepted"),
                  bool(why) == want_refusal, repr(why))
    finally:
        r.close()


def test_unlock_forwards_and_fails_closed():
    print("the unlock")
    r = Relay()
    try:
        consent = load(os.path.join(HERE, "test-lock-consent.py"), "test_lock_consent_relay")
        consent.LOCK_TOOL = os.path.join(HERE, "test-lock.py")
        st = consent.store_task
        saved = (st.live_store, st.forward, st.in_server, consent.STATE_DIR, consent.LOG)
        st.live_store = lambda: r.store
        st.in_server = lambda: False
        consent.STATE_DIR = os.path.join(r.base, "state")
        consent.LOG = os.path.join(consent.STATE_DIR, "log")
        forwarded = []

        def forward(task, argv, stdin=None, deadline=None):
            forwarded.append((task, list(argv)))
            raise st.store_mcp.StoreUnreachable("the daemon did not answer")
        st.forward = forward
        try:
            foreign = r.foreign
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = consent.main([foreign])
            check("an unlock of a store path on a relay goes to the daemon",
                  forwarded and forwarded[0][0] == "test-lock-consent", repr(forwarded))
            check("an unreachable daemon fails the unlock (exit 3, no 'Unlocked:')",
                  code == 3 and "Unlocked:" not in out.getvalue(), "%r %r" % (code, out.getvalue()))
        finally:
            st.live_store, st.forward, st.in_server, consent.STATE_DIR, consent.LOG = saved
    finally:
        r.close()



def test_store_root_routes_to_daemon():
    print("the store root on a relay")
    r = Relay()
    try:
        tl = r.tl
        foreign_root = "/home/nobody-%d/.agent-context" % os.getpid()
        check("the bare foreign store root maps on the authority",
              tl.real(foreign_root) == r.store)
        check("an existing foreign store root keeps its meaning",
              tl.local_spelling(r.daemon) == r.daemon)
        saved_authority = (tl.checkout_root, tl.load)
        tl.checkout_root = lambda path: r.store if path == r.store else None
        tl.load = lambda root: {"root": root, "files": {}, "shared": False}
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = tl.cmd_status(foreign_root)
            check("authority status resolves the bare relay root", code == 0)
        finally:
            tl.checkout_root, tl.load = saved_authority
        check("the live store root belongs to the live tree", tl.in_live_store(r.store))
        check("status of the live store root forwards", tl._forwards("status", [r.store]))
        r.answer = {"exit": 0, "stdout": r.key + "\n", "stderr": ""}
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = tl.main(["test-lock", "status", r.store])
        check("root status returns the daemon answer",
              code == 0 and r.calls == [("test-lock", ["status", r.store])]
              and r.key in out.getvalue(), repr(r.calls))
        consent = load(os.path.join(HERE, "test-lock-consent.py"), "test_lock_consent_root")
        check("an explicit all request for the store root routes to the daemon",
              consent._touches_live("all", [r.store]))
        r.unreachable = True
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = tl.main(["test-lock", "status", r.store])
        check("root status fails closed when the daemon cannot answer", code == 3)
        check("a sibling directory is outside the live tree",
              not tl.in_live_store(r.store + "-other"))
    finally:
        r.close()

def main():
    test_store_root_routes_to_daemon()
    test_mapping()
    test_daemon_is_the_authority()
    test_approval_hook_refuses_without_an_answer()
    test_unlock_forwards_and_fails_closed()
    passed = sum(RESULTS)
    print("\n%d passed, %d failed" % (passed, len(RESULTS) - passed))
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
