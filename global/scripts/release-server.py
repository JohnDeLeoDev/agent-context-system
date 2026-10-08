#!/usr/bin/env python3
"release-server.py -- verify and commit the agent-context server code so the\ndaemon's sync loop propagates it to every machine, where each running daemon\ngate-verifies and self-redeploys onto the new code (see daemon.maybe_self_redeploy).\n\nSafety model:\n  * Never releases unverified code: aborts unless py_compile and pytest pass.\n  * Stages only server/ paths (never `git add -A`) -- no unrelated store churn.\n  * Does not push. The daemon owns all git network ops and serializes them\n    behind its lock; pushing here would race the daemon mid-fetch/rebase/push.\n    The daemon's next sync cycle pushes HEAD to origin/ls/s2.\n  * Idempotent: a clean server/ tree is a no-op.\n\nUsage:  python3 release-server.py          (from any machine with the store present)\n\nThere is no fleet verification run (policy): the gate here is the one check, and each\nrelay self-checks before it switches. `--no-verify` is accepted and ignored, so an\nold habit or an old doc line still releases.\n\nObservations guarded: #121, #185."

import glob
import os
import re
import shutil
import subprocess
import sys
import time

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(
    os.environ.get("HOME", ""), ".agent-context")
SERVER = os.path.join(STORE, "server")


def _git(*args, **kwargs):
    return subprocess.run(["git", "-C", STORE] + list(args), **kwargs)


def _hostname_s(default):
    try:
        proc = subprocess.run(["hostname", "-s"], capture_output=True, text=True)
    except OSError:
        return default
    return proc.stdout.strip() if proc.returncode == 0 else default


def resolve_who(home):
    'Machine id from chezmoi.toml\'s `machine_id = "..."` line under `home`, or the\n    short hostname when the file is missing, unreadable, or has no such line. Always\n    returns something non-empty (falls back to "unknown" if even `hostname -s` fails).\n\n    A missing or unreadable chezmoi.toml (a non-chezmoi machine, or one mid-setup)\n    falls back to the hostname like every other caller of this file.'
    chezmoi_toml = os.path.join(home, ".config", "chezmoi", "chezmoi.toml")
    who = ""
    try:
        with open(chezmoi_toml, encoding="utf-8", errors="replace") as fh:
            pat = re.compile(r"^[ \t]*machine_id[ \t]*=")
            for line in fh:
                if pat.match(line):
                    parts = line.split('"')
                    who = parts[1] if len(parts) > 1 else ""
                    break
    except OSError:
        who = ""
    return who or _hostname_s("unknown")


def _awk_field(path, prefix):
    "Emulate `awk -F'[ =]+' '/^prefix/{print $2; exit}'` on path. Raises OSError\n    if the file cannot be opened, matching awk's own failure there."
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(prefix):
                parts = re.split(r"[ =]+", line.rstrip("\n"))
                return parts[1] if len(parts) > 1 else ""
    return ""


