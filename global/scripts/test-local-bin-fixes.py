#!/usr/bin/env python3
'Renders the touched chezmoi .tmpl with `chezmoi execute-template`, then runs the rendered port as a real subprocess against a\nfixture HOME with a real (local, remote-less) git repo, the way wrapper-parity-run.py\'s\nfixtures do, and inspects the repo\'s git config afterward.\n\nBug covered: git-mirror-converge\'s `remote.origin.fetch` write. Every host\'s agent-context\nstore clone carries a second fetch refspec (`+refs/fleet/*:refs/fleet/origin/*`, for the\ninter-agent inbox), so `remote.origin.fetch` is already multi-valued there. The pre-fix port\ncalled `git config remote.origin.fetch <value>` unconditionally, which git refuses on a\nmulti-valued key ("error: cannot overwrite multiple values with a single value") -- printed to\nstderr but never checked, so the run still exited 0. The fix reads the existing values first\nand only `--add`s the canonical spec when it is not already present, so a multi-valued key\nnever hits the single-value write and no fetch value is ever dropped.\n\nThe Phase 7a and 7b "kept for parity" quirks named in consolidation/plan.md (the 7b review\'s\n"Kept: build-launch-shims exits 128+N on INT/TERM", the 7a/7b --help divergences, and the\nsyno-maintenance non-numeric-stamp divergence) were audited against the\nplan doc, the ported .tmpl files and wrapper-port-cases-p7a/-p7a-edges/-p7b.py and found to be\neither correct POSIX signal-exit convention or already resolved as documented, tested\nDIVERGENCES (not silent parity bugs) -- see the delivery report for the evidence per item.\nNothing there needed a new failing case.\n\n    python3 test-local-bin-fixes.py [--source DIR]\n\n--source defaults to the chezmoi source at ~/.local/share/chezmoi. Exit 0 when every test\npasses, 1 otherwise.'
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

SOURCE = os.path.join(os.path.expanduser("~"), ".local", "share", "chezmoi")
PORT_REL = os.path.join("dot_local", "bin", "executable_git-mirror-converge.tmpl")

GIT = shutil.which("git") or "/usr/bin/git"
CANONICAL_FETCH = "+refs/heads/*:refs/remotes/origin/*"
FLEET_FETCH = "+refs/fleet/*:refs/fleet/origin/*"
REPO_REL = os.path.join(".local", "share", "chezmoi")  





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
    first_line = body.split("\n", 1)[0]
    if first_line.startswith("#!") and "python" not in first_line and "{{" not in first_line:
        raise PortMissing("%s is still the sh original" % port)
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


def _git(home, args):
    return subprocess.run([GIT, "-C", os.path.join(home, REPO_REL)] + args,
                          capture_output=True, text=True)


def fetch_values(home):
    proc = _git(home, ["config", "--get-all", "remote.origin.fetch"])
    return [v for v in proc.stdout.split("\n") if v]


def fixture(tmp):
    'A fixture HOME holding one managed repo at the "chezmoi" entry\'s path_suffix\n    (~/.local/share/chezmoi, REPOS[0] in the rendered port) -- the only one --quick needs,\n    since --quick never dials a remote.'
    home = os.path.join(tmp, "home")
    repo = os.path.join(home, REPO_REL)
    os.makedirs(repo)
    subprocess.run([GIT, "init", "-q", repo], check=True)
    return home





def test_multivalued_fetch_no_error_and_fleet_kept(port, tmp):
    home = fixture(tmp)
    _git(home, ["config", "remote.origin.fetch", CANONICAL_FETCH])
    _git(home, ["config", "--add", "remote.origin.fetch", FLEET_FETCH])
    proc = run(port, home, ["--quick"])
    assert proc.returncode == 0, "exit %r, stderr %r" % (proc.returncode, proc.stderr)
    assert "cannot overwrite multiple values" not in proc.stderr, \
        "multi-value git config error leaked to stderr: %r" % proc.stderr
    values = fetch_values(home)
    assert CANONICAL_FETCH in values, "canonical fetch spec missing: %r" % values
    assert FLEET_FETCH in values, "fleet fetch spec dropped: %r" % values
    assert values.count(CANONICAL_FETCH) == 1, "canonical fetch spec duplicated: %r" % values


def test_second_run_is_idempotent(port, tmp):
    home = fixture(tmp)
    _git(home, ["config", "remote.origin.fetch", CANONICAL_FETCH])
    _git(home, ["config", "--add", "remote.origin.fetch", FLEET_FETCH])
    run(port, home, ["--quick"])
    before = fetch_values(home)
    proc = run(port, home, ["--quick"])
    after = fetch_values(home)
    assert proc.returncode == 0, "exit %r, stderr %r" % (proc.returncode, proc.stderr)
    assert "cannot overwrite multiple values" not in proc.stderr
    assert before == after, "second run changed the fetch values: %r -> %r" % (before, after)


def test_fresh_repo_still_gets_the_canonical_spec(port, tmp):
    home = fixture(tmp)
    proc = run(port, home, ["--quick"])
    assert proc.returncode == 0, "exit %r, stderr %r" % (proc.returncode, proc.stderr)
    values = fetch_values(home)
    assert values == [CANONICAL_FETCH], "fresh-repo fetch spec: %r" % values


def test_unrelated_custom_fetch_value_is_not_dropped(port, tmp):
    'A single custom fetch value that is neither the canonical spec nor the fleet spec is\n    left alone and the canonical spec is added beside it -- the fix only ever adds the\n    canonical entry, it never rewrites or clears remote.origin.fetch wholesale (unlike\n    remote.origin.url/pushurl, which this port does reset from scratch each run).'
    home = fixture(tmp)
    _git(home, ["config", "remote.origin.fetch", "+refs/tags/*:refs/tags/*"])
    proc = run(port, home, ["--quick"])
    assert proc.returncode == 0, "exit %r, stderr %r" % (proc.returncode, proc.stderr)
    values = fetch_values(home)
    assert CANONICAL_FETCH in values, "canonical fetch spec missing: %r" % values
    assert "+refs/tags/*:refs/tags/*" in values, "unrelated custom fetch spec dropped: %r" % values


TESTS = [test_multivalued_fetch_no_error_and_fleet_kept, test_second_run_is_idempotent,
         test_fresh_repo_still_gets_the_canonical_spec, test_unrelated_custom_fetch_value_is_not_dropped]


def main(argv):
    p = argparse.ArgumentParser(prog="test-local-bin-fixes.py")
    p.add_argument("--source", default=SOURCE)
    opts = p.parse_args(argv)
    port_src = os.path.join(opts.source, PORT_REL)
    failed = 0
    with tempfile.TemporaryDirectory(prefix="local-bin-fixes-") as root:
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
