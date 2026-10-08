#!/usr/bin/env python3
'node-tools-sync: install the store\'s Node tools at their latest releases.\n\nThe store names the Node tools its sessions run in global/node-tools/: package.json\nlists each at `latest` (no pins, always the newest release), and pnpm-workspace.yaml carries pnpm\'s settings and\nthe copilot-api patch (memory copilot-api-is-a-patched-build). This script resolves and\ninstalls them with pnpm, points ~/.local/share/agent-context/node-tools at the result, and\nlinks the bins named in package.json "agentContextBins" into ~/.local/bin.\n\nIt fetches, so it runs on demand, and on a machine\'s first chezmoi apply through\ninstall-packages. Nothing runs it at session start. --check compares the\ninstalled copy with the store\'s manifest without running pnpm, so it never fetches.\n\nEach sync installs into a new generation directory beside the root (.node-tools-<stamp>)\nand swaps the root symlink only after pnpm succeeded and every declared bin exists. pnpm\nwrites absolute paths into its bin shims, so an installed tree cannot be moved; the symlink\nswap is what makes the change atomic. A failure removes the new generation and leaves the\nprevious root, the bin links and the parent directory as they were. When the new resolution\nmatches the installed one, the new generation is discarded and nothing changes. The\nprevious generation is kept for one cycle, so a server that is already running keeps its\nfiles.\n\nUsage:\n  node-tools-sync.py [--source DIR] [--root DIR] [--bin-dir DIR] [--check]\n  node-tools-sync.py -h | --help\n\nExit status: 0 installed or already current; 3 --check found drift; 127 pnpm is not on\nPATH; 1 any other failure, with the cause on stderr; 2 bad arguments.'

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse

try:
    import fcntl
except ImportError:  
    fcntl = None

HOME = os.path.expanduser("~")
STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(HOME, ".agent-context")
DEFAULT_SOURCE = os.path.join(STORE, "global", "node-tools")
DEFAULT_ROOT = os.path.join(HOME, ".local", "share", "agent-context", "node-tools")
DEFAULT_BIN = os.path.join(HOME, ".local", "bin")
STATE = ".agent-context-node-tools.json"
GEN_PREFIX = ".node-tools-"
MANIFEST = ("package.json", "pnpm-workspace.yaml")
DEP_FIELDS = ("dependencies", "devDependencies", "optionalDependencies")
INSTALL_TIMEOUT = 900
PNPM_HINT = ("install it through the platform package manager: `brew install pnpm` on macOS, "
             "`winget install pnpm.pnpm` on Windows, and elsewhere install pnpm@latest globally "
             "with the npm that apt or Synology Package Center provides")


class Failure(Exception):
    'A refusal or failure, reported on stderr with exit status 1.'


def say(msg):
    print("node-tools-sync: " + msg, flush=True)


def source_files(source):
    '(relative path, absolute path) for every file the install depends on, sorted.'
    out = []
    for name in MANIFEST:
        path = os.path.join(source, name)
        if not os.path.isfile(path):
            raise Failure("%s is missing" % path)
        out.append((name, path))
    patches = os.path.join(source, "patches")
    if os.path.isdir(patches):
        for dirpath, dirnames, files in os.walk(patches):
            dirnames.sort()
            for f in sorted(files):
                path = os.path.join(dirpath, f)
                out.append((os.path.relpath(path, source), path))
    return sorted(out)


