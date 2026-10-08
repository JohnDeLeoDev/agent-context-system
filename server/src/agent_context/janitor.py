
'The store keeps itself tidy: an hourly sweep for orphans and stale data.\n\nThe daemon looks once an hour, on every machine, and acts only where the action has\none correct answer:\n\n  * worktree entries whose directory is gone            -> pruned\n  * linked worktrees whose branch is fully merged into\n    main, clean, and untouched for a day                -> removed, branch deleted\n  * local branches fully merged into main, untouched\n    for a day, not main and not checked out             -> deleted\n  * write litter (*.tmp, *.part) under the synced\n    buckets, older than an hour                         -> deleted\n  * refs/fleet/<remote>/<id> for a machine the store\n    has no record of                                    -> local ref dropped\n  * the invariant registry (invariant-check --health)   -> re-run, so a rule\n    broken on this machine is in the health verdict\n    within the hour, not at the next session start\n  * spent audit observations and handoffs (daily, on\n    the fleet filer only)                               -> moved to their archives,\n    and the root pages regenerated when a handoff moved\n\nNever touched: anything dirty, anything unmerged (including every parked/* branch,\nwhich exists precisely because a human has not decided), the main checkout, and\nanything the sweep cannot classify. Every action is logged; the result dict is\nwhat the sync loop records. Best-effort throughout: a sweep that raises would take\ndown the loop it rides on, and a missed hour costs nothing.'
from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("agent-context")

SWEEP_SECS = 3600.0
STALE_SECS = 24 * 3600.0
LITTER_SECS = 3600.0
_LITTER_SUFFIXES = (".tmp", ".part")
_LITTER_ROOTS = ("global", "projects", "workspaces", "machines")
_SWEPT_AT = 0.0


def _git(root, *args, timeout=30):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, check=False, timeout=timeout)


def _merged_into_main(root, ref) -> bool:
    return _git(root, "merge-base", "--is-ancestor", ref, "main").returncode == 0


def _newest_mtime(path) -> float:
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in filenames:
            with contextlib.suppress(OSError):
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
    return newest


def _linked_worktrees(root):
    '[(path, branch)] for every linked worktree (never the main one).'
    out, path, branch = [], None, None
    for line in _git(root, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            path, branch = line[9:], None
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):]
        elif line == "" and path:
            if os.path.abspath(path) != os.path.abspath(str(root)):
                out.append((path, branch))
            path, branch = None, None
    if path and os.path.abspath(path) != os.path.abspath(str(root)):
        out.append((path, branch))
    return out


def sweep_worktrees(root, now, stale_secs=STALE_SECS) -> dict:
    res = {"pruned": False, "removed": [], "kept": []}
    _git(root, "worktree", "prune")
    res["pruned"] = True
    for path, branch in _linked_worktrees(root):
        why = None
        if not os.path.isdir(path):
            continue                                    
        if branch is None:
            why = "detached"
        elif branch.startswith("parked/"):
            why = "parked"
        elif _git(path, "status", "--porcelain").stdout.strip():
            why = "dirty"
        elif not _merged_into_main(root, branch):
            why = "unmerged"
        elif now - _newest_mtime(path) < stale_secs:
            why = "recent"
        if why:
            res["kept"].append((path, why))
            continue
        if _git(root, "worktree", "remove", path).returncode == 0:
            _git(root, "branch", "-d", branch)
            res["removed"].append((path, branch))
            log.warning("agent-context janitor: removed merged, idle worktree %s (branch %s)",
                        path, branch)
    return res


def sweep_branches(root, now, stale_secs=STALE_SECS) -> dict:
    res = {"deleted": [], "kept": []}
    current = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    checked_out = {b for _p, b in _linked_worktrees(root) if b}
    fmt = "%(refname:short)\t%(committerdate:unix)"
    for line in _git(root, "for-each-ref", f"--format={fmt}", "refs/heads/").stdout.splitlines():
        name, _, when = line.partition("\t")
        if not name or name == "main" or name == current or name in checked_out:
            continue
        if name.startswith("parked/"):
            res["kept"].append((name, "parked"))
            continue
        if not _merged_into_main(root, name):
            res["kept"].append((name, "unmerged"))
            continue
        with contextlib.suppress(ValueError):
            if now - float(when or 0) < stale_secs:
                res["kept"].append((name, "recent"))
                continue
        if _git(root, "branch", "-d", name).returncode == 0:
            res["deleted"].append(name)
            log.warning("agent-context janitor: deleted merged, idle branch %s", name)
    return res


