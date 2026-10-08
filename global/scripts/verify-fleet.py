#!/usr/bin/env python3
'verify-fleet.py -- run the server suite on other machines in the fleet, against the\ncandidate tree, before it becomes a release. --help prints the full rationale.'

import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile

MAX_PARALLEL = 4
DASH = "—"

HELP_LINES = [
    'verify-fleet.py — run the server suite on other machines in the fleet, against the',
    'candidate tree, before it becomes a release.',
    '',
    'Why. This repo self-deploys: every daemon gate-verifies new code and re-execs onto',
    'it, and the gate fails closed, so one test that is red on one platform pins every',
    'machine on the last-good build, silently, while each host still reports a healthy',
    'daemon. A test that asserts on git\'s error wording ("Unable to create',
    "'…main.lock'\", which git 2.54 emits and 2.53 does not) passes on the machine it",
    'is written on, fails on another, and pins the fleet.',
    '',
    'A suite that is green on one machine is not evidence. The fleet spans macOS',
    '(git 2.54), Debian bookworm (2.39) and Ubuntu (2.53), and the suite shells out to',
    'git constantly — so the release must be proven on more than the machine cutting it.',
    'Since policy a release is verified once, by the landing gate on ls plus a self-check',
    'on each relay. Nothing calls this script: release-server.py no longer runs it. It',
    'remains a manual check, run by hand against a candidate tree.',
    '',
    'How. Stage a full checkout of the store HEAD (depth 1, git metadata included) with',
    "the working tree's server/ laid over it, copy that to a temp dir on each verifier,",
    "sync a venv there from the candidate's uv.lock under that host's own python",
    '(~/.cache/agent-context-verify/venv), and run the suite with that host\'s git. The',
    "verifier's real store is never touched: nothing is committed, checked out, or moved",
    'there, and the temp dir is removed afterwards. A verifier needs ~/.local/bin/uv and',
    'a python matching requires-python, not a store checkout. Verifiers run in parallel; the',
    "fleet's sshd throttles around ten concurrent connections, so the cap stays well",
    'under that.',
    '',
    'The copy is `tar | ssh | tar`, not rsync, because rsync is not dependable on the',
    'Entware nodes and those are the ones that matter most here: they run the oldest',
    'git in the fleet (see the host list). tar and ssh exist everywhere.',
    '',
    'Usage:  python3 verify-fleet.py [--hosts "a b c"] [--quiet]',
]


def host_id(host):
    return "server-host" if host == "ls" else host


def say(quiet, text):
    if not quiet:
        print(text)


def detect_me(home):
    chezmoi_path = os.path.join(home, ".config", "chezmoi", "chezmoi.toml")
    me = ""
    try:
        with open(chezmoi_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if re.match(r"^[ \t]*machine_id[ \t]*=", line):
                    parts = line.split('"')
                    me = parts[1] if len(parts) > 1 else ""
                    break
    except OSError:
        pass
    if me:
        return me
    try:
        proc = subprocess.run(["hostname", "-s"], capture_output=True, text=True,
                              check=False)
    except OSError:
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.rstrip("\n")


def ssh_ok(fq, cmd):
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", fq, cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return False
    return proc.returncode == 0


def ssh_quiet(fq, cmd):
    try:
        subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", fq, cmd],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        pass


def tar_pipe_to_ssh(fq, tar_argv, tar_env, remote_cmd):
    "Runs `tar ... | ssh fq remote_cmd`, returns the pipeline exit status (ssh's, as\n    bash reports it without pipefail)."
    try:
        tar_proc = subprocess.Popen(tar_argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=tar_env)
    except OSError:
        return 1
    assert tar_proc.stdout is not None
    try:
        ssh_proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", fq, remote_cmd],
            stdin=tar_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        tar_proc.stdout.close()
        tar_proc.wait()
        return 1
    tar_proc.stdout.close()
    ssh_rc = ssh_proc.wait()
    tar_proc.wait()
    return ssh_rc


def git_out(repo, *args):
    "stdout of a git command in repo. The user's global and system config stay out:\n    a global core.hooksPath would run the store's own hooks on the staging checkout."
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", repo, *args], env=env, check=True,
                          capture_output=True, text=True).stdout


