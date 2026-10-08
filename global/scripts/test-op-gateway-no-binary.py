#!/usr/bin/env python3
'Acceptance tests for the op gateway\'s "no real binary" exit, the one path no parity case reaches.\n\nThe contract, in the rendered port (module-level, importable, main guarded by __name__):\n\n  CANDIDATES = ("/opt/homebrew/bin/op", "/usr/local/bin/op", "/opt/bin/op", "/usr/bin/op")\n    The fixed paths tried after the words of $OP_REAL, in order, read by main() when it runs.\n  main(argv) -> int\n    The gateway, argv without the program name; its own path is sys.argv[0]. With no\n    executable candidate other than itself it appends one access-log line\n    `<ts> rc=127 mode=no-op-binary ... argv=[<args>]` (0600), prints\n    "op: no 1Password CLI found (looked in /opt/homebrew/bin, /usr/local/bin, /opt/bin, /usr/bin)"\n    on stderr and returns 127.\n\nEvery run passes `--version`, a call that never touches a vault, so even a port that ignored the\nreplaced list and found the real CLI could not spend quota or raise a prompt.\n\n    python3 test-op-gateway-no-binary.py [--port FILE] [--source DIR]\n\n--port defaults to the chezmoi source\'s dot_local/bin/executable_op.tmpl; it is rendered with\n`chezmoi execute-template` (a file whose first line is a sh shebang is no port).\nExit 0 when every test passes, 1 otherwise.'
import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback

SOURCE = os.path.join(os.path.expanduser("~"), ".local", "share", "chezmoi")
PORT_REL = os.path.join("dot_local", "bin", "executable_op.tmpl")
FIXED = ("/opt/homebrew/bin/op", "/usr/local/bin/op", "/opt/bin/op", "/usr/bin/op")
MESSAGE = "op: no 1Password CLI found (looked in /opt/homebrew/bin, /usr/local/bin, /opt/bin, /usr/bin)\n"





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



DRIVER = """
import importlib.machinery, importlib.util, sys
path, candidates, args = sys.argv[1], sys.argv[2], sys.argv[3:]
loader = importlib.machinery.SourceFileLoader("op_port", path)
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
mod.CANDIDATES = tuple(c for c in candidates.split(":") if c)
sys.argv = [path] + args
sys.exit(mod.main(args))
"""


class PortMissing(Exception):
    pass


def render(port, source, dest):
    if not os.path.isfile(port):
        raise PortMissing("no port at %s" % port)
    with open(port, encoding="utf-8") as fh:
        body = fh.read()
    first = body.split("\n", 1)[0]
    if first.startswith("#!") and "python" not in first and "{{" not in first:
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


def load_candidates(path):
    proc = subprocess.run([sys.executable, "-c",
                           "import importlib.machinery, importlib.util, sys\n"
                           "l = importlib.machinery.SourceFileLoader('op_port', sys.argv[1])\n"
                           "m = importlib.util.module_from_spec(importlib.util.spec_from_loader(l.name, l))\n"
                           "l.exec_module(m)\n"
                           "print(repr(tuple(getattr(m, 'CANDIDATES', ()))), repr(callable(getattr(m, 'main', None))))",
                           path], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise PortMissing("cannot import %s: %s" % (path, proc.stderr.strip()[-300:]))
    return proc.stdout.strip()


def run(port, tmp, candidates, extra_env=None, args=("--version",)):
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    log = os.path.join(tmp, "state", "access.log")
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "OP_ACCESS_LOG": log}
    env.update(extra_env or {})
    proc = subprocess.run([sys.executable, "-c", DRIVER, port, ":".join(candidates)] + list(args),
                          env=env, cwd=home, capture_output=True, text=True, timeout=30)
    return proc, log


def nowhere(tmp):
    return [os.path.join(tmp, "nowhere", d, "op") for d in ("opt-homebrew", "usr-local", "opt", "usr")]


def read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()




def test_candidates_are_the_four_fixed_paths(port, tmp):
    got = load_candidates(port)
    assert got == "%r True" % (FIXED,), "CANDIDATES and main(): %s" % got


def test_no_binary_exits_127_with_the_message(port, tmp):
    proc, _log = run(port, tmp, nowhere(tmp))
    assert proc.returncode == 127, "rc %r, expected 127; stderr %r" % (proc.returncode, proc.stderr)
    assert proc.stderr == MESSAGE, "stderr %r, expected %r" % (proc.stderr, MESSAGE)
    assert proc.stdout == "", "stdout %r, expected nothing" % proc.stdout


def test_no_binary_is_logged_once_0600(port, tmp):
    proc, log = run(port, tmp, nowhere(tmp))
    assert proc.returncode == 127, "rc %r, expected 127" % proc.returncode
    assert os.path.isfile(log), "no access log at %s" % log
    lines = read(log).splitlines()
    assert len(lines) == 1, "expected one log line, got %r" % lines
    assert " rc=127 mode=no-op-binary " in lines[0], "log line %r" % lines[0]
    assert lines[0].endswith(" argv=[--version]"), "log line %r" % lines[0]
    mode = stat.S_IMODE(os.stat(log).st_mode)
    assert mode == 0o600, "log mode %o, expected 600" % mode


def test_a_candidate_that_is_not_executable_is_no_binary(port, tmp):
    candidate = os.path.join(tmp, "plain", "op")
    os.makedirs(os.path.dirname(candidate))
    with open(candidate, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(candidate, 0o644)
    proc, _log = run(port, tmp, [candidate])
    assert proc.returncode == 127, "rc %r, expected 127; stderr %r" % (proc.returncode, proc.stderr)
    assert proc.stderr == MESSAGE, "stderr %r" % proc.stderr


def test_op_real_naming_the_gateway_itself_is_no_binary(port, tmp):
    proc, _log = run(port, tmp, nowhere(tmp), extra_env={"OP_REAL": port})
    assert proc.returncode == 127, "rc %r, expected 127; stderr %r" % (proc.returncode, proc.stderr)
    assert proc.stderr == MESSAGE, "stderr %r" % proc.stderr


TESTS = [test_candidates_are_the_four_fixed_paths, test_no_binary_exits_127_with_the_message,
         test_no_binary_is_logged_once_0600, test_a_candidate_that_is_not_executable_is_no_binary,
         test_op_real_naming_the_gateway_itself_is_no_binary]


def main(argv):
    p = argparse.ArgumentParser(prog="test-op-gateway-no-binary.py")
    p.add_argument("--source", default=SOURCE)
    p.add_argument("--port", default=None)
    opts = p.parse_args(argv)
    port = opts.port or os.path.join(opts.source, PORT_REL)
    failed = 0
    with tempfile.TemporaryDirectory(prefix="op-no-binary-") as root:
        rendered = os.path.join(root, "op")
        try:
            render(port, opts.source, rendered)
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
