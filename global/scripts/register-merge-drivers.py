#!/usr/bin/env python3
"register-merge-drivers.py: the store's merge drivers, defined once.\n\nWhy this is its own script\n    The daemon runs the merges (its sync loop, every few minutes), so the daemon\n    registers the drivers. A driver registered only by a harness hook exists on a\n    machine only after a session there has ended a turn; on any other host git falls\n    back to its line merge and stops that machine's sync on the conflict the driver\n    exists to resolve. A guard installed in one publisher is not installed (memory\n    agent-context-store-sync-selfheal-and-alert).\n\n    So: one definition. The daemon calls it before each merge.\n\nUsage:  register-merge-drivers.py [store-root]\nExits 0 always. A machine that cannot register a driver still gets git's\nordinary merge, which is what it had before; failing the caller would be worse.\n\nObservations guarded: #400."

import os
import shlex
import subprocess
import sys


def _default_store():
    return os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(
        os.path.expanduser("~"), ".agent-context")


def _interpreter():
    "This host's absolute Python, from agent-python.py. Git runs a driver with the\n    PATH of whatever process merges, and a bare python3 there is 3.8 on the Synology\n    nodes (invariant interpreter-is-rendered). Falls back to the interpreter running\n    this script, because this script must never fail its caller."
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-python.py")
    try:
        spec = importlib.util.spec_from_file_location("agent_python", path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load " + path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.interpreter()
    except (ImportError, OSError):
        return sys.executable


def _config_get(key):
    proc = subprocess.run(["git", "config", key], capture_output=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")


def register_driver(key, display, command):
    
    
    
    
    
    
    
    
    
    
    
    
    
    if _config_get("merge.%s.driver" % key) == command:
        return
    subprocess.run(["git", "config", "merge.%s.name" % key, display],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "merge.%s.driver" % key, command],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def register_attr(line):
    
    
    
    proc = subprocess.run(["git", "rev-parse", "--git-common-dir"], capture_output=True,
                          encoding="utf-8", errors="replace")
    common = proc.stdout.rstrip("\n") if proc.returncode == 0 else ""
    attrs = common + "/info/attributes"
    if attrs == "/info/attributes":
        return
    try:
        with open(attrs, encoding="utf-8", errors="replace") as fh:
            existing = fh.read().splitlines()
    except OSError:
        existing = None
    if existing is not None and line in existing:
        return
    try:
        os.makedirs(os.path.dirname(attrs), exist_ok=True)
        with open(attrs, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main(argv):
    store = argv[1] if len(argv) > 1 and argv[1] != "" else _default_store()
    try:
        os.chdir(store)
    except OSError:
        return 0
    check = subprocess.run(["git", "rev-parse", "--git-dir"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode != 0:
        return 0
    py = shlex.quote(_interpreter())

    
    
    register_driver("acjsonunion", "agent-context JSON id-union",
                    py + " " + store + "/global/scripts/merge-json-union.py %O %A %B %P")

    
    
    
    register_driver("acdocnewer", "agent-context generated-doc newer-wins",
                    py + " " + store + "/global/scripts/merge-doc-newer.py %O %A %B %P")

    
    
    register_driver("acmetastamp", "agent-context meta sidecar newer-stamp",
                    py + " " + store + "/global/scripts/merge-meta-stamp.py %O %A %B %P")

    
    
    
    register_driver("acstatenewer", "agent-context state newer last_run",
                    py + " " + store + "/global/scripts/merge-state-newer.py %O %A %B %P")
    register_attr("global/state/*.json merge=acstatenewer")

    
    
    register_driver("acobservation", "agent-context audit observation fieldwise",
                    py + " " + store + "/global/scripts/merge-observation.py %O %A %B %P")
    register_attr("global/audit-observations/*.json merge=acobservation")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
