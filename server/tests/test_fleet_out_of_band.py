"Fleet status travels out of band, and a hook can wake the sync loop.\n\nSo: (1) each daemon force-pushes its row as a one-file signed commit to\nrefs/fleet/<machine_id> on every remote, fetched back as refs/fleet/<remote>/*, and\nread_all prefers the newer of the two sources; (2) a hook touches a poke file and the\nloop's sliced sleep returns early."
import subprocess

from fixture_signing import signing_config_beside

from agent_context import daemon, fleet, machine
from agent_context.store import ContextStore


def _git(cwd, *args, check=True):
    return subprocess.run(("git", "-C", str(cwd), *args),
                          capture_output=True, text=True, check=check)


def _identify(root):
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for k, v in signing_config_beside(root):
        _git(root, "config", k, v)


def _fleet(tmp_path):
    up, a, b = tmp_path / "up", tmp_path / "a", tmp_path / "b"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(up))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _identify(seed)
    (seed / "global").mkdir()
    (seed / "global" / ".keep").write_text("")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "remote", "add", "origin", str(up))
    _git(seed, "push", "-q", "origin", "main")
    for r in (a, b):
        _git(tmp_path, "clone", "-q", str(up), str(r))
        _identify(r)
    return a, b


def test_row_reaches_the_fleet_without_touching_main(tmp_path, monkeypatch):
    a, b = _fleet(tmp_path)
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "uuid-a")
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: "m-a")
    sa = ContextStore(str(a))
    
    
    

    fleet.publish(str(a), "uuid-a", {"verdict": "sync-broken", "last_sync_error": "merge conflicted",
                                     "code_current": True},
                  machine_id="m-a", hostname="a")
    assert sa._publish_fleet_ref() == ["origin"]
    
    assert _git(a, "status", "--porcelain").stdout.strip() != ""     
    assert _git(tmp_path / "up", "rev-parse", "refs/fleet/m-a").returncode == 0

    
    
    sb = ContextStore(str(b))
    sb._ensure_fleet_refspecs()
    _git(b, "fetch", "-q", "--all")
    rows = {r["machine_id"]: r for r in fleet.read_all(str(b))}
    assert rows["m-a"]["verdict"] == "sync-broken"
    assert rows["m-a"]["via_ref"] == "refs/fleet/origin/m-a"
    probs = fleet.problems(fleet.read_all(str(b)))
    assert any("m-a" in p and "sync-broken" in p for p in probs)

    
    assert sa._publish_fleet_ref() == []


def test_the_newer_source_wins_and_a_tie_goes_to_the_ref(tmp_path, monkeypatch):
    a, b = _fleet(tmp_path)
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "uuid-a")
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: "m-a")
    sa = ContextStore(str(a))
    
    fleet.publish(str(b), "uuid-a", {"verdict": "healthy", "code_current": True},
                  machine_id="m-a", hostname="a", now=1_000_000)
    
    fleet.publish(str(a), "uuid-a", {"verdict": "sync-broken", "code_current": True},
                  machine_id="m-a", hostname="a", now=2_000_000)
    sa._publish_fleet_ref()
    ContextStore(str(b))._ensure_fleet_refspecs()
    _git(b, "fetch", "-q", "--all")
    rows = {r["machine_id"]: r for r in fleet.read_all(str(b))}
    assert rows["m-a"]["verdict"] == "sync-broken"

    
    fleet.publish(str(b), "uuid-a", {"verdict": "healthy", "code_current": True},
                  machine_id="m-a", hostname="a", now=3_000_000)
    rows = {r["machine_id"]: r for r in fleet.read_all(str(b))}
    assert rows["m-a"]["verdict"] == "healthy"


def test_a_poke_wakes_the_sleep_early(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_state_dir", lambda: tmp_path)
    slept = []
    poke = tmp_path / "sync-requested"
    poke.write_text("")
    assert daemon.sleep_until_poked(1200, slice_secs=15, _sleep=slept.append) is True
    assert slept == [15]                     
    assert not poke.exists()                 
    assert daemon.sleep_until_poked(40, slice_secs=15, _sleep=slept.append) is False
    assert slept[1:] == [15, 15, 10]         


def test_the_fleet_commit_is_signed_exactly_when_the_repo_signs(tmp_path, monkeypatch):
    'test the fleet commit is signed exactly when the repo signs.'
    a, _ = _fleet(tmp_path)
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: "uuid-a")
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: "m-a")
    fleet.publish(str(a), "uuid-a", {"verdict": "healthy", "code_current": True},
                  machine_id="m-a", hostname="a")
    sa = ContextStore(str(a))
    seen = []
    orig = sa._git

    def spy(*args, **kw):
        if args and args[0] == "commit-tree":
            seen.append(args)
            
            args = tuple(x for x in args if x != "-S")
        return orig(*args, **kw)
    monkeypatch.setattr(sa, "_git", spy)

    _git(a, "config", "commit.gpgsign", "false")
    sa._publish_fleet_ref()
    assert seen and "-S" not in seen[-1]

    _git(a, "config", "commit.gpgsign", "true")
    fleet.publish(str(a), "uuid-a", {"verdict": "sync-broken", "code_current": True},
                  machine_id="m-a", hostname="a")
    sa._publish_fleet_ref()
    assert "-S" in seen[-1]
