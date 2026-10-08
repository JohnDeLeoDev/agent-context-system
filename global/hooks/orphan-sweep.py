#!/usr/bin/env python3

"SessionStart: reap two leaks that nothing else in the system owns.\n\n1. Local: leaked `opencode acp` servers.\n\n2. Fleet: stray throwaway pi tmux sessions.\n\nDevice passes and probes start pi in a detached tmux session named `pi`, which\ncollides and auto-suffixes to `pi-2`, `pi-3`, ... The spawning agent is supposed to\nkill its own session, but the failure mode is the spawner dying or compacting first,\nso an ownership rule cannot fix it and a sweep can. Unlike the acp reap this one goes\nover the fleet, because the sessions pile up on the host that was measured while\nthe agent that made them ran somewhere else, and a local-only sweep would not reach\nthem until an agent happened to start there.\n\nThe predicate is narrow. Only a detached session whose name is\n`pi` or `pi-<digits>` and whose last activity is older than ORPHAN_PI_MIN_SECS\n(default 3600, 1 h) is killed. That spares every named fixture (`ao-pi-fixture`,\n`ao-copilot-fixture`, ...), every session someone is attached to, and every session\nnamed after a project. Best effort: BatchMode ssh with a short connect\ntimeout, all hosts in parallel, and an unreachable host is skipped so it does not\ndelay a session start. ConnectTimeout alone is not enough: a host can accept the\nTCP connection and then hang the session. So every wait\nshares one wall-clock budget, ORPHAN_SWEEP_DEADLINE_SECS (default 6), and ssh\nkeepalives drop a session that stops answering.\n\nOne down host must not cost every session start that budget. So the remote sweep runs at\nmost once per ORPHAN_SWEEP_REMOTE_EVERY_SECS (default 600) on a machine, and a host\nthat fails is skipped for ORPHAN_SWEEP_DOWN_SECS (default 1800). A stray pi session\nonly counts after an hour idle, so neither delay leaves one behind for long. State\nlives in ~/.local/state/agent-context/orphan-sweep/.\n\nThe session start waits on nothing. Even bounded, a tmux and ssh sweep run inline\nwould hold the start for up to the 6 s budget plus process start-up, which under\nload crosses the dispatcher's 10 s limit. A sweep is\nhousekeeping no session start depends on. So the hook does only the local `ps` reap\n(about 0.1 s), prints what the previous sweep killed, and, when a sweep is due, starts\n`orphan-sweep.py --sweep` in a session of its own with no pipe back, and returns. The\nsweep writes its line to `report`, which the next session start prints and removes.\nORPHAN_SWEEP_INLINE=1 runs the sweep in the foreground, for a person who wants to see\nit run and for the hook tests that assert what it did."
import os
import signal
import subprocess
import sys
import time





PI_PROBE = r'''min="$1"
tm="$(command -v tmux 2>/dev/null || echo /opt/bin/tmux)"
[ -x "$tm" ] || exit 0
now=$(date +%s)
"$tm" list-sessions -F "#{session_name} #{session_attached} #{session_activity}" 2>/dev/null |
while read -r n attached act; do
  [ "$attached" = 0 ] || continue
  case "$n" in pi|pi-[0-9]*) ;; *) continue ;; esac
  [ $(( now - act )) -ge "$min" ] || continue
  "$tm" kill-session -t "=$n" 2>/dev/null && printf "%s " "$n"
done'''


def etime_secs(e):
    'ps etime "[[dd-]hh:]mm:ss" -> seconds.'
    d = 0
    if "-" in e:
        d_str, e = e.split("-", 1)
        d = int(d_str) if d_str.isdigit() else 0
    parts = e.split(":")
    h = m = s = 0
    try:
        if len(parts) == 3:
            h, m, s = (int(p) for p in parts)
        elif len(parts) == 2:
            m, s = (int(p) for p in parts)
        elif len(parts) == 1:
            s = int(parts[0]) if parts[0] else 0
    except ValueError:
        return 0
    return d * 86400 + h * 3600 + m * 60 + s


def sweep_opencode_acp(min_age):
    killed = []
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,etime=,command="],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return killed
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etime, cmd = parts
        if ppid != "1":
            continue
        if "opencode acp" not in cmd:
            continue
        age = etime_secs(etime)
        if age < min_age:
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed.append("%s(up %s)" % (pid, etime))
        except (OSError, ValueError):
            continue
    return killed


