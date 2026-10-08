"Tests for the ``project-memory-verify-autorun`` SessionStart hook — the launcher\nfor the per-project background memory-BODY verifier.\n\nThis is the highest-risk autonomous machinery in the store (the launched agent edits\nmemory bodies against the code), so its launch gates must be airtight. Driven through\nthe hook's test seams (``PROJECT_MEMORY_VERIFY_CLAUDE`` fake bin,\n``PROJECT_MEMORY_VERIFY_NOLAUNCH``) in a fully isolated ``$HOME`` — no real headless\n``claude`` spawned, live store untouched. Covers: kill-switch OFF, recursion guard, the\nin-a-project gate (must be inside a ``.agents/`` tree), overdue + per-project lock,\ncooldown, not-overdue, the DAYS override, and the missing-HOME fail-safe."
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "global" / "hooks" / "project-memory-verify-autorun.py"

pytestmark = pytest.mark.skipif(
    not HOOK.is_file(), reason="hook test needs the hook script present"
)


def _world(base, *, enabled=True):
    'Isolated HOME + a project dir (with .agents/) + a non-project dir.'
    base.mkdir(parents=True, exist_ok=True)
    home = base / "home"
    (home / ".local" / "state" / "agent-context" / "mem-verify").mkdir(parents=True)
    if enabled:
        (home / ".local" / "state" / "agent-context" / "mem-verify" / "enabled").write_text("")
    proj = base / "proj"
    (proj / ".agents" / "sub").mkdir(parents=True)   
    nonproj = base / "plain"
    nonproj.mkdir()
    return home, proj, nonproj


def _run(home, cwd, *, extra_env=None, nolaunch=True, drop_home=False):
    env = {"PATH": os.environ["PATH"],
           "PROJECT_MEMORY_VERIFY_CLAUDE": sys.executable,  
           
           
           "PROJECT_MEMORY_VERIFY_CEILING": str(home.parent)}
    if not drop_home:
        env["HOME"] = str(home)
    if nolaunch:
        env["PROJECT_MEMORY_VERIFY_NOLAUNCH"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"cwd": str(cwd)}),
                          env=env, capture_output=True, text=True, timeout=30)


def _launched(p):
    return "NOLAUNCH would run" in p.stderr


def _state(home):
    files = list((home / ".local" / "state" / "agent-context" / "mem-verify").glob("proj-*.json"))
    return json.loads(files[0].read_text()) if files else None



def test_in_project_and_overdue_launches_in_project_root(tmp_path):
    home, proj, _ = _world(tmp_path)
    p = _run(home, proj / ".agents" / "sub")
    assert p.returncode == 0
    assert '"suppressOutput": true' in p.stdout
    assert _launched(p)
    assert str(proj) in p.stderr                 
    assert _state(home)["running_since"] is not None  


def test_not_inside_a_project_does_not_launch(tmp_path):
    home, _, nonproj = _world(tmp_path)
    p = _run(home, nonproj)                        
    assert not _launched(p)
    assert _state(home) is None                    


def test_home_own_agents_dir_is_not_a_project(tmp_path):
    
    
    home, _, _ = _world(tmp_path)
    (home / ".agents" / "tmp").mkdir(parents=True)
    work = home / "Developer" / "scratch"
    work.mkdir(parents=True)
    p = _run(home, work)
    assert not _launched(p)
    assert _state(home) is None


def test_kill_switch_off_never_launches(tmp_path):
    home, proj, _ = _world(tmp_path, enabled=False)
    assert not _launched(_run(home, proj / ".agents" / "sub"))


def test_recursion_guard_blocks(tmp_path):
    home, proj, _ = _world(tmp_path)
    p = _run(home, proj / ".agents" / "sub", extra_env={"PROJECT_MEMORY_VERIFY_RUN": "1"})
    assert not _launched(p)


def test_cooldown_blocks_re_fire(tmp_path):
    home, proj, _ = _world(tmp_path)
    assert _launched(_run(home, proj / ".agents" / "sub"))     
    assert not _launched(_run(home, proj / ".agents" / "sub")) 


def test_recent_run_is_not_overdue(tmp_path):
    home, proj, _ = _world(tmp_path)
    
    st = home / ".local" / "state" / "agent-context" / "mem-verify" / "proj-proj.json"
    st.write_text(json.dumps({"last_run": int(time.time()), "running_since": None, "running_host": None}))
    assert not _launched(_run(home, proj / ".agents" / "sub"))


def test_days_override_controls_overdue(tmp_path):
    home, proj, _ = _world(tmp_path)
    st = home / ".local" / "state" / "agent-context" / "mem-verify" / "proj-proj.json"
    two_days = int(time.time()) - 2 * 86400
    st.write_text(json.dumps({"last_run": two_days, "running_since": None, "running_host": None}))
    
    assert not _launched(_run(home, proj / ".agents" / "sub"))
    
    st.write_text(json.dumps({"last_run": two_days, "running_since": None, "running_host": None}))
    (home / ".local" / "state" / "agent-context" / "mem-verify" / "cool-proj").unlink(missing_ok=True)
    assert _launched(_run(home, proj / ".agents" / "sub", extra_env={"PROJECT_MEMORY_VERIFY_DAYS": "1"}))


def test_missing_home_is_noop(tmp_path):
    home, proj, _ = _world(tmp_path)
    p = _run(home, proj / ".agents" / "sub", drop_home=True)
    assert p.returncode == 0
    assert not _launched(p)
