'test janitor and parking.'
import os
import subprocess
import time

from fixture_signing import signing_config

from agent_context import janitor
from agent_context.store import ContextStore


def _git(cwd, *args, check=True):
    return subprocess.run(("git", "-C", str(cwd), *args),
                          capture_output=True, text=True, check=check)


def _repo(tmp_path, name="store"):
    r = tmp_path / name
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    for k, v in signing_config(tmp_path):
        _git(r, "config", k, v)
    (r / "global").mkdir()
    (r / "global" / ".keep").write_text("")
    (r / "server").mkdir()
    (r / "server" / "app.py").write_text("v1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "seed")
    return r


def _age(path, secs):
    old = time.time() - secs
    os.utime(path, (old, old))




def test_merged_idle_worktree_and_branch_are_removed_but_everything_else_stays(tmp_path):
    r = _repo(tmp_path)
    now = time.time()
    
    _git(r, "worktree", "add", "-q", str(r / ".claude/worktrees/done"), "-b", "done")
    for dp, _dn, fns in os.walk(r / ".claude/worktrees/done"):
        for f in fns:
            _age(os.path.join(dp, f), 2 * 86400)
    
    _git(r, "worktree", "add", "-q", str(r / ".claude/worktrees/dirty"), "-b", "dirty")
    (r / ".claude/worktrees/dirty/server/app.py").write_text("edited\n")
    
    _git(r, "worktree", "add", "-q", str(r / ".claude/worktrees/wip"), "-b", "wip")
    (r / ".claude/worktrees/wip/server/new.py").write_text("x\n")
    _git(r / ".claude/worktrees/wip", "add", "-A")
    _git(r / ".claude/worktrees/wip", "commit", "-qm", "wip")
    
    _git(r, "worktree", "add", "-q", str(r / ".claude/worktrees/parked-x"), "-b", "parked/x")
    
    _git(r, "worktree", "add", "-q", str(tmp_path / "gone"), "-b", "gone")
    subprocess.run(["rm", "-rf", str(tmp_path / "gone")], check=True)
    
    
    _git(r, "branch", "old-merged")

    res = janitor.sweep(str(r), now=now + 3 * 86400)
    wt = {os.path.basename(p) for p, _b in janitor._linked_worktrees(str(r))}
    assert "done" not in wt and "dirty" in wt and "wip" in wt and "parked-x" in wt
    assert "gone" not in wt
    branches = _git(r, "branch", "--format=%(refname:short)").stdout.split()
    assert "done" not in branches
    assert {"dirty", "wip", "parked/x"} <= set(branches)
    assert "old-merged" not in branches
    kept = dict(res["worktrees"]["kept"])
    assert kept[str(r / ".claude/worktrees/dirty")] == "dirty"
    assert kept[str(r / ".claude/worktrees/wip")] == "unmerged"
    assert kept[str(r / ".claude/worktrees/parked-x")] == "parked"


def test_a_recent_merged_branch_is_left_for_its_author(tmp_path):
    r = _repo(tmp_path)
    _git(r, "branch", "just-landed")
    res = janitor.sweep_branches(str(r), time.time())
    assert ("just-landed", "recent") in res["kept"]


def test_write_litter_older_than_an_hour_is_removed(tmp_path):
    r = _repo(tmp_path)
    old = r / "global" / "memory"
    old.mkdir()
    (old / "note.md.tmp").write_text("half")
    _age(old / "note.md.tmp", 7200)
    (old / "fresh.md.tmp").write_text("in flight")
    gone = janitor.sweep_litter(str(r), time.time())
    assert gone == ["global/memory/note.md.tmp"]
    assert (old / "fresh.md.tmp").exists()


def test_fleet_refs_for_unknown_machines_are_dropped_locally(tmp_path):
    r = _repo(tmp_path)
    (r / "machines").mkdir()
    (r / "machines" / "u1.toml").write_text('type = "machine"\nmachine_id = "laptop"\n')
    head = _git(r, "rev-parse", "HEAD").stdout.strip()
    _git(r, "update-ref", "refs/fleet/ls/laptop", head)
    _git(r, "update-ref", "refs/fleet/ls/retired-box", head)
    dropped = janitor.sweep_fleet_refs(str(r))
    assert dropped == ["refs/fleet/ls/retired-box"]
    refs = _git(r, "for-each-ref", "--format=%(refname)", "refs/fleet/").stdout.split()
    assert refs == ["refs/fleet/ls/laptop"]


def test_the_sweep_is_hourly_and_stamped_before_it_runs(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    monkeypatch.setattr(janitor, "_SWEPT_AT", 0.0)
    assert janitor.maybe_sweep(str(r), now=1000.0) is not None
    assert janitor.maybe_sweep(str(r), now=1000.0 + 1800) is None
    assert janitor.maybe_sweep(str(r), now=1000.0 + 3601) is not None




def test_abandoned_server_edits_are_parked_on_a_branch_and_main_is_cleared(tmp_path):
    r = _repo(tmp_path)
    (r / "server" / "app.py").write_text("edited in main, then abandoned\n")
    (r / "server" / "new.py").write_text("untracked too\n")
    _age(r / "server" / "app.py", 3600)
    _age(r / "server" / "new.py", 3600)
    store = ContextStore(str(r))
    
    assert store._park_dirty_server_if_abandoned(store._PARK_AFTER_CYCLES - 1) is None
    parked = store._park_dirty_server_if_abandoned(store._PARK_AFTER_CYCLES)
    assert parked and parked["branch"].startswith("parked/")
    assert sorted(parked["files"]) == ["server/app.py", "server/new.py"]
    
    assert _git(r, "status", "--porcelain", "--", "server").stdout.strip() == ""
    assert (r / "server" / "app.py").read_text() == "v1\n"
    assert not (r / "server" / "new.py").exists()
    
    assert _git(r, "show", f"{parked['branch']}:server/app.py").stdout == \
        "edited in main, then abandoned\n"
    assert _git(r, "show", f"{parked['branch']}:server/new.py").stdout == "untracked too\n"
    
    kept = dict(janitor.sweep_branches(str(r), time.time() + 10 * 86400)["kept"])
    assert kept[parked["branch"]] == "parked"


def test_edits_someone_is_still_touching_are_left_alone(tmp_path):
    r = _repo(tmp_path)
    (r / "server" / "app.py").write_text("being edited right now\n")
    store = ContextStore(str(r))
    assert store._park_dirty_server_if_abandoned(99) is None
    assert (r / "server" / "app.py").read_text() == "being edited right now\n"