def digest(files):
    h = hashlib.sha256()
    for rel, path in files:
        h.update(rel.encode("utf-8") + b"\0")
        with open(path, "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()


def load_manifest(source):
    '(bins to link, every dependency name) from the source package.json.'
    path = os.path.join(source, "package.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as ex:
        raise Failure("cannot read %s: %s" % (path, ex))
    if not isinstance(data, dict):
        raise Failure("%s is not a JSON object" % path)
    bins = data.get("agentContextBins", [])
    if not isinstance(bins, list) or not all(
            isinstance(b, str) and b and "/" not in b and os.sep not in b and b not in (".", "..")
            for b in bins):
        raise Failure("%s: agentContextBins must be a list of bin names" % path)
    deps = set()
    for field in DEP_FIELDS:
        section = data.get(field)
        if isinstance(section, dict):
            deps.update(section)
    return bins, sorted(deps)


def read_state(root):
    try:
        with open(os.path.join(root, STATE), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def link_target(root, name):
    return os.path.join(root, "node_modules", ".bin", name)


def owned(link, root, name):
    'True when `link` is the symlink this script makes for bin `name` under `root`.'
    return os.path.islink(link) and os.readlink(link) == link_target(root, name)


def refusal(link, root, name):
    'Why `link` will not be replaced, saying what it really is.'
    if os.path.islink(link):
        return ("refusing to replace %s: it links to %s, not to %s under this root"
                % (link, os.readlink(link), link_target(root, name)))
    return "refusing to replace %s: it is not a symlink node-tools-sync made" % link


def drift(source, root, bin_dir):
    files = source_files(source)
    bins, _ = load_manifest(source)
    if not os.path.isdir(os.path.join(root, "node_modules")):
        return ["nothing is installed at %s" % root]
    found = []
    if read_state(root).get("digest") != digest(files):
        found.append("the manifest in %s changed since the last sync" % source)
    for name in bins:
        if not os.path.lexists(link_target(root, name)):
            found.append("bin %s is missing from %s" % (name, root))
        link = os.path.join(bin_dir, name)
        if not owned(link, root, name):
            found.append("%s is not linked to %s" % (link, link_target(root, name)))
    return found


def failed_packages(output, deps):
    low = output.lower()
    return [d for d in deps if d.lower() in low or urllib.parse.quote(d, safe="@").lower() in low]


def install(pnpm, files, gen):
    os.makedirs(gen)
    for rel, path in files:
        dst = os.path.join(gen, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst)
    say("resolving the latest releases into %s" % gen)
    try:
        r = subprocess.run([pnpm, "install", "--dir", gen], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=dict(os.environ, CI="1"), timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Failure("pnpm install did not finish within %ds" % INSTALL_TIMEOUT)
    except OSError as ex:
        raise Failure("cannot run %s: %s" % (pnpm, ex))
    return r.returncode, r.stdout.decode("utf-8", "replace")


def swap(root, gen):
    'Point `root` at `gen` atomically. Returns the generation root pointed at before, or None.'
    parent = os.path.dirname(root)
    previous = None
    if os.path.islink(root):
        target = os.readlink(root)
        previous = target if os.path.isabs(target) else os.path.join(parent, target)
    elif os.path.lexists(root):
        
        previous = os.path.join(parent, "%sadopted-%d" % (GEN_PREFIX, os.getpid()))
        os.rename(root, previous)
    tmp_link = os.path.join(parent, "%slink-%d" % (GEN_PREFIX, os.getpid()))
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(os.path.basename(gen), tmp_link)
    os.replace(tmp_link, root)
    return previous


def prune(parent, keep):
    keep = {os.path.abspath(k) for k in keep if k}
    for name in os.listdir(parent):
        path = os.path.join(parent, name)
        if (name.startswith(GEN_PREFIX) and os.path.isdir(path) and not os.path.islink(path)
                and os.path.abspath(path) not in keep):
            shutil.rmtree(path, ignore_errors=True)


def link_bins(bins, old_bins, root, bin_dir):
    os.makedirs(bin_dir, exist_ok=True)
    for name in bins:
        link = os.path.join(bin_dir, name)
        if owned(link, root, name):
            continue
        if os.path.lexists(link):
            raise Failure(refusal(link, root, name))
        tmp_link = "%s.node-tools-sync-%d" % (link, os.getpid())
        if os.path.lexists(tmp_link):
            os.remove(tmp_link)
        os.symlink(link_target(root, name), tmp_link)
        os.replace(tmp_link, link)
    for name in old_bins:
        link = os.path.join(bin_dir, name)
        if name not in bins and owned(link, root, name):
            os.remove(link)


def sync(source, root, bin_dir):
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        print("node-tools-sync: pnpm is not on PATH; " + PNPM_HINT, file=sys.stderr, flush=True)
        return 127
    files = source_files(source)
    bins, deps = load_manifest(source)
    foreign = [n for n in bins
               if os.path.lexists(os.path.join(bin_dir, n)) and not owned(os.path.join(bin_dir, n), root, n)]
    if foreign:
        raise Failure("; ".join(refusal(os.path.join(bin_dir, n), root, n) for n in foreign))

    parent = os.path.dirname(root)
    made_parent = not os.path.isdir(parent)
    os.makedirs(parent, exist_ok=True)
    lock_fd = os.open(parent, os.O_RDONLY)
    gen = os.path.join(parent, "%s%s-%d" % (GEN_PREFIX, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), os.getpid()))
    swapped = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Failure("another node-tools-sync is running on %s" % parent)
        old_state = read_state(root)
        rc, out = install(pnpm, files, gen)
        if rc != 0:
            cause = "pnpm install exited %d" % rc
            names = failed_packages(out, deps)
            if names:
                cause += " for %s" % ", ".join(names)
            low = out.lower()
            if "patch" in low and "apply" in low:
                cause += "; a patch in %s no longer applies to the latest release" % os.path.join(source, "patches")
            raise Failure(cause + "\n" + "\n".join(out.rstrip().splitlines()[-40:]))
        missing = [n for n in bins if not os.path.lexists(link_target(gen, n))]
        if missing:
            raise Failure("the install has no bin named %s" % ", ".join(missing))

        dg = digest(files)
        current = (os.path.isdir(os.path.join(root, "node_modules")) and old_state.get("digest") == dg
                   and read_bytes(os.path.join(root, "pnpm-lock.yaml")) == read_bytes(os.path.join(gen, "pnpm-lock.yaml")))
        if current:
            say("already current at %s" % root)
        else:
            with open(os.path.join(gen, STATE), "w", encoding="utf-8") as fh:
                json.dump({"digest": dg, "bins": bins,
                           "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, fh, indent=2)
                fh.write("\n")
            previous = swap(root, gen)
            swapped = True
            prune(parent, keep=(gen, previous))
            say("installed the latest %s at %s" % (", ".join(deps), root))
        link_bins(bins, old_state.get("bins") or [], root, bin_dir)
        return 0
    finally:
        if not swapped:
            shutil.rmtree(gen, ignore_errors=True)
        os.close(lock_fd)
        if made_parent and not swapped:
            try:
                os.rmdir(parent)
            except OSError:
                pass


def main(argv):
    ap = argparse.ArgumentParser(
        prog="node-tools-sync.py",
        description="Install the store's Node tools at their latest releases and link their bins.",
        epilog="Exit: 0 installed or current; 3 --check found drift; 127 no pnpm; 1 failure; 2 usage.")
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="the tools manifest (default: %(default)s)")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="where the tools live (default: %(default)s)")
    ap.add_argument("--bin-dir", default=DEFAULT_BIN, help="where bins are linked (default: %(default)s)")
    ap.add_argument("--check", action="store_true",
                    help="report drift from the manifest without running pnpm; exit 3 on drift")
    args = ap.parse_args(argv)
    source, root, bin_dir = (os.path.abspath(os.path.expanduser(p)) for p in (args.source, args.root, args.bin_dir))
    try:
        if args.check:
            found = drift(source, root, bin_dir)
            for line in found:
                say("drift: " + line)
            if not found:
                say("current at %s" % root)
            return 3 if found else 0
        return sync(source, root, bin_dir)
    except Failure as ex:
        print("node-tools-sync: %s" % ex, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