def sweep_litter(root, now, litter_secs=LITTER_SECS) -> list[str]:
    gone = []
    for bucket in _LITTER_ROOTS:
        base = Path(root) / bucket
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or not p.name.endswith(_LITTER_SUFFIXES):
                continue
            with contextlib.suppress(OSError):
                if now - p.stat().st_mtime > litter_secs:
                    p.unlink()
                    gone.append(str(p.relative_to(root)))
    if gone:
        log.warning("agent-context janitor: removed %d write-litter file(s): %s",
                    len(gone), ", ".join(gone[:6]))
    return gone


def sweep_fleet_refs(root) -> list[str]:
    "Drop local refs/fleet/<remote>/<id> rows for machines the store does not know.\n    The remote copy is left alone: a retired machine's last word is cheap to keep\n    there and a human may want it."
    known = set()
    mdir = Path(root) / "machines"
    with contextlib.suppress(OSError):
        for t in mdir.glob("*.toml"):
            txt = t.read_text(errors="replace")
            for line in txt.splitlines():
                if line.startswith("machine_id"):
                    known.add(line.split("=", 1)[1].strip().strip('"'))
    if not known:
        return []                       
    dropped = []
    for ref in _git(root, "for-each-ref", "--format=%(refname)", "refs/fleet/").stdout.split():
        mid = ref.rsplit("/", 1)[-1]
        if mid not in known and _git(root, "update-ref", "-d", ref).returncode == 0:
            dropped.append(ref)
    if dropped:
        log.warning("agent-context janitor: dropped %d fleet ref(s) for unknown machines: %s",
                    len(dropped), ", ".join(dropped))
    return dropped


def refresh_invariant_health(root) -> str | None:
    "Re-run the invariant registry's health probe so a broken rule reaches the\n    verdict file (and the per-turn notice) within the hour."
    script = Path(root) / "global" / "scripts" / "invariant-check.py"
    py = Path(root) / "server" / ".venv" / "bin" / "python"
    if not script.is_file():
        return None
    exe = str(py) if py.is_file() else "python3"
    try:
        p = subprocess.run([exe, str(script), "--health"], capture_output=True, text=True,
                           timeout=240, cwd=str(root), env=_task_env(root))
    except (OSError, subprocess.SubprocessError) as e:
        return f"invariant health probe failed to run: {e}"
    return "refreshed" if p.returncode in (0, 1) else f"exit {p.returncode}"









DAILY_SECS = 86400.0
EVAL_GAP_DAYS = 1
EVAL_GAP_MIN = 5
FOOTPRINT_GROWTH = 1.15
_DAILY_AT = 0.0


def _python(root):
    py = Path(root) / "server" / ".venv" / "bin" / "python"
    return str(py) if py.is_file() else "python3"


def _task_env(root):
    'The environment for a store task the janitor runs itself: marked as running inside\n    the daemon, so the script does its work here and does not forward back over MCP\n    (policy).'
    from .store_tasks import SERVER_TASK_ENV
    return dict(os.environ, **{SERVER_TASK_ENV: "1", "AGENT_CONTEXT_STORE": str(root)})


def _open_observation_with(store, marker):
    from . import audit
    for o in audit.list_audit_observations(store, status="open"):
        if marker in (o.get("observation") or ""):
            return o
    return None


def file_once(store, marker, text, evidence, severity="normal") -> str:
    'File an observation carrying `marker` unless one is already open.'
    from . import audit
    if _open_observation_with(store, marker):
        return "already open"
    res = audit.add_audit_observation(store, text, scope="universal", evidence=evidence,
                                      severity=severity)
    if res.get("id"):
        log.warning("agent-context janitor: filed observation #%s: %s", res["id"], marker)
        return f"filed #{res['id']}"
    return f"not filed: {(res.get('error') or '?')[:120]}"


def _run_candidates(root, days, min_firings) -> dict | None:
    script = Path(root) / "global" / "scripts" / "eval-case-candidates.py"
    if not script.is_file():
        return None
    p = subprocess.run([_python(root), str(script), "--days", str(days), "--min",
                        str(min_firings), "--json"], capture_output=True, text=True,
                       timeout=300, cwd=str(root))
    if p.returncode != 0:
        raise RuntimeError(f"eval-case-candidates exit {p.returncode}: "
                           f"{(p.stderr or '')[-200:].strip()}")
    return json.loads(p.stdout or "{}")


def sweep_eval_gaps(root, store, days=EVAL_GAP_DAYS, min_firings=EVAL_GAP_MIN) -> dict | None:
    'Refusals become cases. A guard that fired `min_firings` times in `days` with\n    no eval case covering it is filed as an observation naming the case to write.'
    if store is None:
        return None
    doc = _run_candidates(root, days, min_firings)
    if not doc or doc.get("no_data"):
        return {"no_data": True}
    import socket
    host = socket.gethostname().split(".")[0]
    out = {}
    for name, n in (doc.get("gaps") or {}).items():
        marker = f"[janitor] eval gap: {name}"
        text = (f"{marker} -- this guard refused {n} time(s) on {host} in the last "
                f"{days} day(s) and no eval case covers it. A rule the fleet breaks this "
                f"often has a known-correct outcome and nothing measuring whether a scaffold "
                f"change improves it. Write a case in eval-cases.py with covers=[\"{name}\"], "
                f"or exempt it in eval-case-candidates.py with the reason. Filed by the "
                f"daemon's daily janitor; one open record per guard.")
        out[name] = file_once(store, marker, text,
                              evidence=f"eval-case-candidates.py --days {days} --min "
                                       f"{min_firings} --json on {host}: {name} = {n}")
    return out


