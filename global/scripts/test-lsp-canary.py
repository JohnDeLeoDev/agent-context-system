#!/usr/bin/env python3
"Battery for lsp-canary.py's argument handling.\n\nEvery case runs against a throwaway HOME, so no probe can reach a real daemon and no\nverdict can land in the real health directory.\n\nRun: python3 test-lsp-canary.py"
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CANARY = os.path.join(HERE, "lsp-canary.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if detail and not cond else ""))


def main():
    home = os.path.realpath(tempfile.mkdtemp(prefix="canary-test-"))
    try:
        proj = os.path.join(home, "proj")
        os.makedirs(proj)
        store = os.path.join(home, ".agent-context", "global")
        os.makedirs(store)
        with open(os.path.join(store, "lsp-canaries.json"), "w") as fh:
            json.dump({"canaries": {"proj": [{"server": "zz-ls", "symbol": "Thing"}]}}, fh)
        verdicts = os.path.join(home, ".claude", "state", "health", "lsp")
        env = dict(os.environ, HOME=home)

        def run(*args, cwd=proj):
            
            shutil.rmtree(verdicts, ignore_errors=True)
            p = subprocess.run([sys.executable, CANARY] + list(args), cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=120)
            return p.returncode, p.stdout, p.stderr

        def written():
            try:
                return sorted(os.listdir(verdicts))
            except OSError:
                return []

        print("lsp-canary argument handling\n")
        for flag in ("--help", "-h"):
            rc, out, err = run(flag)
            check("%s inside a canary project: exit 0 with usage" % flag,
                  rc == 0 and "usage" in (out + err).lower(), "rc=%s out=%r err=%r"
                  % (rc, out[:200], err[:200]))
            check("%s writes no verdict" % flag, written() == [], "wrote %s" % written())

        rc, out, err = run("--bogus")
        check("an unknown flag is refused with exit 2 naming it",
              rc == 2 and "--bogus" in err, "rc=%s err=%r" % (rc, err[:200]))
        check("... and writes no verdict", written() == [], "wrote %s" % written())

        rc, out, err = run(proj, proj)
        check("a second argument is refused with exit 2", rc == 2, "rc=%s err=%r"
              % (rc, err[:200]))
        check("... and writes no verdict", written() == [], "wrote %s" % written())

        
        elsewhere = os.path.join(home, "elsewhere")
        os.makedirs(elsewhere)
        rc, out, err = run(elsewhere, cwd=elsewhere)
        check("a cwd with no configured canary still exits 0 and writes nothing",
              rc == 0 and written() == [], "rc=%s wrote %s" % (rc, written()))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
