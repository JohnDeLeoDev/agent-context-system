'Tests for the ``context-audit-autorun`` SessionStart hook — the launcher for\nthe autonomous closed-loop context-audit (roadmap T4.2).\n\nThe hook is *powerful autonomous machinery* guarded by four independent gates,\nany one of which must stop it. It ships with explicit test seams\n(``AGENT_CONTEXT_STORE``, ``AGENT_CONTEXT_AUTO_AUDIT_CLAUDE``,\n``AGENT_CONTEXT_AUTO_AUDIT_NOLAUNCH``) so its decision logic can be exercised\nwithout ever spawning a real headless ``claude`` or touching the live store — but\nuntil now nothing exercised them. These tests drive the hook in a fully\nisolated ``$HOME`` + store under ``tmp_path`` and assert on the two observable\neffects: whether it *decided to launch* (NOLAUNCH prints the command to stderr)\nand whether it *claimed the distributed lock* (mutated the shared state file).\n\nEvery gate is covered:\n  * Gate 0  — missing ``$HOME``            -> no-op\n  * Gate 1  — kill-switch marker absent    -> no-op (default OFF)\n  * Gate 2  — recursion guard env set      -> no-op\n  * Gate 3  — local cooldown fresh         -> no-op\n  * Gate 4a — not overdue                  -> no-op\n  * Gate 4b — fresh remote lock held       -> no-op (lock untouched)\nplus the happy path (overdue + enabled -> launch + claim + cooldown touch), stale\nlock reclaim, the missing-state-file dependency bail, and the DAYS override.'
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "global" / "hooks" / "context-audit-autorun.py"

pytestmark = pytest.mark.skipif(
    not HOOK.is_file(), reason="hook test needs the hook script present"
)





def _fake_claude(tmp_path):
    'A stand-in for the `claude` binary that the hook\'s `command -v` dependency\n    check must find. It is NEVER executed (every test sets NOLAUNCH), so any real\n    executable works — and we deliberately use one OUTSIDE the test tmp dir. A fake\n    script placed under `tmp_path` breaks on nodes that mount /tmp `noexec` (pytest\'s\n    tmp_path lives under /tmp there): `command -v` applies an executability check that\n    fails on a noexec mount, so the hook bails before launching and the "should-launch"\n    tests fail spuriously — which, run as the self-deploy gate, would wrongly block a\n    node from updating. `sys.executable` is always present, always executable, and never\n    under /tmp. (tmp_path kept for call-site compatibility.)'
    return sys.executable


def _make_world(base, *, enabled=True, last_run=0, running_since=None, running_host=None):
    'Build an isolated HOME + store. Returns (home, store).'
    base.mkdir(parents=True, exist_ok=True)
    home = base / "home"
    audit = home / ".local" / "state" / "agent-context" / "audit"
    audit.mkdir(parents=True)
    if enabled:
        (audit / "auto-audit-enabled").write_text("")
    store = base / "store"
    (store / "global" / "state").mkdir(parents=True)
    state = {"last_run": last_run, "running_since": running_since, "running_host": running_host}
    (store / "global" / "state" / "context-audit.json").write_text(json.dumps(state))
    return home, store


def _run(home, store, fake_claude, *, extra_env=None, nolaunch=True, drop_home=False):
    env = {
        "PATH": os.environ["PATH"],
        "AGENT_CONTEXT_STORE": str(store),
        "AGENT_CONTEXT_AUTO_AUDIT_CLAUDE": str(fake_claude),
    }
    if not drop_home:
        env["HOME"] = str(home)
    if nolaunch:
        env["AGENT_CONTEXT_AUTO_AUDIT_NOLAUNCH"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK)], env=env, capture_output=True, text=True, timeout=30
    )


def _state(store):
    return json.loads((store / "global" / "state" / "context-audit.json").read_text())


def _launched(proc):
    'True iff the hook decided to spawn (NOLAUNCH prints the command to stderr).'
    return "NOLAUNCH would run" in proc.stderr


def _cooldown(home):
    return home / ".local" / "state" / "agent-context" / "audit" / "auto-last-launch"





def test_overdue_and_enabled_launches_claims_lock_touches_cooldown(tmp_path):
    home, store = _make_world(tmp_path, last_run=0)  
    proc = _run(home, store, _fake_claude(tmp_path))

    assert proc.returncode == 0
    assert '"suppressOutput": true' in proc.stdout  
    assert _launched(proc)

    st = _state(store)
    assert st["running_since"] is not None          
    assert st["running_host"]                        
    assert _cooldown(home).exists()                  





def test_kill_switch_off_never_launches(tmp_path):
    home, store = _make_world(tmp_path, enabled=False, last_run=0)
    proc = _run(home, store, _fake_claude(tmp_path))

    assert proc.returncode == 0
    assert not _launched(proc)
    assert _state(store)["running_since"] is None    





def test_recursion_guard_blocks_when_run_flag_set(tmp_path):
    home, store = _make_world(tmp_path, last_run=0)
    proc = _run(
        home, store, _fake_claude(tmp_path),
        extra_env={"AGENT_CONTEXT_AUTO_AUDIT_RUN": "1"},
    )
    assert not _launched(proc)
    assert _state(store)["running_since"] is None





def test_local_cooldown_blocks_recent_launch(tmp_path):
    home, store = _make_world(tmp_path, last_run=0)
    _cooldown(home).write_text("")  
    proc = _run(home, store, _fake_claude(tmp_path))

    assert not _launched(proc)
    assert _state(store)["running_since"] is None





def test_recent_run_is_not_overdue(tmp_path):
    home, store = _make_world(tmp_path, last_run=int(time.time()))
    proc = _run(home, store, _fake_claude(tmp_path))

    assert not _launched(proc)
    assert _state(store)["running_since"] is None





def test_fresh_remote_lock_blocks_and_is_untouched(tmp_path):
    home, store = _make_world(
        tmp_path, last_run=0, running_since=int(time.time()), running_host="other-node"
    )
    proc = _run(home, store, _fake_claude(tmp_path))

    assert not _launched(proc)
    assert _state(store)["running_host"] == "other-node"  


def test_stale_lock_is_reclaimed(tmp_path):
    old = int(time.time()) - 4000  
    home, store = _make_world(
        tmp_path, last_run=0, running_since=old, running_host="dead-node"
    )
    proc = _run(home, store, _fake_claude(tmp_path))

    assert _launched(proc)
    assert _state(store)["running_host"] != "dead-node"   





def test_missing_home_is_noop(tmp_path):
    home, store = _make_world(tmp_path, last_run=0)
    proc = _run(home, store, _fake_claude(tmp_path), drop_home=True)

    assert proc.returncode == 0
    assert not _launched(proc)
    assert _state(store)["running_since"] is None


def test_missing_state_file_is_noop(tmp_path):
    home, store = _make_world(tmp_path, last_run=0)
    (store / "global" / "state" / "context-audit.json").unlink()
    proc = _run(home, store, _fake_claude(tmp_path))

    assert proc.returncode == 0
    assert not _launched(proc)





def test_days_override_controls_overdue_window(tmp_path):
    two_days_ago = int(time.time()) - 2 * 86400
    fc = _fake_claude(tmp_path)

    
    home_a, store_a = _make_world(tmp_path / "a", last_run=two_days_ago)
    assert not _launched(_run(home_a, store_a, fc))

    
    home_b, store_b = _make_world(tmp_path / "b", last_run=two_days_ago)
    proc = _run(home_b, store_b, fc, extra_env={"AGENT_CONTEXT_AUTO_AUDIT_DAYS": "1"})
    assert _launched(proc)