def watch_bootstrap_footprint(root, store, growth=FOOTPRINT_GROWTH) -> dict | None:
    "The cost every session pays, watched daily: over the ceiling, or grown by\n    `growth` since yesterday, is an observation. Yesterday's figure lives in the\n    state dir, machine-local, because the reading is of this machine's index."
    if store is None:
        return None
    from . import integrity, paths
    f = integrity.check_integrity(store)
    fp = f.get("bootstrap_footprint") or {}
    
    g = (int(((fp.get("global") or {}).get("est_bootstrap_bytes")) or 0)
         + int(f.get("audit_block_bytes") or 0))
    ceiling = int(f.get("bootstrap_ceiling") or 0)
    over = list(f.get("over_ceiling_sessions") or [])
    stamp = paths.state_dir() / "janitor-footprint.json"
    prev = None
    with contextlib.suppress(OSError, ValueError):
        prev = json.loads(stamp.read_text(encoding="utf-8"))
    with contextlib.suppress(OSError):
        stamp.write_text(json.dumps({"global": g, "at": int(time.time())}))
    res = {"global_bytes": g, "over": over}
    if over:
        res["filed"] = file_once(
            store, "[janitor] bootstrap over ceiling",
            f"[janitor] bootstrap over ceiling -- get_session_context now exceeds "
            f"{ceiling} chars for scope(s) {', '.join(over)} (global alone {g}). Every "
            f"session on every machine pays this before its first word. Tier memories "
            f"to lazy (upsert_memory(slug, load_behavior='lazy')) or shorten descriptions; "
            f"check_integrity lists prune_candidates. Filed by the daily janitor.",
            evidence=f"check_integrity: bootstrap_footprint global={g}, ceiling={ceiling}, "
                     f"over_ceiling_sessions={over}", severity="high")
    elif prev and prev.get("global") and g > int(prev["global"]) * growth:
        res["filed"] = file_once(
            store, "[janitor] bootstrap footprint grew",
            f"[janitor] bootstrap footprint grew -- the global bootstrap went from "
            f"{prev['global']} to {g} chars in a day ({g / prev['global']:.0%} of "
            f"yesterday). Under the {ceiling} ceiling, but this is the leading indicator: "
            f"find what was added (get_usage_report with report='tokens') and decide whether it earns its "
            f"place in every session. Filed by the daily janitor.",
            evidence=f"janitor-footprint.json yesterday={prev['global']} today={g}")
    return res


def sweep_store_compact(root) -> dict | None:
    'sweep store compact.'
    script = Path(root) / "global" / "scripts" / "store-compact.py"
    if not script.is_file():
        return None
    p = subprocess.run([_python(root), str(script), "--apply", "--json"], capture_output=True,
                       text=True, timeout=300, cwd=str(root), env=_task_env(root))
    if p.returncode != 0:
        raise RuntimeError(f"store-compact exit {p.returncode}: "
                           f"{(p.stderr or '')[-200:].strip()}")
    archived = json.loads(p.stdout or "{}").get("archived") or {}
    if archived.get("observations") or archived.get("handoffs"):
        log.warning("agent-context janitor: archived %s observation(s), %s handoff(s)",
                    archived.get("observations", 0), archived.get("handoffs", 0))
    return archived


def _recording(root, store, chore):
    'Run `chore`, then record every store file it changed as a daemon write, so the\n    commit on write takes it (policy); a file changed outside the ledger is never\n    committed.'
    from . import store_tasks
    before = store_tasks._dirty(str(root))
    try:
        return chore()
    finally:
        after = store_tasks._dirty(str(root))
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        if changed and store is not None:
            store.record_task_writes(changed)