def stage_candidate(store, dest):
    "Build at dest a full checkout of the store's HEAD with the candidate's\n    working-tree server/ laid over it.\n\n    The suite reads the whole store, not only server/: tests resolve the store root\n    from their own path and inspect .gitignore, `git ls-files`, global/scripts,\n    projects/ and server/uv.lock. A copy of selected files kept failing whichever\n    test needed the next file, so the verifier gets the real\n    tree, git metadata included. The fetch is depth 1: history is not under test.\n\n    The candidate may be a server/ with uncommitted edits, so\n    the working tree's server/ files (tracked and untracked, not ignored) replace the\n    committed ones, and a tracked file deleted in the working tree is deleted here."
    head = git_out(store, "rev-parse", "HEAD").strip()
    git_out(store, "init", "-q", dest)
    git_out(dest, "fetch", "-q", "--depth", "1", "file://" + os.path.abspath(store), head)
    git_out(dest, "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")

    def listed(repo, *flags):
        out = git_out(repo, "ls-files", "-z", *flags, "--", "server")
        return {p for p in out.split("\0") if p}

    for rel in listed(dest) | listed(store, "-c", "-o", "--exclude-standard"):
        src = os.path.join(store, rel)
        dst = os.path.join(dest, rel)
        if os.path.lexists(dst):
            os.unlink(dst)
        if os.path.lexists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=False)








VERIFY_DIR = "$HOME/.cache/agent-context-verify"
VERIFY_VENV = VERIFY_DIR + "/venv"
REMOTE_ENV = ('export PATH="$PATH:$HOME/.local/bin:/opt/homebrew/bin" '
              f'UV_PROJECT_ENVIRONMENT="{VERIFY_VENV}" UV_PYTHON_DOWNLOADS=never; ')


def run_one(host, domain, staged):
    fq = host if "." in host else f"{host}.{domain}"
    
    
    
    
    
    remote = f"{VERIFY_DIR}/run-{os.getpid()}"

    if not ssh_ok(fq, "true"):
        return {"rc": "UNREACHABLE", "out": ""}
    if not ssh_ok(fq, "[ -x $HOME/.local/bin/uv ]"):
        return {"rc": "NOUV", "out": ""}

    ssh_quiet(fq, f"rm -rf {remote} && mkdir -p {remote}/store {remote}/tmp")

    env = dict(os.environ)
    env["COPYFILE_DISABLE"] = "1"
    
    
    rc = tar_pipe_to_ssh(fq, ["tar", "--no-xattrs", "-czf", "-", "-C", staged, "."],
                         env, f"tar -xzf - -C {remote}/store")
    if rc != 0:
        ssh_quiet(fq, f"rm -rf {remote}")
        return {"rc": "COPYFAIL", "out": ""}

    remote_cmd = (
        REMOTE_ENV + f"export TMPDIR={remote}/tmp && "
        f"cd {remote}/store/server && uv sync -q --frozen --group dev 2>&1 && "
        "\"$UV_PROJECT_ENVIRONMENT/bin/python\" -m pytest "
        "-q -rfE --tb=line -p no:cacheprovider tests 2>&1; echo RC=$?; "
        f"git --version; rm -rf {remote}")
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", fq, remote_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        out = proc.stdout or ""
    except OSError:
        out = ""

    found = None
    for line in out.splitlines():
        m = re.match(r"^RC=([0-9]*)$", line)
        if m:
            found = m.group(1)
    return {"rc": found or "99", "out": out}


def last_match(pattern, text):
    
    
    matches = []
    for line in text.splitlines():
        matches.extend(pattern.findall(line))
    return matches[-1] if matches else ""


