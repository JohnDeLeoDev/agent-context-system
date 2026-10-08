
'Fleet health — is every machine syncing, and is every machine on current code?\n\nEvery health signal is per-machine and self-reported, so nothing else can answer\n"is the fleet converged?".\n\nThe store is already replicated to every machine, so it carries this itself. Each\ndaemon publishes a small status file into its own directory; any machine can then\nread the fleet\'s state out of the store with no SSH and no new transport. A machine\nwhose sync is dead stops updating its file, and its staleness is the signal.\n\nChurn discipline: `updated_at` is bucketed to the hour, so an unchanging healthy\nmachine rewrites this file at most once an hour and commits at most once an hour.\nA machine whose verdict changes rewrites immediately, because a state change should\nbe published at once. Frequent telemetry commits are costly, since every commit is\nan integration on every machine.'

import contextlib
import json
import re
import socket
import subprocess
import time
from pathlib import Path

from . import deps_report
from .paths import write_atomic
from .relay_source import current_etag as current_relay_etag_of


def current_relay_etag(store_root) -> str:
    'The relay source ETag ls serves now, for the `server/` tree of the store.'
    return current_relay_etag_of(Path(store_root) / "server")



_BUCKET_SECS = 3600



STALE_SECS = 3 * 3600

EARLY_STALE_SECS = 90 * 60













SLEEPER_GONE_SECS = 7 * 86400






CODE_GRACE_SECS = 30 * 60



RESTART_LOOP_STARTS = 3

_LOOP_CAUSES = "(a supervisor reload loop, a second supervisor, a pkill)"
_LOOP_READ = ("the daemon log's 'shutdown on' lines for the signal and its parent, then "
              "`launchctl print` / `systemctl status` for who keeps reloading the job.")


def restart_loop_message(reader: str, starts: object = None, who: str = "") -> str:
    'The restart-loop wording, defined once for its three readers: "fleet" (a row in\n    another machine\'s fleet health), "health" (the daemon\'s own health record) and\n    "session" (the note in this machine\'s session bootstrap).'
    if reader == "fleet":
        return (f"{who}: its daemon has started {starts} times in the last hour — "
                f"a restart loop, so every store call there can time out. "
                f"The store code is not the first suspect: something outside "
                f"the daemon is killing it {_LOOP_CAUSES}. On that machine read {_LOOP_READ}")
    if reader == "health":
        return (f"this machine's store daemon has started {starts} times in the last "
                f"hour: something outside it is killing it every few seconds "
                f"{_LOOP_CAUSES}. Every store call here can time out mid-request until it "
                f"stops. The store code is not the first suspect. Read {_LOOP_READ}")
    return ("this machine's store daemon is being killed and restarted every "
            "few seconds by something OUTSIDE it (policy): store calls here "
            "can time out mid-request. Say so to user. The store code is not "
            "the first suspect; the daemon log's 'shutdown on' lines and the "
            "supervisor's own log say who keeps reloading it.")







PROJECTION_STALE_SECS = 2 * 86400


