#!/usr/bin/env python3
"hook-client-build: compile hook-client.rs for this host (policy).\n\nhome-materialize runs this at SessionStart, before home-settings-sync, which wires the\ndispatched hook events through the binary only when installed() names a current one.\nA host with no binary keeps the plain `<python> hook-dispatch.py <Event>` command.\n\nThe binary goes to ~/.local/share/agent-context/bin/hook-client, with the SHA-256 of the\nsource it was built from beside it in hook-client.source. A changed source is rebuilt on\nthe next SessionStart; until then installed() answers None, so a stale binary is never\nwired. rustc comes from the platform package manager through chezmoi (memory\nrust-from-package-manager-not-rustup); standard library only, so no cargo and no network.\n\nUsage:\n  hook-client-build.py            build when missing or stale; print the binary's path\n  hook-client-build.py --check    print the path of a current binary, or exit 1\n  hook-client-build.py --quiet    as the first form, printing nothing on success\nExit 0 built or current, 1 not current (--check), 2 the build failed, 3 no rustc.\n\nNot on Windows: the client talks to hook-server.py over a Unix socket, and the server forks a\nchild per call; Windows has neither. There the dispatched events run the plain dispatcher\ncommand, and a plain run exits 0 with nothing built (SUPPORTED)."
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_paths as hp

SOURCE = os.path.join(HERE, "hook-client.rs")
NAME = "hook-client"
BUILD_TIMEOUT = 300


RUSTC_FALLBACKS = ("/opt/homebrew/bin/rustc", "/usr/local/bin/rustc", "/usr/bin/rustc")
SUPPORTED = os.name != "nt"


def binary_path(home=None):
    return os.path.join(hp.home(home), ".local", "share", "agent-context", "bin", NAME)


def source_digest():
    with open(SOURCE, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _stamp(path):
    return path + ".source"


def installed(home=None):
    "The binary's path when it exists and was built from the current source, else None."
    if not SUPPORTED:
        return None
    path = binary_path(home)
    if not os.access(path, os.X_OK):
        return None
    try:
        with open(_stamp(path), encoding="utf-8") as fh:
            built_from = fh.read().strip()
        return path if built_from == source_digest() else None
    except OSError:
        return None


def find_rustc():
    found = shutil.which("rustc")
    if found:
        return found
    for path in RUSTC_FALLBACKS:
        if os.access(path, os.X_OK):
            return path
    return None


def build(home=None):
    '(exit code, message): compile the source into binary_path(home).'
    rustc = find_rustc()
    if not rustc:
        return 3, "no rustc on this host; chezmoi's install-packages provides it (`rust`)"
    path = binary_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    digest = source_digest()
    partial = "%s.%d.tmp" % (path, os.getpid())
    try:
        proc = subprocess.run([rustc, "--edition", "2021", "-O", "-C", "strip=symbols",
                               "-o", partial, SOURCE],
                              capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, "rustc did not finish: %s" % exc
    if proc.returncode != 0 or not os.path.exists(partial):
        try:
            os.unlink(partial)
        except OSError:
            pass
        return 2, "rustc failed:\n%s" % (proc.stderr.strip() or proc.stdout.strip())
    os.replace(partial, path)
    with open(_stamp(path), "w", encoding="utf-8") as fh:
        fh.write(digest + "\n")
    return 0, path


def main(argv):
    args = argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print((__doc__ or "").strip())
        return 0
    if not SUPPORTED and "--check" not in args:
        return 0
    current = installed()
    if "--check" in args:
        if current:
            print(current)
            return 0
        return 1
    if current:
        code, message = 0, current
    else:
        code, message = build()
    if code == 0:
        if "--quiet" not in args:
            print(message)
    else:
        print("hook-client-build: %s" % message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
