#!/usr/bin/env python3
"Keep a NON-EXPIRED intellij-server installed for the Kotlin LSP.\n\nHomebrew is not the fix. `jetbrains/utils/kotlin-lsp` installs one symlink,\nbin/kotlin-lsp -> libexec/bin/intellij-server, and its formula was still pinned\nto the expired 262.9593.0 four days after that build died; the Kotlin/kotlin-lsp\nGitHub releases page and JetBrains' data-services feed were no better. The ONLY\nchannel observed shipping a live build is the VS Code marketplace extension\nJetBrains.intellij-server, whose payload under extension/server/ is the same\ndistribution the .sit archive carries. So that is what this pulls.\n\nInstalls land at ~/.local/opt/intellij-server/<build>, with `current` symlinked\nat the newest; `lspd.py --mcp` runs `current/bin/intellij-server`, so flipping that\nsymlink is the whole cutover. Homebrew is left alone as a fallback.\n\nSafe to run repeatedly and from launchd: it exits early when the marketplace\nversion is unchanged (before the ~370 MB download), stages every install under a\ntemp path so an interrupted run cannot leave a half-tree behind `current`, and\nverifies `--version` on the new binary BEFORE the symlink moves.\n\nExit codes: 0 nothing to do or updated cleanly | 3 new build held for EULA\nreview | 4 the update failed and the old install is still in place."

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

HOME = os.path.expanduser("~")
DEST = os.path.join(HOME, ".local", "opt", "intellij-server")
ACCEPTED = os.path.join(DEST, "accepted-eulas.txt")
STATUS = os.path.join(DEST, ".refresh-status.json")
HELD = os.path.join(DEST, "EULA-CHANGED-READ-ME.txt")
KEEP_OLD = 1  

PUBLISHER, EXTENSION = "JetBrains", "intellij-server"
GALLERY = "https://marketplace.visualstudio.com/_apis/public/gallery"


NET_TIMEOUT = 120
DEADMAN = 45 * 60





LSPD = os.path.join(HOME, ".agent-context", "global", "scripts", "lspd.py")
CANARY = os.path.join(HOME, ".agent-context", "global", "scripts", "lsp-canary.py")
WORKSPACE = os.environ.get("AGENT_CONTEXT_WORKSPACE") or os.getcwd()


def log(msg):
    print("%s refresh-intellij-server: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


def write_status(state, **extra):
    rec = {"ts": int(time.time()), "state": state}
    rec.update(extra)
    os.makedirs(DEST, exist_ok=True)
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh, indent=2)
    os.replace(tmp, STATUS)


PROGRESS_FILE = os.path.join(HOME, ".local", "state", "agent-context",
                              "refresh-intellij-server-progress.json")


def write_progress(step, started_at, finished=False):
    'State for the one step (the canary) that can run up to 15 minutes silent.\n    A failed write must never break the script.'
    try:
        doc = {"pid": os.getpid(), "started_at": started_at, "step": step,
               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "finished": finished}
        os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
        tmp = PROGRESS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, PROGRESS_FILE)
    except OSError:
        pass


def target_platform():
    'VSIX platform tag. The payload is a native launcher plus a bundled JBR,\n    so the wrong tag installs a tree that cannot exec -- never guess universal.'
    mach = "arm64" if platform.machine() in ("arm64", "aarch64") else "x64"
    if sys.platform == "darwin":
        return "darwin-%s" % mach
    if sys.platform.startswith("linux"):
        return "linux-%s" % mach
    raise SystemExit("unsupported platform %s/%s" % (sys.platform, platform.machine()))