def _adoption_payload(adoption) -> dict | None:
    'The publishable subset of an adoption probe: absolute facts only, no ages.\n\n    Returns None when there is nothing to say, so a daemon that cannot probe writes\n    the same bytes it always did and an idle fleet keeps generating no commits.'
    if not isinstance(adoption, dict):
        return None
    proj = adoption.get("projection")
    proj = proj if isinstance(proj, dict) else {}
    stale = adoption.get("stale_daemons")
    at = proj.get("at")
    return {
        
        
        
        
        "projected_at": (int(at // _BUCKET_SECS) * _BUCKET_SECS
                         if isinstance(at, (int, float)) else None),
        "projected_commit": proj.get("commit"),
        
        
        "stale_daemons": list(stale) if isinstance(stale, list) else None,
    }


def _adoption(row) -> dict:
    'The adoption block, defaulted. A row published by a daemon too old to send one\n    reads as all-unknown rather than as all-clear -- the direction that cannot invent\n    good news about a machine that never spoke.'
    a = row.get("adoption")
    return a if isinstance(a, dict) else {}


def status_path(store_root, machine_uuid) -> Path:
    return Path(store_root) / "machines" / str(machine_uuid) / "daemon-status.json"









_VOLATILE_FIELDS = (("adoption", "projected_commit"),)


def _masked(row: dict) -> str:
    "The row's bytes with every volatile field blanked, for identity tests."
    row = json.loads(json.dumps(row))          
    for path in _VOLATILE_FIELDS:
        node = row
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and path[-1] in node:
            node[path[-1]] = None
    return json.dumps(row, separators=(",", ":"), sort_keys=True)


def _same_but_for_volatile(existing_text: str, payload: dict) -> bool:
    "True when the on-disk row and the new payload differ in nothing that earns a\n    commit. A row that is not JSON is never 'the same'."
    try:
        existing = json.loads(existing_text)
    except ValueError:
        return False
    return isinstance(existing, dict) and _masked(existing) == _masked(payload)


def _build(server_dir) -> str | None:
    'The `build=NN` line from server/VERSION — the number humans actually quote.'
    with contextlib.suppress(OSError):
        m = re.search(r"^build=(\S+)", Path(server_dir, "VERSION").read_text(), re.MULTILINE)
        if m:
            return m.group(1)
    return None


def publish(store_root, machine_uuid, health: dict, machine_id=None,
            hostname=None, server_dir=None, now=None, adoption=None, deps=None) -> bool:
    "Write this machine's status into the store. True when the file changed.\n\n    Byte-identical writes are skipped, which combined with the hourly bucket is what\n    keeps an idle fleet from generating commits. Never raises: a status file is\n    diagnostics, and diagnostics must not be able to break the sync that carries it."
    t = time.time() if now is None else now
    p = status_path(store_root, machine_uuid)
    current = health.get("code_current")
    
    
    
    stale_since = None
    if current is False:
        with contextlib.suppress(OSError, ValueError):
            stale_since = json.loads(p.read_text()).get("code_stale_since")
        if not stale_since:
            stale_since = int(t)
    payload = {
        "machine_id": machine_id,
        
        
        
        "machine_uuid": str(machine_uuid),
        "hostname": hostname,
        "build": _build(server_dir) if server_dir else None,
        "code_version": health.get("code_version"),
        
        
        
        
        "server_commit": health.get("server_commit"),
        "code_current": current,
        "code_stale_since": stale_since,
        
        
        
        "code_defer_reason": health.get("code_defer_reason") if current is False else None,
        "verdict": health.get("verdict"),
        
        
        
        
        "sync_reason": (str(health.get("last_sync_error") or "")[:300] or None
                        if health.get("verdict") not in (None, "healthy", "starting")
                        else None),
        
        
        
        
        
        "adoption": _adoption_payload(adoption),
        
        
        
        "deps": deps if isinstance(deps, dict) else None,
        
        "updated_at": int(t // _BUCKET_SECS) * _BUCKET_SECS,
    }
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with contextlib.suppress(OSError):
        if _same_but_for_volatile(p.read_text(), payload):
            return False
    try:
        
        
        write_atomic(p, blob)
        return True
    except OSError:
        return False


def with_live(row_text: str, sessions=None, recent_writes=None, starts_last_hour=None) -> str:
    'The row as it travels OUT OF BAND (refs/fleet), with the live facts added.\n\n    `sessions` (claims.read_live) and `recent_writes` (store.recent_writes) change\n    every few minutes while anyone is working, so they must not go into the in-tree\n    row: that file is committed, and the hourly bucket on `updated_at` exists\n    precisely so an active fleet does not commit every cycle. The ref row is\n    force-pushed and parentless, so churn there costs a small push and no history.\n    read_all merges the two rows newest-first with the ref winning a tie, which is\n    how a reader sees the live fields at all.'
    try:
        row = json.loads(row_text)
    except ValueError:
        return row_text
    if not isinstance(row, dict):
        return row_text
    if sessions:
        row["sessions"] = sessions
    if recent_writes:
        row["recent_writes"] = recent_writes
    
    
    if starts_last_hour:
        row["starts_last_hour"] = int(starts_last_hour)
    return json.dumps(row, separators=(",", ":"), sort_keys=True)


def _sleeps(base, uuid) -> bool:
    'Is this machine recorded as one that sleeps? (machines/<uuid>.toml)\n\n    Read from the machine record and not from the published status, because the\n    status is what a sleeping machine stops writing. Anything a machine has\n    to be awake to tell us is useless for deciding whether being quiet is normal.\n\n    Best-effort: an unreadable or malformed record means "not known to sleep", which\n    reports the machine as before. The safe default here is the noisy one.'
    with contextlib.suppress(OSError, ValueError):
        import tomllib
        return bool(tomllib.loads((base / f"{uuid}.toml").read_text()).get("sleeps"))
    return False


def _relay_only(base, uuid) -> bool:
    'Is this machine recorded as one that runs no daemon and reaches the store through ls?\n    (machines/<uuid>.toml, `set_machine(relay_only=True)`.) Read from the record for the same\n    reason as `_sleeps`: the published status is what such a machine never writes. An\n    unreadable record means "has a daemon", which reports the machine as before.'
    with contextlib.suppress(OSError, ValueError):
        import tomllib
        return tomllib.loads((base / f"{uuid}.toml").read_text()).get("relay_only") is True
    return False


def _relay_only_row(base, uuid, seen_at, fallback=None) -> dict | None:
    "The row for a machine recorded relay-only, built only from facts such a machine can\n    still produce: identity, and the last time its relay traffic was seen. None when the\n    record is missing or not relay-only.\n\n    Never reads a leftover daemon-status.json for verdict / code_current / sync_reason /\n    adoption: once a machine is relay_only its daemon is gone, and that file is stale\n    state from the daemon that used to run there (policy), which a reader would take as\n    the machine's current state. `machine_id` / `hostname` fall back\n    to the retired daemon's last row when the machine record itself does not carry them.\n    `age_secs` / `stale` are left for the caller to fill in from `updated_at`, the same as\n    every other row."
    with contextlib.suppress(OSError, ValueError):
        import tomllib
        rec = tomllib.loads((base / f"{uuid}.toml").read_text())
        if rec.get("relay_only") is not True:
            return None
        fallback = fallback or {}
        if seen_at is None:
            seen_at = fallback.get("updated_at")
        return {"machine_uuid": uuid,
                "machine_id": rec.get("machine_id") or fallback.get("machine_id"),
                "hostname": rec.get("hostname") or fallback.get("hostname"),
                "updated_at": seen_at}
    return None


def _ref_rows(store_root) -> list[dict]:
    'Rows that arrived OUT OF BAND: refs/fleet/<remote>/<machine_id>, each a\n    one-file commit a daemon force-pushed (store._publish_fleet_ref). These exist so a\n    machine whose integration into main is broken can still say so -- the in-tree row\n    travels with main and so could never carry that news. Best-effort:\n    no git, no refs, or an unreadable blob simply means no rows from here.'
    out = []
    try:
        refs = subprocess.run(
            ["git", "-C", str(store_root), "for-each-ref", "--format=%(refname)", "refs/fleet/"],
            capture_output=True, text=True, timeout=10, check=False).stdout.split()
        for ref in refs:
            show = subprocess.run(
                ["git", "-C", str(store_root), "show", f"{ref}:daemon-status.json"],
                capture_output=True, text=True, timeout=10, check=False)
            if show.returncode != 0:
                continue
            with contextlib.suppress(ValueError):
                row = json.loads(show.stdout)
                if isinstance(row, dict) and row.get("machine_uuid"):
                    row["via_ref"] = ref
                    out.append(row)
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def read_all(store_root, now=None, stale_secs=STALE_SECS, include_remote=True) -> list[dict]:
    "Every machine's last published status, newest first, each with `stale` and\n    `age_secs` resolved against the caller's clock.\n\n    Two sources per machine, the in-tree row and any out-of-band ref rows, and the\n    newest wins; on a tie the ref row does, because it is force-updated on every\n    change while the tree row waits for a merge that may never come."
    t = time.time() if now is None else now
    best = {}
    base = Path(store_root) / "machines"
    with contextlib.suppress(OSError):
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            with contextlib.suppress(OSError, ValueError):
                row = json.loads((d / "daemon-status.json").read_text())
                row["machine_uuid"] = d.name
                best[d.name] = row
    for row in _ref_rows(store_root):
        u = str(row["machine_uuid"])
        cur = best.get(u)
        if cur is None or (row.get("updated_at") or 0) >= (cur.get("updated_at") or 0):
            best[u] = row
    
    
    
    
    
    daemon_uuids = {u for u in best if not _relay_only(base, u)}
    from . import claims  
    relay_seen = claims.relay_seen()
    relay_etags = claims.relay_etags()
    try:
        current_etag = current_relay_etag(store_root)
    except (OSError, ValueError):
        current_etag = None
    
    
    
    
    relay_only_candidates = {u for u in best if _relay_only(base, u)} | set(relay_seen)
    for u in relay_only_candidates:
        if (row := _relay_only_row(base, u, relay_seen.get(u), fallback=best.get(u))):
            best[u] = row
    rows = []
    for u, row in best.items():
        age = t - (row.get("updated_at") or 0)
        row["age_secs"] = int(age)
        row["stale"] = age > stale_secs
        row["sleeps"] = _sleeps(base, u)
        row["relay_only"] = _relay_only(base, u)
        
        
        
        
        if u not in daemon_uuids:
            row["deps"] = deps_report.read(base / u / "deps.json", now=t)
        if u in relay_seen:
            row["relay_last_seen"] = relay_seen[u]
        if u in relay_etags and current_etag is not None:
            row["relay_etag"] = relay_etags[u]
            row["relay_stale"] = relay_etags[u] != current_etag
        rows.append(row)
    rows.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
    if not include_remote:      
        return rows
    return merge_remote_claims(rows, claims.read_remote(now=now))




_LOG_HINT = "last_sync_error / daemon log"


def problems(rows, now=None, code_grace_secs=CODE_GRACE_SECS) -> list[str]:
    'One human-readable line per machine that needs attention, or [].\n\n    Two conditions, distinct because the remedies differ:\n      - **not reporting** — nobody has heard from it. Three causes, and this line\n        must not pick one: the loop is dead, the machine cannot reach the store,\n        or the loop is alive and its integration keeps failing (a conflict it\n        cannot merge). The third is invisible from here: a machine publishes\n        daemon-status.json through the sync that is broken, so its last row still\n        reads `verdict: healthy` from the last cycle that landed. Restarting is\n        right for the first cause and useless for the third: it does not resolve a\n        conflict, and it discards the in-process failure streak that names it. So\n        say what is known (silence) and where the answer lives (that machine\'s\n        daemon log / last_sync_error), and let the reader choose.\n      - **stale code** — it syncs fine but has not adopted the code on disk for\n        longer than a release takes to land, which means its deploy gate is\n        failing. Read that machine\'s gate lines; do not just restart it, since a\n        restart cold-starts onto the new code and hides the gate failure that will\n        strand the next build too.\n\n    A machine inside the grace window is not a problem: every machine is briefly\n    behind after a release and adopting is the healthy path. Saying "your gate is\n    failing" then is a false alarm on every release.'
    t = time.time() if now is None else now
    out = []
    for r in rows:
        who = r.get("machine_id") or r.get("hostname") or r.get("machine_uuid", "?")[:8]
        if r.get("relay_only"):
            
            
            
            
            
            out.extend(_deps_problems(r, who))
            continue
        if (r.get("reachable") and not r.get("sleeps") and not r.get("stale")
                and r.get("verdict") in (None, "healthy", "starting")):
            out.append(f"{who}: reachable but not reporting for {r['age_secs'] // 60}m. "
                       f"Read that machine's {_LOG_HINT}.")
            continue
        if r.get("stale"):
            relay_seen_at = r.get("relay_last_seen")
            if isinstance(relay_seen_at, (int, float)) and relay_seen_at > (r.get("updated_at") or 0):
                
                
                
                
                
                
                out.append(f"{who}: its daemon has not published in {r['age_secs'] // 3600}h, "
                           f"but the relay has seen it since -- its daemon looks gone "
                           f"without being marked. If that was done deliberately, run "
                           f"set_machine(machine='{who}', relay_only=True); if not, look at "
                           f"its daemon.")
                continue
            
            
            
            if r.get("sleeps"):
                if r["age_secs"] < SLEEPER_GONE_SECS:
                    continue
                out.append(f"{who}: recorded as a machine that sleeps, so its quiet "
                           f"is normally ignored — but it has now been silent for "
                           f"{r['age_secs'] // 86400} days, which is long for a nap. "
                           f"Worth checking it came back up at all.")
                continue
            
            
            
            
            
            
            
            
            
            out.append(f"{who}: not reporting for {r['age_secs'] // 3600}h. First ask "
                       f"whether it is even up (ping / tailscale status / ssh): asleep "
                       f"or powered off is the common case and needs no action at all. "
                       f"If it is reachable, then its sync loop is dead, it cannot "
                       f"reach the store, or its integration keeps failing (a conflict "
                       f"it cannot merge) — read that machine's {_LOG_HINT}, since "
                       f"restarting fixes the first and does nothing for the last")
        elif r.get("code_current") is False:
            since = r.get("code_stale_since")
            behind_for = (t - since) if since else 0
            if behind_for <= code_grace_secs:
                continue                  
            
            
            
            
            head = (f"{who}: pinned on old code (build {r.get('build')}) for "
                    f"{int(behind_for) // 60}m — that is past the "
                    f"{code_grace_secs // 60}m a release takes to land")
            why = r.get("code_defer_reason")
            if why:
                out.append(f"{head}. That machine reports: {why}")
            else:
                out.append(f"{head}. It has not said why — it may be running a daemon "
                           f"too old to report one. Read its log before acting, and do "
                           f"not assume the gate failed: 'never ran' looks identical "
                           f"from here. Restarting masks a gate failure rather than "
                           f"fixing it.")
        elif r.get("verdict") not in (None, "healthy", "starting"):
            
            
            
            
            why = r.get("sync_reason")
            
            
            
            
            if why:
                tail = f" — {why}"
            elif "sync_reason" in r:
                tail = f". It has not said why; read its {_LOG_HINT} on that machine."
            else:
                tail = (". It has not said why (a daemon too old to publish the "
                        f"reason); read its {_LOG_HINT}.")
            out.append(f"{who}: its own daemon reports sync {r['verdict']}{tail}")
        
        
        
        
        
        
        
        if not r.get("stale"):
            out.extend(_adoption_problems(r, t, who))
            out.extend(_deps_problems(r, who))
            
            
            
            n = r.get("starts_last_hour")
            if isinstance(n, int) and n > RESTART_LOOP_STARTS:
                out.append(restart_loop_message("fleet", n, who))
    return out


def _adoption_problems(r, t, who) -> list[str]:
    'Lines for a machine that has synced code it has not put into use.\n\n    Each names the remedy, because neither announces itself and a reader meeting one\n    for the first time has no reason to know what to do about it.'
    out = []
    a = _adoption(r)
    at = a.get("projected_at")
    if isinstance(at, (int, float)) and (t - at) > PROJECTION_STALE_SECS:
        days = int(t - at) // 86400
        commit = a.get("projected_commit")
        seen = f" (last projected store {commit})" if commit else ""
        out.append(f"{who}: harness projection is {days}d old{seen}. Projection runs "
                   f"only from a SessionStart hook — there is no timer for it — so on "
                   f"a machine nobody logs into, the store is current and its "
                   f"harnesses are not. Run `home-materialize.py --force` there, or "
                   f"start a session on it.")
    stale = a.get("stale_daemons")
    if stale:
        names = ", ".join(sorted(stale))
        out.append(f"{who}: {len(stale)} language-server daemon(s) running superseded "
                   f"code: {names}. A daemon is pinned to the build it was spawned "
                   f"with and --restart does not change that. Retire each with "
                   f"`lspd.py --upgrade --key <k>` when no session is attached; they "
                   f"also adopt it on their own at the next natural restart.")
    return out


def _deps_problems(r, who) -> list[str]:
    "One line for a machine whose deps-check reports a missing, broken or below-floor tool.\n\n    A row with no `deps` block (a daemon too old to send one, or no report on that machine) says\n    nothing: unknown is not a finding and is not clean either. The line names the tools; the fix\n    for each is in `deps-check.py`'s output on that machine."
    block = r.get("deps")
    if isinstance(block, dict) and block.get("stale") is True:
        return [(f"{who}: its dependency report is more than 3 days old, so the checker has not "
                 f"run there. Run `deps-check.py` on that machine.")]
    if not isinstance(block, dict) or block.get("ok") is not False:
        return []
    parts = []
    for key, label in (("missing", "missing"), ("broken", "broken"), ("below_floor", "below floor")):
        names = block.get(key)
        if isinstance(names, list) and names:
            parts.append(f"{label} {', '.join(str(n) for n in names)}")
    if not parts:
        return []
    return [(f"{who}: dependency problem: {'; '.join(parts)}. Run `deps-check.py` on that "
             f"machine; each line ends with the fix.")]


def _ssh_reachable(hostname):
    'A bounded liveness probe; a refused TCP connection still means a live host.'
    try:
        with socket.create_connection((hostname, 22), timeout=0.35):
            return True
    except ConnectionRefusedError:
        return True
    except (OSError, TimeoutError):
        return False


def add_early_reachability(rows):
    'Probe only a quiet, non-sleeping machine before the ordinary stale alarm.'
    for row in rows:
        if (not row.get("sleeps") and not row.get("relay_only") and not row.get("stale")
                and row.get("age_secs", 0) > EARLY_STALE_SECS):
            hostname = row.get("hostname")
            if hostname:
                row["reachable"] = _ssh_reachable(hostname)


def health(store_root, now=None, stale_secs=STALE_SECS) -> dict:
    rows = read_all(store_root, now=now, stale_secs=stale_secs)
    add_early_reachability(rows)
    probs = problems(rows, now=now)
    return {
        "machines": rows,
        "problems": probs,
        "converged": not probs,
        "reporting": len(rows),
        "stale_after_secs": stale_secs,
    }


def merge_remote_claims(rows: list[dict], remote_claims: list[dict]) -> list[dict]:
    'Add each remote session to the fleet row of the machine it runs on.\n\n    A machine with no row yet (a relay-only machine publishes no daemon status) gets a\n    minimal one, so its sessions are still named. The input rows are not mutated.'
    out = [{**r, "sessions": list(r.get("sessions") or [])} for r in rows]
    by_uuid = {str(r.get("machine_uuid")): r for r in out}
    for claim in remote_claims:
        uuid = str(claim.get("machine_uuid"))
        row = by_uuid.get(uuid)
        if row is None:
            row = {"machine_uuid": claim.get("machine_uuid"), "machine_id": claim.get("machine_id"),
                   "updated_at": claim.get("last_seen"), "age_secs": 0, "stale": False,
                   "relay_only": True, "sessions": []}
            by_uuid[uuid] = row
            out.append(row)
        row["sessions"].append({k: v for k, v in claim.items()
                                if k not in ("machine_uuid", "machine_id")})
    return out
