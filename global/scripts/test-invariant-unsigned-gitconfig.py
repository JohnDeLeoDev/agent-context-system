#!/usr/bin/env python3
'Criteria: every spelling that turns commit signing off is flagged; server/tests is\nscanned; signing turned on, and a comment that only names the pattern, are not flagged.\nSame PASS/FAIL style and exit code as the other batteries.'
import importlib.util
import os
import sys

STORE = os.path.expanduser("~/.agent-context")
spec = importlib.util.spec_from_file_location(
    "invariant_check_unsigned", os.path.join(STORE, "global", "scripts", "invariant-check.py"))
assert spec is not None and spec.loader is not None, "cannot load invariant-check.py"
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

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


FLAGGED = {
    "a gitconfig file written with [commit] gpgsign = false":
        'fh.write("[user]\\n\\tname = t\\n[commit]\\n\\tgpgsign = false\\n")\n',
    "a gitconfig file with no spaces around the equals sign":
        'fh.write("[commit]\\n\\tgpgsign=false\\n")\n',
    "a -c flag in mixed case (commit.gpgSign=false)":
        'subprocess.run(["git", "-c", "commit.gpgSign=false", "commit", "-m", "x"])\n',
    "a -c flag in the original spelling (commit.gpgsign=false)":
        'subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-m", "x"])\n',
    "git config with a space (commit.gpgsign false)":
        'os.system("git config commit.gpgsign false")\n',
    "key and value as separate arguments":
        '_git(root, "config", "commit.gpgsign", "false")\n',
    "a (key, value) pair":
        'for k, v in (("user.name", "t"), ("commit.gpgsign", "false")):\n    _git(root, "config", k, v)\n',
    "a (key, value) pair with single quotes":
        "cfg = [('commit.gpgsign', 'false')]\n",
    "--no-gpg-sign":
        'subprocess.run(["git", "commit", "--no-gpg-sign", "-m", "x"])\n',
}

CLEAN = {
    "a gitconfig file that turns signing on":
        'fh.write("[commit]\\n\\tgpgsign = true\\n")\n',
    "a -c flag that turns signing on":
        'subprocess.run(["git", "-c", "commit.gpgsign=true", "commit", "-m", "x"])\n',
    "key and value as separate arguments, turning signing on":
        '_git(root, "config", "commit.gpgsign", "true")\n',
    "a comment that only names the pattern":
        '# never write commit.gpgsign=false or gpgsign = false here\nx = 1\n',
}

print("invariant no-unsigned-commit-fallback: pattern battery")
for label, src in FLAGGED.items():
    got = mod._has_unsigned_commit_fallback("fixture.py", src)
    check("flags " + label, bool(got), "got %r for %r" % (got, src))
for label, src in CLEAN.items():
    got = mod._has_unsigned_commit_fallback("fixture.py", src)
    check("leaves alone " + label, not got, "got %r for %r" % (got, src))

inv = next(i for i in mod.REGISTRY if i.id == "no-unsigned-commit-fallback")
tests_dir = os.path.join(STORE, "server", "tests") + os.sep
scanned = [p for p in inv.sites() if os.path.realpath(p).startswith(os.path.realpath(tests_dir))]
check("scans server/tests", bool(scanned), "no site under %s" % tests_dir)

print("\ninvariant-unsigned-gitconfig: %d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