def state_dir():
    return os.path.join(os.path.expanduser("~"), ".local", "state", "agent-context",
                        "orphan-sweep")


def _stamp_age(name):
    try:
        return time.time() - os.stat(os.path.join(state_dir(), name)).st_mtime
    except OSError:
        return None


def _touch(name):
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(os.path.join(state_dir(), name), "a"):
            pass
        os.utime(os.path.join(state_dir(), name))
    except OSError:
        pass


def _clear(name):
    try:
        os.remove(os.path.join(state_dir(), name))
    except OSError:
        pass


def sweep_due(every):
    'Whether a tmux sweep should start now: none ran in the last `every` seconds.'
    age = _stamp_age("last-remote")
    return age is None or age >= every


def up_hosts(hosts, down_for):
    'Every host not marked down in the last `down_for` seconds.'
    out = []
    for h in hosts:
        down = _stamp_age("down-" + h)
        if down is None or down >= down_for:
            out.append(h)
    return out


def sweep_pi_sessions(pi_min, hosts, domain, deadline):
    results = {}
    procs = {}
    try:
        local = subprocess.run(
            ["sh", "-c", PI_PROBE, "sh", str(pi_min)],
            capture_output=True, text=True,
            timeout=max(deadline - time.monotonic(), 0.1),
        )
        results["local"] = local.stdout
    except Exception:
        results["local"] = ""

    for h in hosts:
        try:
            procs[h] = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                 "-o", "ServerAliveInterval=2", "-o", "ServerAliveCountMax=1",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "%s.%s" % (h, domain), "sh -s %s" % pi_min],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:
            procs[h] = None

    for h, p in procs.items():
        if p is None:
            results[h] = ""
            continue
        try:
            out, _ = p.communicate(
                input=PI_PROBE, timeout=max(deadline - time.monotonic(), 0.1))
            results[h] = out or ""
            if p.returncode == 255:      
                _touch("down-" + h)
            else:
                _clear("down-" + h)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
            results[h] = ""
            _touch("down-" + h)

    swept = []
    for name, out in results.items():
        out = out.strip()
        if not out:
            continue
        cleaned = " ".join(out.split())
        swept.append("%s:%s" % (name, cleaned))
    return swept


def sweep():
    'The tmux sweep, local and fleet: the part that can wait on a wedged tmux server or\n    a host that stops answering. Its line goes to `report` for the next session start.'
    pi_min = int(os.environ.get("ORPHAN_PI_MIN_SECS") or 3600)
    
    
    hosts_env = os.environ.get("ORPHAN_SWEEP_HOSTS")
    if hosts_env is None:
        hosts_env = os.environ.get("AGENT_CONTEXT_VERIFY_HOSTS") or ""
    hosts = up_hosts(hosts_env.split(), float(os.environ.get("ORPHAN_SWEEP_DOWN_SECS") or 1800))
    domain = os.environ.get("AGENT_CONTEXT_FLEET_DOMAIN") or "example.invalid"
    deadline = time.monotonic() + float(
        os.environ.get("ORPHAN_SWEEP_DEADLINE_SECS") or 6)
    swept = sweep_pi_sessions(pi_min, hosts, domain, deadline)
    if swept:
        try:
            with open(os.path.join(state_dir(), "report"), "a", encoding="utf-8") as fh:
                fh.write("orphan-sweep: killed idle pi tmux session(s): %s\n" % " ".join(swept))
        except OSError:
            pass


def take_report():
    
    
    path = os.path.join(state_dir(), "report")
    claimed = "%s.%d" % (path, os.getpid())
    try:
        os.rename(path, claimed)
    except OSError:
        return ""
    try:
        with open(claimed, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""
    finally:
        try:
            os.remove(claimed)
        except OSError:
            pass


def start_sweep():
    
    
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--sweep"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        pass


def main():
    min_age = int(os.environ.get("ORPHAN_ACP_MIN_SECS") or 86400)
    killed = sweep_opencode_acp(min_age)
    if killed:
        print("orphan-sweep: terminated leaked opencode acp server(s): %s"
              % " ".join(killed))

    if sweep_due(float(os.environ.get("ORPHAN_SWEEP_REMOTE_EVERY_SECS") or 600)):
        
        
        _touch("last-remote")
        if os.environ.get("ORPHAN_SWEEP_INLINE") == "1":
            sweep()
        else:
            start_sweep()

    report = take_report()
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(sweep() if sys.argv[1:] == ["--sweep"] else main())
    except Exception:
        sys.exit(0)