def refresh_root_pages(root, store, archived) -> dict | None:
    'Regenerate the root pages (entity-root-pages.py) after the archive sweep moved a\n    handoff. A root page links every Markdown file in its scope by path, so each moved\n    handoff leaves a dangling link there. Writes only the pages whose body changed.'
    if store is None or not isinstance(archived, dict) or not archived.get("handoffs"):
        return None
    script = Path(root) / "global" / "scripts" / "entity-root-pages.py"
    if not script.is_file():
        return None
    from . import docs, paths
    state = paths.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    written = []
    with tempfile.TemporaryDirectory(dir=str(state)) as out:
        p = subprocess.run([_python(root), str(script), out, str(root)], capture_output=True,
                           text=True, timeout=300, cwd=str(root), env=_task_env(root))
        if p.returncode != 0:
            raise RuntimeError(f"entity-root-pages exit {p.returncode}: "
                               f"{(p.stderr or '')[-200:].strip()}")
        for page in json.loads(p.stdout or "[]"):
            project, workspace = page.get("project"), page.get("workspace")
            body = Path(page["body_path"]).read_text(encoding="utf-8")
            cur = store.get("doc", page["path"], project,
                            scope=store.scope_for_read(project, workspace))
            if cur and (cur.get("body") or "").strip() == body.strip():
                continue
            res = docs.upsert_doc(store, page["path"], body=body, project=project,
                                  workspace=workspace, title=page.get("title"), origin="agent")
            if res.get("error"):
                raise RuntimeError(f"root page {page['path']}: {res['error']}")
            written.append(page["path"])
    if written:
        log.warning("agent-context janitor: regenerated %d root page(s): %s",
                    len(written), ", ".join(written))
    return {"written": written}


def _daily_due(now) -> bool:
    'Once a day per machine, stamped before running (a stamp file, so a daemon\n    restart does not re-run it; file_once dedups the observations regardless).'
    from . import paths
    stamp = paths.state_dir() / "janitor-daily"
    with contextlib.suppress(OSError):
        if now - stamp.stat().st_mtime < DAILY_SECS:
            return False
    with contextlib.suppress(OSError):
        stamp.touch()
        os.utime(stamp, (now, now))
    return True


def _own_uuid() -> str | None:
    with contextlib.suppress(Exception):
        from .machine import get_machine_uuid
        return get_machine_uuid()
    return None


def is_fleet_filer(root, machine_uuid=None, now=None) -> bool:
    'Does this machine file the janitor\'s fleet-wide observations?\n\n    The eval-gap and footprint checks read fleet-wide data, so every machine reaches the\n    same verdict, and file_once\'s "already open" test only sees records that have synced.\n    One filer avoids duplicates: the lowest machine_uuid among the fleet rows that are\n    not stale. Every machine computes that from the same synced rows, so no lock has to\n    travel.\n\n    When every row is stale (a fleet-wide publish failure), the most recently updated\n    machine files, lowest uuid breaking a tie: returning True there would make every\n    machine the filer again. With no row at all, or no\n    identity for this machine, it files: a missing filing is silent forever, and a\n    duplicate is still caught by file_once once synced. Uuids compare case-folded,\n    because macOS spells them upper case and Linux lower.'
    from . import fleet
    try:
        rows = fleet.read_all(root, now=now, include_remote=False)
    except Exception:                   
        return True
    if not rows:
        return True

    def key(r):
        return str(r.get("machine_uuid")).casefold()

    fresh = [r for r in rows if not r.get("stale")]
    if fresh:
        pick = min(fresh, key=key)
    else:
        pick = min(rows, key=lambda r: (-(r.get("updated_at") or 0), key(r)))
    me = machine_uuid or _own_uuid()
    return me is None or key(pick) == str(me).casefold()


def sweep(root, now=None, store=None, machine_uuid=None) -> dict:
    t = time.time() if now is None else now
    out = {}
    jobs = [("worktrees", lambda: sweep_worktrees(root, t)),
            ("branches", lambda: sweep_branches(root, t)),
            ("litter", lambda: sweep_litter(root, t)),
            ("fleet_refs", lambda: sweep_fleet_refs(root)),
            ("invariants", lambda: refresh_invariant_health(root))]
    
    
    
    filer = store is not None and is_fleet_filer(root, machine_uuid, now=t)
    if filer and _daily_due(t):
        jobs += [("eval_gaps", lambda: sweep_eval_gaps(root, store)),
                 ("footprint", lambda: watch_bootstrap_footprint(root, store)),
                 ("store_compact", lambda: _recording(root, store, lambda: sweep_store_compact(root))),
                 ("root_pages", lambda: refresh_root_pages(root, store, out.get("store_compact")))]
    for name, fn in jobs:
        try:
            out[name] = fn()
        except Exception as e:          
            out[name] = f"failed: {e}"
            log.warning("agent-context janitor: %s sweep failed: %s", name, e)
    return out


def maybe_sweep(root, now=None, every=SWEEP_SECS, store=None) -> dict | None:
    'Hourly, from the sync loop. Stamped before running so a sweep that raises\n    every time cannot turn into one that runs every cycle.'
    global _SWEPT_AT
    t = time.time() if now is None else now
    if _SWEPT_AT and (t - _SWEPT_AT) < every:
        return None
    _SWEPT_AT = t
    return sweep(root, t, store=store)
