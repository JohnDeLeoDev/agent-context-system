#!/usr/bin/env python3
'test-lock-consent.py: YOU (the human) run this to unlock acceptance tests an agent locked.\n\ntest-lock.py locks the tests a task agreed on before implementation.\nblock-locked-test-edit then refuses edits to them, and locked-test-drift-gate\nrefuses to end a turn while one differs from its lock. Run this when a locked\ntest is itself wrong. Every unlock is logged. Agents ask with a structured question,\nand the approval-question or codex-test-unlock hook runs this when you pick Approve;\nblock-consent-self-grant refuses an agent that runs it itself. This buys friction\nand an audit trail, not cryptographic authority.\n\nUsage:\n  python3 ~/.agent-context/global/scripts/test-lock-consent.py <file>...       # unlock these files\n  python3 ~/.agent-context/global/scripts/test-lock-consent.py --all [<dir>]   # unlock the checkout holding <dir>\n  python3 ~/.agent-context/global/scripts/test-lock-consent.py --list [<dir>]  # show locks\n  python3 ~/.agent-context/global/scripts/test-lock-consent.py --log           # show the unlock log'
import datetime
import importlib.util
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_task  




HELP_TEXT = """test-lock-consent.py: YOU (the human) run this to unlock acceptance tests an agent locked.

test-lock.py locks the tests a task agreed on before implementation.
block-locked-test-edit then refuses edits to them, and locked-test-drift-gate
refuses to end a turn while one differs from its lock. Run this when a locked
test is itself wrong. Every unlock is logged. Agents ask with a structured question,
and the approval-question or codex-test-unlock hook runs this when you pick Approve;
block-consent-self-grant refuses an agent that runs it itself. This buys friction and an
audit trail, not cryptographic authority.

Usage:
  python3 ~/.agent-context/global/scripts/test-lock-consent.py <file>...       # unlock these files
  python3 ~/.agent-context/global/scripts/test-lock-consent.py --all [<dir>]   # unlock the checkout holding <dir>
  python3 ~/.agent-context/global/scripts/test-lock-consent.py --list [<dir>]  # show locks
  python3 ~/.agent-context/global/scripts/test-lock-consent.py --log           # show the unlock log
"""

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.join(os.environ.get("HOME", ""), ".local", "state"),
    "agent-context")
LOG = os.path.join(STATE_DIR, "test-lock-consent.log")
LOCK_TOOL = os.environ.get("TEST_LOCK_TOOL") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "test-lock.py")


def _unlock(mode, targets):
    spec = importlib.util.spec_from_file_location("test_lock", LOCK_TOOL)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % LOCK_TOOL)
    tl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tl)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(LOG, "a", encoding="utf-8") as fh:
        
        
        fh.write("%s\tAPPROVED-UNLOCK\t%s\t%s\n" %
                 (stamp, mode, "\t".join(targets)))
        fh.flush()
        os.fsync(fh.fileno())

        removed = []
        if mode == "all":
            root = tl.checkout_root(tl.real(targets[0] if targets else "."))
            if not root:
                print("error: not inside a git checkout", file=sys.stderr)
                return 1
            doc = tl.load(root)
            keys = sorted(doc["files"])
            removed = [(root, rel) for rel in keys]
            doc["files"] = {}
            tl.save(doc)
            
            
            
            if hasattr(tl, "bound_docs"):
                for rel in keys:
                    _key, hits = tl.bound_docs(os.path.join(root, rel))
                    for other_doc, other_rel in hits:
                        if other_rel in other_doc["files"]:
                            del other_doc["files"][other_rel]
                            tl.save(other_doc)
                            removed.append((other_doc["root"], other_rel))
        else:
            
            
            for target in targets:
                
                
                
                key, hits = tl.bound_docs(target) if hasattr(tl, "bound_docs") else (None, [])
                if hits:
                    for doc, rel in hits:
                        del doc["files"][rel]
                        tl.save(doc)
                        removed.append((doc["root"], rel))
                    continue
                hit = tl.locked_entry(target)
                if not hit:
                    print("not locked: %s" % target)
                    continue
                doc, rel = hit
                del doc["files"][rel]
                tl.save(doc)
                removed.append((doc["root"], rel))

        for root, rel in removed:
            fh.write("%s\tUNLOCK\t%s\n" % (stamp, os.path.join(root, rel)))
        fh.flush()
        os.fsync(fh.fileno())
    for root, rel in removed:
        print("Unlocked: %s" % os.path.join(root, rel))
    if not removed:
        print("Nothing was unlocked.")
    return 0


def _touches_live(mode, rest):
    ' touches live.'
    if not store_task.targets_live_store(os.environ.get("TEST_LOCK_STORE_ROOT")):
        return False
    spec = importlib.util.spec_from_file_location("test_lock", LOCK_TOOL)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % LOCK_TOOL)
    tl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tl)
    
    
    
    if mode == "all":
        return tl.in_live_store(rest[0] if rest else ".")
    return any(tl.in_live_store(f) for f in rest)


def _forward(argv):
    "Forward this invocation to the daemon's test-lock-consent task; print and exit\n    as if it had run here."
    try:
        result = store_task.forward("test-lock-consent", argv)
    except (store_task.store_mcp.StoreUnreachable, store_task.store_mcp.ToolError) as exc:
        sys.stderr.write("test-lock-consent: the store did not run this task (%s)\n" % exc)
        return store_task.UNREACHABLE_EXIT
    sys.stdout.write(result.get("stdout") or "")
    sys.stderr.write(result.get("stderr") or "")
    sys.stdout.flush()
    return int(result.get("exit") or 0)


def main(argv):
    os.makedirs(STATE_DIR, exist_ok=True)

    first = argv[0] if argv else ""
    if first in ("", "--help", "-h"):
        print(HELP_TEXT)
        return 0
    if first in ("--log", "-l"):
        if os.path.isfile(LOG) and os.path.getsize(LOG) > 0:
            with open(LOG, encoding="utf-8", errors="replace") as fh:
                sys.stdout.write(fh.read())
        else:
            print("Nothing has been unlocked yet.")
        return 0
    if first == "--list":
        target = argv[1] if len(argv) > 1 else "."
        sys.stdout.flush()
        proc = subprocess.run([sys.executable, LOCK_TOOL, "status", target])
        return proc.returncode

    mode = "files"
    rest = argv
    if rest and rest[0] == "--all":
        mode = "all"
        rest = rest[1:]
    next_first = rest[0] if rest else ""
    if next_first.startswith("-"):
        print("error: unknown flag %s" % next_first, file=sys.stderr)
        return 2

    if not store_task.in_server() and _touches_live(mode, rest):
        return _forward(argv)
    return _unlock(mode, rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
