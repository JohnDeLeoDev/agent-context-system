#!/usr/bin/env python3
"A project wt-finish-core.py passes the landing invariants by delegating to the shared\ncore only when it makes a real run_project call and loads a path naming\nwt_finish_core.py. A file that mentions both only in a string or a comment is flagged.\n\nCovers landing-gate-judges-relevance, neutral-worktree-layout and\nside-effecting-script-parses-its-flags, plus the core's own two markers.\n\nUsage: test-invariant-wt-finish-delegation.py"

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.dirname(os.path.dirname(HERE))
INVS = ("landing-gate-judges-relevance", "neutral-worktree-layout",
        "side-effecting-script-parses-its-flags")

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def load():
    os.environ["AGENT_CONTEXT_STORE"] = STORE
    spec = importlib.util.spec_from_file_location("invariant_check_wtf", os.path.join(HERE, "invariant-check.py"))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load invariant-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GOOD = (
    "import importlib.util, os, sys\n"
    "path = os.path.join(os.path.dirname(__file__), 'wt_finish_core.py')\n"
    "spec = importlib.util.spec_from_file_location('wt_finish_core', path)\n"
    "core = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(core)\n"
    "sys.exit(core.run_project(sys.modules[__name__], None))\n"
)
DEAD_STRING = (
    "import subprocess, sys\n"
    "NOTE = 'loads wt_finish_core.py and calls run_project(module, project)'\n"
    "subprocess.run(['./gradlew', sys.argv[1] if len(sys.argv) > 1 else ''])\n"
)
COMMENT_ONLY = (
    "# see wt_finish_core.py: run_project(module, project)\n"
    "import subprocess\n"
    "subprocess.run(['git', 'push'])\n"
)
NO_LOADER = (
    "import sys\n"
    "import wt_finish_core as core  # no path to the core, so nothing proves which one\n"
    "sys.exit(core.run_project(sys.modules[__name__], None))\n"
)


def main():
    mod = load()
    inv = {i.id: i for i in mod.REGISTRY}
    for name in INVS:
        check(name + " is registered", name in inv)
    if failures:
        return 1
    real = os.path.join(STORE, "projects", "example-app", "scripts", "wt-finish-core.py")
    fake = os.path.join(STORE, "projects", "Fixture", "scripts", "wt-finish-core.py")
    core = os.path.join(STORE, "global", "scripts", "wt_finish_core.py")

    print("[1] delegating files pass")
    check("GOOD fixture delegates", mod._delegates_to_wt_finish_core(GOOD))
    for name in INVS:
        check("GOOD fixture passes " + name, inv[name].violated(fake, GOOD) is None)
    if os.path.isfile(real):
        src = mod._read(real)
        check("example-app's wt-finish-core.py delegates", mod._delegates_to_wt_finish_core(src))
        for name in INVS:
            check("example-app passes " + name, inv[name].violated(real, src) is None,
                  str(inv[name].violated(real, src)))

    print("[2] mentions without calls are flagged")
    for label, src in (("DEAD_STRING", DEAD_STRING), ("COMMENT_ONLY", COMMENT_ONLY),
                       ("NO_LOADER", NO_LOADER), ("unparsable", "def (:\n")):
        check(label + " does not delegate", not mod._delegates_to_wt_finish_core(src))
        for name in INVS:
            check(label + " is flagged by " + name, bool(inv[name].violated(fake, src)))

    print("[3] the core carries both markers")
    if os.path.isfile(core):
        csrc = mod._read(core)
        rel = inv["landing-gate-judges-relevance"]
        neu = inv["neutral-worktree-layout"]
        check("core is a relevance site", core in rel.sites())
        check("core is a neutral-worktree site", core in neu.sites())
        check("core passes relevance", rel.violated(core, csrc) is None)
        check("core passes neutral-worktree", neu.violated(core, csrc) is None)
        check("core without GATE_RELEVANCE is flagged",
              bool(rel.violated(core, csrc.replace("GATE_RELEVANCE", "X"))))
        check("core without hp.HARNESS_DIRNAMES is flagged",
              bool(neu.violated(core, csrc.replace("hp.HARNESS_DIRNAMES", "X"))))

    print("\n%d passed, %d failed" % (passed, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