def summary_lines(out):
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "short test summary" in line:
            return lines[i:i + 20]
    return []


def parse_args(argv):
    
    
    hosts = os.environ.get("AGENT_CONTEXT_VERIFY_HOSTS", "")
    quiet = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--hosts":
            
            
            
            if i + 1 >= len(argv):
                print("verify-fleet: --hosts requires a value", file=sys.stderr)
                sys.exit(64)
            hosts = argv[i + 1]
            i += 2
        elif arg == "--quiet":
            quiet = True
            i += 1
        elif arg in ("-h", "--help"):
            sys.stdout.write("\n".join(HELP_LINES) + "\n")
            sys.exit(0)
        else:
            
            
            
            print(f"verify-fleet: unknown flag {arg}", file=sys.stderr)
            sys.exit(2)
    return hosts, quiet


def main(argv):
    hosts, quiet = parse_args(argv)
    domain = os.environ.get("AGENT_CONTEXT_FLEET_DOMAIN", "example.invalid")
    home = os.environ.get("HOME", "")
    store = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(home, ".agent-context")
    server = os.path.join(store, "server")

    if not os.path.isdir(os.path.join(server, "tests")):
        print(f"verify-fleet: no server/tests at {server}", file=sys.stderr)
        return 64

    
    
    me = detect_me(home)

    say(quiet, f"== verifying the candidate tree on the fleet (this machine: {me}) ==")

    started = []
    results = {}
    with tempfile.TemporaryDirectory(prefix="verify-fleet-") as staged:
        try:
            stage_candidate(store, staged)
        except subprocess.CalledProcessError as exc:
            cmd = " ".join(exc.cmd)
            print(f"verify-fleet: could not stage the candidate tree: {cmd}\n{exc.stderr or ''}",
                  file=sys.stderr)
            return 64
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            futures = {}
            for h in hosts.split():
                if h == me or host_id(h) == me:
                    say(quiet, f"   {h}: skipped (this is the machine cutting the release)")
                    continue
                started.append(h)
                futures[h] = pool.submit(run_one, h, domain, staged)
            for h in started:
                results[h] = futures[h].result()

    ran = 0
    failed = 0
    passed_re = re.compile(r"[0-9]+ passed[^)]*")
    gitver_re = re.compile(r"git version [0-9.]+")
    for h in started:
        rc = results[h]["rc"]
        out = results[h]["out"]
        if rc == "0":
            ran += 1
            say(quiet, f"   {h}: PASS  ({last_match(passed_re, out)}, "
                       f"{last_match(gitver_re, out)})")
        elif rc == "UNREACHABLE":
            say(quiet, f"   {h}: unreachable {DASH} skipped")
        elif rc == "NOUV":
            say(quiet, f"   {h}: no ~/.local/bin/uv {DASH} skipped")
        elif rc == "COPYFAIL":
            say(quiet, f"   {h}: could not copy the candidate tree {DASH} skipped")
        else:
            ran += 1
            failed += 1
            print(f"   {h}: FAILED (rc={rc})", file=sys.stderr)
            
            
            
            
            for line in summary_lines(out) or out.splitlines()[-20:]:
                print("      " + line, file=sys.stderr)
            if not out:
                print("      (no output captured)", file=sys.stderr)

    if failed > 0:
        print(f"verify-fleet: {failed} of {ran} verifier(s) FAILED {DASH} not releasable.",
              file=sys.stderr)
        print("  The fleet's deploy gate fails closed, so releasing this would pin every",
              file=sys.stderr)
        print("  machine on the last-good build until someone noticed.", file=sys.stderr)
        return 1
    if ran == 0:
        print(f"verify-fleet: no verifier could be reached {DASH} the candidate is proven on",
              file=sys.stderr)
        print(f"  {me} only, which is not enough evidence for a release.",
              file=sys.stderr)
        return 2
    say(quiet, f"== verified on {ran} other machine(s) ==")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