def latest_version(tp):
    'Newest marketplace version for this platform, or None.'
    body = json.dumps({"filters": [{"criteria": [
        {"filterType": 7, "value": "%s.%s" % (PUBLISHER, EXTENSION)}],
        "pageSize": 1, "pageNumber": 1}], "flags": 947}).encode()
    req = urllib.request.Request(
        GALLERY + "/extensionquery", data=body,
        headers={"Accept": "application/json;api-version=7.2-preview.1",
                 "Content-Type": "application/json",
                 "User-Agent": "refresh-intellij-server"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
        data = json.load(resp)
    exts = data.get("results", [{}])[0].get("extensions") or []
    if not exts:
        return None
    for v in exts[0].get("versions", []):
        
        if v.get("targetPlatform") == tp:
            return v.get("version")
    return None


def installed():
    '(extension version, build) currently installed, from the status file and\n    build.txt. build.txt is the authority: the status file can be stale or absent\n    on a tree someone installed by hand.'
    cur = os.path.join(DEST, "current")
    build = None
    try:
        with open(os.path.join(cur, "build.txt")) as fh:
            build = fh.read().strip()
    except OSError:
        pass
    ver = None
    try:
        with open(STATUS) as fh:
            rec = json.load(fh)
        if rec.get("build") == build:  
            ver = rec.get("ext_version")
    except (OSError, ValueError):
        pass
    return ver, build


def accepted_hashes():
    try:
        with open(ACCEPTED) as fh:
            return {ln.split("#")[0].strip() for ln in fh if ln.split("#")[0].strip()}
    except OSError:
        return set()


def download(tp, version, path):
    url = "%s/publishers/%s/vsextensions/%s/%s/vspackage?targetPlatform=%s" % (
        GALLERY, PUBLISHER, EXTENSION, version, tp)
    req = urllib.request.Request(url, headers={
        "User-Agent": "refresh-intellij-server",
        
        
        "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp, open(path, "wb") as fh:
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            shutil.copyfileobj(gzip.GzipFile(fileobj=resp), fh)
        else:
            shutil.copyfileobj(resp, fh)
    return os.path.getsize(path)


def extract(vsix, staging):
    'Unpack extension/server/* and restore the exec bits zipfile drops.'
    prefix = "extension/server/"
    with zipfile.ZipFile(vsix) as z:
        members = [m for m in z.infolist() if m.filename.startswith(prefix)
                   and not m.filename.endswith("/")]
        if not members:
            raise RuntimeError("VSIX has no %s payload" % prefix)
        for m in members:
            rel = m.filename[len(prefix):]
            out = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(m) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = (m.external_attr >> 16) & 0o7777
            if mode & 0o111:
                os.chmod(out, mode)
    
    
    for rel in ("bin/intellij-server",):
        p = os.path.join(staging, rel)
        if os.path.exists(p):
            os.chmod(p, 0o755)
    for root, _dirs, files in os.walk(os.path.join(staging, "jbr")):
        for f in files:
            p = os.path.join(root, f)
            if root.endswith("/bin") or os.access(p, os.X_OK):
                os.chmod(p, 0o755)


def restart_lsp():
    'Drop the daemon so the next attach spawns the new binary, then warm it.'
    subprocess.run(["pkill", "-f", "lspd.py .*--key kotlin-lsp"],
                   check=False, capture_output=True)
    if os.path.exists(CANARY) and os.path.isdir(WORKSPACE):
        
        
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        t0 = time.time()
        print("refresh-intellij-server: canary starting, timeout 900s",
              file=sys.stderr, flush=True)
        write_progress("canary", started_at)
        result = subprocess.run([sys.executable, CANARY, WORKSPACE],
                                check=False, capture_output=True, timeout=900)
        write_progress("canary", started_at, finished=True)
        print("refresh-intellij-server: canary finished rc=%d in %ds"
              % (result.returncode, time.time() - t0), file=sys.stderr, flush=True)


def prune(keep):
    'Drop old build trees, newest-first, keeping `keep` beyond current.'
    try:
        entries = [d for d in os.listdir(DEST)
                   if os.path.isdir(os.path.join(DEST, d)) and d[0].isdigit()]
    except OSError:
        return
    cur = os.path.realpath(os.path.join(DEST, "current"))
    others = [d for d in entries if os.path.join(DEST, d) != cur]
    others.sort(key=lambda d: os.path.getmtime(os.path.join(DEST, d)), reverse=True)
    for d in others[keep:]:
        log("pruning old build %s" % d)
        shutil.rmtree(os.path.join(DEST, d), ignore_errors=True)


def main():
    
    
    
    
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    if args:
        print("refresh-intellij-server: unknown flag(s): %s" % " ".join(args), file=sys.stderr)
        print("refresh-intellij-server: a bare run DOWNLOADS and may re-point `current`. "
              "Refusing rather than guessing.", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2

    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(
        SystemExit("deadman: exceeded %ds" % DEADMAN)))
    signal.alarm(DEADMAN)

    os.makedirs(DEST, exist_ok=True)
    tp = target_platform()
    have_ver, have_build = installed()

    version = latest_version(tp)
    if not version:
        log("marketplace returned no %s build for %s -- leaving %s in place"
            % (EXTENSION, tp, have_build or "nothing"))
        return 0
    if version == have_ver:
        log("up to date (extension %s, build %s)" % (version, have_build))
        write_status("current", ext_version=version, build=have_build)
        return 0

    log("marketplace has %s (installed: %s, build %s)"
        % (version, have_ver or "none", have_build or "none"))

    tmpdir = tempfile.mkdtemp(prefix="intellij-server-", dir=DEST)
    try:
        vsix = os.path.join(tmpdir, "ext.vsix")
        size = download(tp, version, vsix)
        log("downloaded %.0f MB" % (size / 1e6))

        with zipfile.ZipFile(vsix) as z:
            build = z.read("extension/server/build.txt").decode().strip()
            eula = z.read("extension/server/EULA.txt")
        eula_hash = hashlib.sha256(eula).hexdigest()[:16]
        log("build %s, EULA %s" % (build, eula_hash))

        if eula_hash not in accepted_hashes():
            
            
            
            with open(HELD, "w") as fh:
                fh.write(
                    "intellij-server %s (extension %s) was downloaded and then NOT "
                    "installed.\n\nIts bundled EULA hashes to %s, which is not in\n%s "
                    "-- so user has not accepted this text and this script will not\n"
                    "accept it for him.\n\nThe running Kotlin LSP is unaffected until "
                    "its own 30-day EAP term expires.\n\nTo review and accept:\n"
                    "  1. read the EULA:  <build tree>/EULA.txt  (or `--show-eula`)\n"
                    "  2. append the hash to %s\n"
                    "  3. re-run this script\n"
                    % (build, version, eula_hash, ACCEPTED, ACCEPTED))
            log("HELD: EULA %s not accepted -- see %s" % (eula_hash, HELD))
            write_status("held-eula", ext_version=version, build=build,
                         eula_hash=eula_hash, note=HELD)
            return 3

        number = build.split("-")[-1]  
        final = os.path.join(DEST, number)
        staging = os.path.join(tmpdir, "server")

        
        
        
        
        
        if build == have_build and os.access(
                os.path.join(DEST, "current", "bin", "intellij-server"), os.X_OK):
            log("build %s already installed and runnable -- recording version only"
                % build)
            write_status("current", ext_version=version, build=build,
                         eula_hash=eula_hash)
            return 0
        extract(vsix, staging)
        os.remove(vsix)  

        probe = subprocess.run([os.path.join(staging, "bin", "intellij-server"),
                                "--version"], capture_output=True, text=True, timeout=300)
        if probe.returncode != 0 or number not in probe.stdout:
            raise RuntimeError("new binary failed --version: rc=%d %r"
                               % (probe.returncode, (probe.stdout + probe.stderr)[:400]))
        log("verified %s" % probe.stdout.strip())

        if os.path.exists(final):
            shutil.rmtree(final, ignore_errors=True)
        os.rename(staging, final)
        
        tmplink = os.path.join(DEST, ".current.new")
        if os.path.islink(tmplink):
            os.unlink(tmplink)
        os.symlink(final, tmplink)
        os.rename(tmplink, os.path.join(DEST, "current"))
        log("current -> %s" % number)

        if os.path.exists(HELD):
            os.remove(HELD)
        write_status("updated", ext_version=version, build=build, eula_hash=eula_hash)
        restart_lsp()
        prune(KEEP_OLD)
        log("done")
        return 0
    except Exception as exc:
        log("FAILED: %r -- leaving build %s in place" % (exc, have_build or "none"))
        write_status("failed", ext_version=version, error=repr(exc),
                     build=have_build)
        return 4
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