def _srvfp(ref):
    "Fingerprint of server/ EXCLUDING VERSION itself, at ref. 'none' on failure."
    ls = _git("ls-tree", "-r", ref, "--", "server/", cwd=SERVER,
              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if ls.returncode != 0:
        return "none"
    filtered = "".join(line for line in ls.stdout.splitlines(True)
                       if "server/VERSION" not in line)
    h = subprocess.run(["git", "hash-object", "--stdin"], input=filtered, cwd=SERVER,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if h.returncode != 0:
        return "none"
    return h.stdout.strip()


def main(argv):
    
    
    
    
    args = argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    unknown = [a for a in args if a != "--no-verify"]
    if unknown:
        print("release-server: unknown flag %s" % unknown[0], file=sys.stderr)
        return 2
    if args:
        print("release-server: --no-verify does nothing now; there is no fleet "
              "verification run (policy)", file=sys.stderr)
    if not os.path.isdir(os.path.join(STORE, ".git")):
        print("error: no agent-context store at %s" % STORE, file=sys.stderr)
        return 1
    if not os.path.isdir(os.path.join(SERVER, "src", "agent_context")):
        print("error: no server package under %s" % SERVER, file=sys.stderr)
        return 1
    
    
    if (os.path.isdir(os.path.join(STORE, ".git", "rebase-merge"))
            or os.path.isdir(os.path.join(STORE, ".git", "rebase-apply"))):
        print("error: the store has a rebase in progress (.git/rebase-merge) "
              "— wait for it to finish", file=sys.stderr)
        return 1

    
    
    
    
    
    lock = os.path.join(STORE, ".git", ".release-server.lock")
    tries = 0
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            pass
        holder = ""
        try:
            with open(os.path.join(lock, "pid"), encoding="utf-8", errors="replace") as fh:
                holder = fh.read().strip()
        except OSError:
            holder = ""
        dead = not holder
        if not dead:
            try:
                os.kill(int(holder), 0)
            except (OSError, ValueError):
                dead = True
        
        
        
        if dead:
            print("release-server: clearing a lock left behind by dead pid %s"
                  % (holder or "?"), file=sys.stderr)
            shutil.rmtree(lock, ignore_errors=True)
            continue
        tries += 1
        if tries > 150:
            print("error: another release (pid %s) has held the lock for over 5 minutes"
                  % holder, file=sys.stderr)
            return 1
        if tries == 1:
            print("release-server: waiting for the release held by pid %s…" % holder,
                  file=sys.stderr)
        time.sleep(2)

    lock_held = True
    try:
        with open(os.path.join(lock, "pid"), "w", encoding="utf-8") as fh:
            fh.write("%d\n" % os.getpid())

        py = os.path.join(SERVER, ".venv", "bin", "python")
        if not os.access(py, os.X_OK):
            print("error: no venv python at %s — run: (cd '%s' && uv sync)" % (py, SERVER),
                  file=sys.stderr)
            return 1

        os.chdir(SERVER)

        
        print("== pending changes under server/ ==")
        sys.stdout.flush()
        _git("status", "--porcelain", "--", "server/", cwd=SERVER)
        print()
        sys.stdout.flush()
        _git("--no-pager", "diff", "--stat", "--", "server/", cwd=SERVER)
        print()

        
        
        
        
        
        
        
        
        
        
        curfp = _srvfp("HEAD")
        version_file = os.path.join(SERVER, "VERSION")
        recfp = ""
        try:
            recfp = _awk_field(version_file, "tree")
        except OSError:
            recfp = ""

        status_out = _git("status", "--porcelain", "--", "server/", cwd=SERVER,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
        if status_out == "" and curfp == recfp:
            print("server/ is clean and VERSION already names this tree — nothing to release.")
            return 0
        if status_out == "":
            print("== server/ is clean but changed since the last release (landed commit) ==")

        
        print("== gate: py_compile ==")
        sys.stdout.flush()
        py_files = sorted(glob.glob(os.path.join(SERVER, "src", "agent_context", "*.py")))
        py_args = ([os.path.relpath(p, SERVER) for p in py_files] if py_files
                  else ["src/agent_context/*.py"])
        proc = subprocess.run([py, "-m", "py_compile"] + py_args, cwd=SERVER)
        if proc.returncode != 0:
            return proc.returncode
        print("   py_compile OK")
        print()
        print("== gate: pytest ==")
        sys.stdout.flush()
        proc = subprocess.run([py, "-m", "pytest", "-q"], cwd=SERVER)
        if proc.returncode != 0:
            return proc.returncode
        print("   pytest OK")
        print()

        
        
        
        
        
        
        
        
        
        
        
        
        ruff_cmd = None
        if shutil.which("ruff"):
            ruff_cmd = ["ruff"]
        elif shutil.which("uvx"):
            check = subprocess.run(["uvx", "ruff", "--version"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            if check.returncode == 0:
                ruff_cmd = ["uvx", "ruff"]
        if ruff_cmd is None:
            print("== lint: ruff unavailable — skipped ==")
        else:
            print("== gate: ruff ==")
            sys.stdout.flush()
            lrc = subprocess.run(ruff_cmd + ["check", "src/", "tests/"], cwd=SERVER).returncode
            if lrc != 0:
                print(file=sys.stderr)
                print("error: lint failures — not released. Fix them (many are auto-fixable:",
                      file=sys.stderr)
                print("  cd '%s' && %s check --fix src/ tests/)" % (SERVER, " ".join(ruff_cmd)),
                      file=sys.stderr)
                return 1
            print("   ruff OK")
        print()

        
        

        
        
        prev = 0
        if os.path.isfile(version_file):
            val = _awk_field(version_file, "build")
            prev = int(val) if val.isdigit() else 0
        
        
        
        
        
        
        
        
        
        hist_proc = _git("log", "--format=%s", "--grep=^release: server build ", cwd=SERVER,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        hist = 0
        if hist_proc.returncode == 0:
            pat = re.compile(r"^release: server build ([0-9]+)")
            nums = [int(m.group(1)) for m in
                   (pat.match(line) for line in hist_proc.stdout.splitlines()) if m]
            if nums:
                hist = max(nums)
        if hist > prev:
            prev = hist
        next_build = prev + 1
        stamp = time.strftime("%Y%m%d-%H%M%S")
        sha = _git("rev-parse", "--short", "HEAD", cwd=SERVER, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, text=True).stdout.strip()
        
        
        
        
        who = resolve_who(os.environ.get("HOME", ""))
        
        
        
        with open(version_file, "w", encoding="utf-8") as fh:
            fh.write("build=%s\nby=%s\ndate=%s\nsha=%s\ntree=%s\n"
                     % (next_build, who, stamp, sha, curfp))
        print("== VERSION -> build=%s by=%s date=%s sha=%s tree=%s ==" %
             (next_build, who, stamp, sha, curfp[:12]))
        print()

        
        _git("add", "--", "server/", cwd=SERVER)
        diff_check = _git("diff", "--cached", "--quiet", "--", "server/", cwd=SERVER)
        if diff_check.returncode == 0:
            print("nothing staged under server/ after add — already released.")
            return 0
        
        
        commit_env = dict(os.environ)
        commit_env["AGENT_CONTEXT_GATE_PASSED"] = "1"
        sys.stdout.flush()
        commit_proc = subprocess.run(
            ["git", "-C", STORE, "commit", "-m",
             "release: server build %s (%s, %s)" % (next_build, stamp, who), "--", "server/"],
            cwd=SERVER, env=commit_env)
        if commit_proc.returncode != 0:
            return commit_proc.returncode
        print()
        head = _git("rev-parse", "--short", "HEAD", cwd=SERVER, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True).stdout.strip()
        print("committed server build %s (HEAD now %s)." % (next_build, head))
        print("NOT pushing: the agent-context daemon's sync loop will push HEAD to origin/ls/s2")
        print("on its next cycle (manual push races the daemon's serialized git ops).")
        print("Remote daemons then fetch, gate-verify, and self-redeploy onto build %s."
             % next_build)
        return 0
    finally:
        if lock_held:
            shutil.rmtree(lock, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
