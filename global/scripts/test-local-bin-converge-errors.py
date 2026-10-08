#!/usr/bin/env python3
'The bash original has no `set -e` and its last statement is `[ "$QUICK" = 1 ] || echo "done."`,\nso under --quick it is LITERALLY IMPOSSIBLE for the script to exit non-zero: bash\'s exit code is\nthe last command\'s, and `[ "$QUICK" = 1 ]` is always true when --quick was passed. Every write\nfailure in every repo -- a locked .git directory, a full disk, a read-only mount -- is silently\ndiscarded. The Python port inherited this by construction, not by a specific bug: most of its\n`git config` writes run in "pass" mode (git\'s own stderr shows) but the return code is never\nchecked, and the two `--unset-all` calls (url, pushurl) run in "silent" mode, discarding stderr\noutright, matching the original\'s `2>/dev/null || true`.\n\nThe fix must: (1) report a failed config write on stderr, naming the store, the `git config`\nargs and git\'s own message; (2) keep converging the rest of that repo and every other repo\n(never stop early); (3) exit non-zero once every repo has been tried, if any write failed; (4) an\n`--unset-all` on a key that was never set (git\'s own "nothing to unset", exit 5) is not a\nfailure -- that is the documented first-run no-op the original\'s `|| true` already tolerated, and\ntreating it as one would make --quick print and fail on every brand-new repo.\n\nRenders the chezmoi .tmpl via `chezmoi execute-template`, the same way test-local-bin-fixes.py\ndoes, then runs it as a real subprocess against a fixture\nHOME with two real (local, remote-less) git repos at the first two [git_mirror] path_suffixes\n(chezmoi, agent-context) -- one left writable, one with its .git directory chmod 555 so every\nconfig write into it fails with git\'s own "could not lock config file: Permission denied".\n\n    python3 test-local-bin-converge-errors.py [--source DIR]\n\nExit 0 when every test passes, 1 otherwise.'
import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback

SOURCE = os.path.join(os.path.expanduser("~"), ".local", "share", "chezmoi")
PORT_REL = os.path.join("dot_local", "bin", "executable_git-mirror-converge.tmpl")

GIT = shutil.which("git") or "/usr/bin/git"
WRITABLE_REL = os.path.join(".local", "share", "chezmoi")  
LOCKED_REL = ".agent-context"  
CANONICAL_FETCH = "+refs/heads/*:refs/remotes/origin/*"





_CHEZMOI_FALLBACKS = [
    os.path.join(os.path.expanduser("~"), ".local", "bin", "chezmoi"),
    "/opt/homebrew/bin/chezmoi",
    "/usr/local/bin/chezmoi",
    "/opt/bin/chezmoi",
    "/opt/sbin/chezmoi",
    "/snap/bin/chezmoi",
    "/usr/bin/chezmoi",
]


def _chezmoi_bin(fallbacks=_CHEZMOI_FALLBACKS):
    "chezmoi's executable, or None when it is on neither PATH nor a fallback location."
    found = shutil.which("chezmoi")
    if found:
        return found
    for p in fallbacks:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


class PortMissing(Exception):
    pass


