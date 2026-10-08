#!/usr/bin/env python3
'store-landed -- is this commit on the mirrors, or only on this machine?\n\nLanding onto `main` is not shipping. For the agent-context store a commit is\nshipped only when a mirror has it, and the daemon can hold publishing for hours\n(an unsigned commit does that), with the only signal a "publishing HELD" line in\nthe daemon\'s own log. The check is one git command; this script\nexists so it is the same command every time and so its exit code can gate a claim.\n\nUsage:  python3 store-landed.py [sha-or-branch]   (default: main)\nExit:   0 every remote has it - 1 at least one does not - 2 could not check\n\nObservations guarded: #388.'
import glob
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_task  

STORE = (os.environ.get("AGENT_CONTEXT_STORE")
        or os.path.join(os.environ.get("HOME", ""), ".agent-context"))


def _git(store, *args, suppress_stderr=True):
    "Capture stdout as text. stderr is discarded by default (matches the shell\n    original's `2>/dev/null`); suppress_stderr=False passes it through untouched,\n    for the one call the original leaves unredirected."
    return subprocess.run(
        ["git", "-C", store] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if suppress_stderr else None,
        text=True,
    )


def _same_code(store, runs, want):
    
    
    
    if runs.startswith(want) or want.startswith(runs):
        return True
    try:
        
        if subprocess.run(["git", "-C", store, "merge-base", "--is-ancestor", want, runs],
                          capture_output=True, timeout=10).returncode == 0:
            return True
        d = subprocess.run(["git", "-C", store, "diff", "--quiet", runs, want, "--",
                            "server", ":!server/VERSION"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return d.returncode == 0


def _relay_only(store, uuid):
    ' relay only.'
    try:
        with open(os.path.join(store, "machines", "%s.toml" % uuid), encoding="utf-8") as fh:
            return any(re.match(r"\s*relay_only\s*=\s*true\s*(#.*)?$", line) for line in fh)
    except OSError:
        return False


def _report_server_adoption(store, want):
    
    
    
    
    
    
    
    
    best = {}
    for p in sorted(glob.glob(os.path.join(store, "machines", "*", "daemon-status.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        r.setdefault("machine_uuid", os.path.basename(os.path.dirname(p)))
        best[r["machine_uuid"]] = r
    try:
        refs = subprocess.run(["git", "-C", store, "for-each-ref", "--format=%(refname)",
                               "refs/fleet/"], capture_output=True, text=True,
                              timeout=10).stdout.split()
        for fleet_ref in refs:
            show = subprocess.run(["git", "-C", store, "show", fleet_ref + ":daemon-status.json"],
                                  capture_output=True, text=True, timeout=10)
            if show.returncode != 0:
                continue
            try:
                r = json.loads(show.stdout)
            except ValueError:
                continue
            u = r.get("machine_uuid")
            if not u:
                continue
            cur = best.get(u)
            if cur is None or (r.get("updated_at") or 0) >= (cur.get("updated_at") or 0):
                best[u] = r
    except (OSError, subprocess.SubprocessError):
        pass

    n_yes = n_all = n_relay = 0
    for u, r in sorted(best.items(), key=lambda kv: str(kv[1].get("machine_id"))):
        who = r.get("machine_id") or r.get("hostname") or u
        runs = r.get("server_commit")
        if _relay_only(store, u):
            n_relay += 1
            continue
        n_all += 1
        if runs and _same_code(store, runs, want):
            state, n_yes = "adopted", n_yes + 1
        elif runs:
            state = "not yet seen (last published row runs %s)" % runs
        else:
            state = "unknown: its daemon predates server_commit reporting"
        if r.get("sleeps"):
            state += ", sleeps"
        print("  %-9s %s" % (who, state))
    print("store-landed: %d of %d published daemon(s) adopted; rows lag one sync cycle each way."
          % (n_yes, n_all))
    if n_relay:
        print("store-landed: %d relay-only host(s) run no daemon and are not counted." % n_relay)


def main(argv):
    store = STORE
    ref = argv[1] if len(argv) > 1 else "main"

    proc = _git(store, "rev-parse", "--verify", ref + "^{commit}")
    if proc.returncode != 0:
        print("store-landed: no such commit or branch: %s" % ref, file=sys.stderr)
        return 2
    sha = proc.stdout.strip()

    
    
    fetch = subprocess.run(["git", "-C", store, "fetch", "--all", "-q"],
                           stderr=subprocess.DEVNULL)
    if fetch.returncode != 0:
        print("store-landed: fetch failed -- cannot tell what the mirrors hold", file=sys.stderr)
        return 2

    remotes_proc = _git(store, "remote", suppress_stderr=False)
    remotes = remotes_proc.stdout.split()
    if not remotes:
        print("store-landed: no remotes configured", file=sys.stderr)
        return 2

    missing = 0
    for r in remotes:
        has = subprocess.run(["git", "-C", store, "merge-base", "--is-ancestor", sha,
                              "refs/remotes/%s/main" % r], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL).returncode == 0
        if has:
            print("  %-7s has %s" % (r, sha[:12]))
        else:
            print("  %-7s does not have %s" % (r, sha[:12]))
            missing += 1

    if missing == 0:
        print("store-landed: %s is on every remote -- shipped." % sha[:12])
        diff_tree = _git(store, "diff-tree", "--no-commit-id", "--name-only", "-r", sha,
                         "--", "server/")
        if diff_tree.stdout.strip():
            want_proc = _git(store, "log", "-1", "--format=%h", sha, "--", "server/")
            want = want_proc.stdout.strip()
            print("store-landed: this commit touches server/ -- daemons must adopt %s:" % want)
            _report_server_adoption(store, want)
        return 0

    
    
    
    home = os.environ.get("HOME", "")
    info = None
    for f in (os.path.join(home, "Library", "Application Support", "agent-context", "daemon.info"),
              os.path.join(home, ".local", "state", "agent-context", "daemon.info")):
        if os.access(f, os.R_OK):
            info = f
            break
    if info is not None:
        err = ""
        try:
            with open(info, encoding="utf-8") as fh:
                d = json.load(fh)
            val = d.get("last_sync_error")
            err = str(val) if val else ""
        except (OSError, ValueError):
            err = ""
        if err:
            print("store-landed: daemon reports: %s" % err)
    print("store-landed: not shipped -- %d remote(s) lack it. Do not report this change as done."
          % missing, file=sys.stderr)
    return 1


if __name__ == "__main__":
    store_task.main_or_forward("store-landed", lambda: main(sys.argv), store=STORE)
