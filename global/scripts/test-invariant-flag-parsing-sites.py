#!/usr/bin/env python3
'side-effecting-script-parses-its-flags sees every runner and writer, not a hand-named few.\n\n  [1] every script matching the convention in the real store is a site\n  [2] every real site passes the static check\n  [3] in a throwaway store, a fixture runner that ignores --help is a site and is flagged; a\n      fixture reporter whose name matches no convention is not a site\n  [4] every real site that passes [2] answers --help with rc 0 and refuses an unknown flag with\n      rc 2 at runtime. A site that fails [2] is never run, so a bare live path is never reached.\n      Runs use a GIT_DIR that does not exist, so a broken fix cannot write git state.\n\nUsage: test-invariant-flag-parsing-sites.py'

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")
STORE = os.path.dirname(os.path.dirname(HERE))
INV = "side-effecting-script-parses-its-flags"



CONVENTION = re.compile(
    r"^(verify-.*|.*-run|.*-canary|.*-materialize|.*-commit|.*-sync|release-.*|.*-bootstrap"
    r"|refresh-.*|self-heal|.*-sweep|.*-prune|.*-install|.*-deploy.*|.*-reset|.*-finish.*"
    r"|.*-seed|.*-apply)\.py$")

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


def load(store):
    saved = os.environ.get("AGENT_CONTEXT_STORE")
    os.environ["AGENT_CONTEXT_STORE"] = store
    try:
        spec = importlib.util.spec_from_file_location("invariant_check_%d" % len(store), CHECK)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load " + CHECK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is None:
            os.environ.pop("AGENT_CONTEXT_STORE", None)
        else:
            os.environ["AGENT_CONTEXT_STORE"] = saved


def invariant(mod):
    return next((i for i in mod.REGISTRY if i.id == INV), None)


def candidates(mod):
    out = []
    for p in mod._scripts("py") + mod._project_scripts("py") + mod._project_hooks("py"):
        b = os.path.basename(p)
        if b.startswith("test-") or "port-cases" in b:
            continue
        if CONVENTION.match(b):
            out.append(p)
    return sorted(out)


def run(path, flag):
    env = dict(os.environ, GIT_DIR=os.path.join(tempfile.gettempdir(), "no-such-git-dir"),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    try:
        r = subprocess.run([sys.executable, path, flag], capture_output=True, text=True,
                           timeout=30, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "timed out after 30 s"
    return r.returncode, (r.stdout + r.stderr)[-300:]


def real_store():
    mod = load(STORE)
    inv = invariant(mod)
    check(INV + " is registered", inv is not None)
    if inv is None:
        return
    sites = sorted(inv.sites())
    want = candidates(mod)

    print("[1] every convention-named script is a site")
    for name in ("hook-test-run.py", "verify-quality-system.py", "lsp-canary.py"):
        check("%s is a site" % name, any(os.path.basename(s) == name for s in sites))
    missing = [os.path.relpath(p, STORE) for p in want if p not in sites]
    check("all %d convention-named scripts are sites" % len(want), not missing,
          "missing: " + ", ".join(missing))

    print("[2] every real site passes the static check")
    clean = []
    for p in sites:
        verdict = inv.violated(p, mod._read(p))
        check(os.path.relpath(p, STORE) + " parses its flags", not verdict, str(verdict))
        if not verdict:
            clean.append(p)

    print("[4] a passing site answers --help and refuses an unknown flag")
    for p in clean:
        rel = os.path.relpath(p, STORE)
        rc, tail = run(p, "--help")
        check(rel + " --help exits 0", rc == 0, "rc %r: %s" % (rc, tail))
        rc, tail = run(p, "--no-such-flag-phase8")
        check(rel + " refuses --no-such-flag-phase8 with rc 2", rc == 2, "rc %r: %s" % (rc, tail))


def fixture_store():
    print("[3] a fixture runner that ignores --help is flagged")
    tmp = tempfile.mkdtemp(prefix="flag-parsing-sites-")
    try:
        scripts = os.path.join(tmp, "store", "global", "scripts")
        os.makedirs(scripts)
        os.makedirs(os.path.join(tmp, "store", "global", "hooks"))
        os.makedirs(os.path.join(tmp, "store", "projects"))
        runner = os.path.join(scripts, "fixture-suite-run.py")
        reporter = os.path.join(scripts, "fixture-report.py")
        body = ("import subprocess, sys\n"
                "ONLY = sys.argv[1] if len(sys.argv) > 1 else ''\n"
                "subprocess.run([sys.executable, 'suite.py', ONLY])\n")
        for path in (runner, reporter):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        mod = load(os.path.join(tmp, "store"))
        inv = invariant(mod)
        if inv is None:
            check(INV + " is registered in the fixture store", False)
            return
        sites = inv.sites()
        check("fixture-suite-run.py is a site", runner in sites, repr(sites))
        check("fixture-suite-run.py is flagged", bool(inv.violated(runner, mod._read(runner))))
        check("fixture-report.py is not a site", reporter not in sites, repr(sites))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for section in (real_store, fixture_store):
        try:
            section()
        except Exception as exc:
            check(section.__name__ + " ran to the end", False,
                  "raised %s: %s" % (type(exc).__name__, exc))
    print("\n%d passed, %d failed" % (passed, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