def render(port, source, dest):
    if not os.path.isfile(port):
        raise PortMissing("no port at %s" % port)
    with open(port, encoding="utf-8") as fh:
        body = fh.read()
    chezmoi = _chezmoi_bin()
    if chezmoi is None:
        raise PortMissing("chezmoi not found on PATH or in " + ", ".join(_CHEZMOI_FALLBACKS))
    proc = subprocess.run([chezmoi, "--source", source, "execute-template"], input=body,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise PortMissing("cannot render %s: %s" % (port, proc.stderr.strip()[:300]))
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(proc.stdout)
    os.chmod(dest, 0o755)


def run(port, home, args):
    env = dict(os.environ, HOME=home, PATH=os.path.dirname(GIT) + ":/usr/bin:/bin")
    return subprocess.run([sys.executable, port] + list(args), cwd=home, env=env,
                          capture_output=True, text=True, timeout=30)


def _git(home, repo_rel, args):
    return subprocess.run([GIT, "-C", os.path.join(home, repo_rel)] + args,
                          capture_output=True, text=True)


def fetch_values(home, repo_rel):
    proc = _git(home, repo_rel, ["config", "--get-all", "remote.origin.fetch"])
    return [v for v in proc.stdout.split("\n") if v]


def two_repo_fixture(tmp, lock_second=True):
    home = os.path.join(tmp, "home")
    for rel in (WRITABLE_REL, LOCKED_REL):
        repo = os.path.join(home, rel)
        os.makedirs(repo)
        subprocess.run([GIT, "init", "-q", repo], check=True)
    if lock_second:
        os.chmod(os.path.join(home, LOCKED_REL, ".git"), 0o555)
    return home


def unlock(home):
    'Locked-mode dirs must be writable again before TemporaryDirectory can clean up.'
    path = os.path.join(home, LOCKED_REL, ".git")
    if os.path.isdir(path):
        os.chmod(path, 0o755)





def test_locked_repo_write_failure_is_reported_and_exit_is_nonzero(port, tmp):
    home = two_repo_fixture(tmp)
    try:
        proc = run(port, home, ["--quick"])
        assert proc.returncode != 0, \
            "exit 0 despite a locked .git directory; stderr: %r" % proc.stderr
        assert "remote.origin.url" in proc.stderr, \
            "failing key not named on stderr: %r" % proc.stderr
        assert LOCKED_REL in proc.stderr or "Permission denied" in proc.stderr, \
            "git's own message/store path not on stderr: %r" % proc.stderr
    finally:
        unlock(home)


def test_run_continues_to_the_other_repo(port, tmp):
    home = two_repo_fixture(tmp)
    try:
        run(port, home, ["--quick"])
        values = fetch_values(home, WRITABLE_REL)
        assert CANONICAL_FETCH in values, \
            "the writable repo (after the locked one) was not converged: %r" % values
    finally:
        unlock(home)


def test_clean_run_prints_nothing_and_exits_zero(port, tmp):
    home = two_repo_fixture(tmp, lock_second=False)
    proc = run(port, home, ["--quick"])
    assert proc.returncode == 0, "exit %r, stderr %r" % (proc.returncode, proc.stderr)
    assert proc.stdout == "", "unexpected stdout on --quick success: %r" % proc.stdout
    assert proc.stderr == "", "unexpected stderr on --quick success: %r" % proc.stderr


def test_second_clean_run_is_still_silent(port, tmp):
    "A fresh repo's first run hits --unset-all on a key that was never set (git's own\n    documented no-op, exit 5) -- confirms that is never treated as a failure, on either run."
    home = two_repo_fixture(tmp, lock_second=False)
    first = run(port, home, ["--quick"])
    second = run(port, home, ["--quick"])
    assert first.returncode == 0, "first run: exit %r, stderr %r" % (first.returncode, first.stderr)
    assert first.stderr == "", "first run (fresh repo, nothing to unset): stderr %r" % first.stderr
    assert second.returncode == 0, "second run: exit %r, stderr %r" % (second.returncode, second.stderr)
    assert second.stderr == "", "second run: stderr %r" % second.stderr


TESTS = [test_locked_repo_write_failure_is_reported_and_exit_is_nonzero,
         test_run_continues_to_the_other_repo, test_clean_run_prints_nothing_and_exits_zero,
         test_second_clean_run_is_still_silent]


def main(argv):
    p = argparse.ArgumentParser(prog="test-local-bin-converge-errors.py")
    p.add_argument("--source", default=SOURCE)
    opts = p.parse_args(argv)
    port_src = os.path.join(opts.source, PORT_REL)
    failed = 0
    with tempfile.TemporaryDirectory(prefix="converge-errors-") as root:
        rendered = os.path.join(root, "git-mirror-converge")
        try:
            render(port_src, opts.source, rendered)
        except PortMissing as exc:
            for test in TESTS:
                print("FAIL %s: %s" % (test.__name__, exc))
            print("\n%d passed, %d failed" % (0, len(TESTS)))
            return 1
        for test in TESTS:
            tmp = tempfile.mkdtemp(dir=root)
            try:
                test(rendered, tmp)
                print("ok   %s" % test.__name__)
            except AssertionError as exc:
                failed += 1
                print("FAIL %s: %s" % (test.__name__, exc))
            except Exception:
                failed += 1
                print("FAIL %s: raised\n%s" % (test.__name__, traceback.format_exc()))
    print("\n%d passed, %d failed" % (len(TESTS) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
