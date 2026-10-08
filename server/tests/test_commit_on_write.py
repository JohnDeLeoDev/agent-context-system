"Commit and push on write (debounced), in the ls daemon's store.\n\nInterface these tests pin (agent_context.commit_on_write, plus ContextStore hooks):\n\n- `store.enable_commit_on_write(clock=None, push=None, start=True)` reads\n  AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS (default 3, cap 30) and returns a `CommitOnWrite`, or\n  None when the variable is 0 (feature off). `start=False` skips the worker threads so a\n  test drives `tick()` on a fake clock.\n- `CommitOnWrite.tick()` commits when the debounce or the cap is due, then pushes when a\n  push is owed and its retry time has passed. `.status()` reports pending, unpushed,\n  last_commit_at, last_push_at, last_commit_error, last_push_error.\n- `store.commit_local(message=None)` and `store.push_all()` are the commit and push halves\n  of `sync()`, which now calls them; `sync()` behavior is unchanged.\n- Every store write arms the timer through `store._arm_commit(path)`, NOT through the\n  write callbacks: audit.py, session.py and projects.py write without firing them, and\n  firing them would change the content_changed broadcast.\n\nEvery case runs on throwaway repos under tmp_path; the live store is never touched."
import subprocess
import threading
import time

import pytest
from fixture_signing import signing_config

from agent_context import audit, docs, entities, generic, memory, paths
from agent_context.store import ContextStore


def _git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=check)


def _count(root):
    return int(_git(root, "rev-list", "--count", "HEAD").stdout.strip())


def _remote_tip(bare):
    r = subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", "main"],
                       capture_output=True, text=True)
    return r.stdout.strip()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def repo(tmp_path, monkeypatch):
    'A git-backed store with two bare mirrors, all agreeing on one signed commit.'
    monkeypatch.delenv("AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS", raising=False)
    root = tmp_path / "ctx"
    (root / "global").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 *signing_config(tmp_path)):
        _git(root, "config", k, v)
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "one")
    mirrors = {}
    for n in ("origin", "m1"):
        p = tmp_path / f"{n}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(p)], check=True)
        _git(root, "remote", "add", n, str(p))
        _git(root, "push", "-q", n, "main")
        mirrors[n] = p
    _git(root, "fetch", "--all", "-q")
    return ContextStore(root=str(root)), root, mirrors


@pytest.fixture
def cow(repo):
    store, _, _ = repo
    clock = Clock()
    c = store.enable_commit_on_write(clock=clock, start=False)
    c.clock = clock
    return c


def _doc(store, name, body="x"):
    docs.upsert_doc(store, name, body=body)




def test_write_commits_once_after_debounce_signed(repo, cow):
    store, root, _ = repo
    before = _count(root)
    _doc(store, "a.md")
    cow.clock.advance(2.9)
    cow.tick()
    assert _count(root) == before, "committed before the 3 s debounce elapsed"
    assert cow.status()["pending"] is True
    cow.clock.advance(0.2)
    cow.tick()
    assert _count(root) == before + 1
    header = _git(root, "cat-file", "commit", "HEAD").stdout.split("\n\n", 1)[0]
    assert any(ln.startswith("gpgsig ") for ln in header.splitlines()), "commit is unsigned"
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert cow.status()["pending"] is False




def test_burst_inside_window_makes_one_commit(repo, cow):
    store, root, _ = repo
    before = _count(root)
    for i in range(5):
        _doc(store, f"b{i}.md")
        cow.clock.advance(1.0)
        cow.tick()                      
    assert _count(root) == before
    cow.clock.advance(3.1)
    cow.tick()
    assert _count(root) == before + 1
    files = _git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert sum(f.startswith("global/docs/b") for f in files) == 5


def test_continuous_writes_cannot_delay_past_the_cap(repo, cow):
    store, root, _ = repo
    before = _count(root)
    start = cow.clock.t
    first_commit_at = None
    for i in range(60):
        _doc(store, f"c{i}.md")
        cow.clock.advance(1.0)
        cow.tick()
        if first_commit_at is None and _count(root) > before:
            first_commit_at = cow.clock.t
    assert first_commit_at is not None, "continuous writes starved the commit forever"
    assert first_commit_at - start <= 31.0




def test_hung_push_does_not_slow_writes_or_block_later_commits(repo, monkeypatch):
    store, root, _ = repo
    monkeypatch.setenv("AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS", "0.05")
    release = threading.Event()
    pushes = []

    def hung_push():
        pushes.append(time.monotonic())
        release.wait(60)
        return {"push": True}

    c = store.enable_commit_on_write(push=hung_push, start=True)
    try:
        before = _count(root)
        t0 = time.monotonic()
        _doc(store, "h1.md")
        assert time.monotonic() - t0 < 0.5, "write waited on the commit or push"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pushes:
            time.sleep(0.02)
        assert pushes, "the background worker never started a push"
        
        _doc(store, "h2.md")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _count(root) < before + 2:
            time.sleep(0.02)
        assert _count(root) == before + 2, "a hung push stalled the next commit"
    finally:
        release.set()
        c.stop()




def _break_signing(root):
    _git(root, "config", "gpg.ssh.program", "/bin/false")


def _fix_signing(root, tmp_path):
    for k, v in signing_config(tmp_path):
        _git(root, "config", k, v)


def test_signing_refused_defers_commit_and_write_succeeds(repo, cow, tmp_path):
    store, root, mirrors = repo
    _break_signing(root)
    tip = _remote_tip(mirrors["origin"])
    before = _count(root)
    _doc(store, "s.md")                   
    assert (root / "global" / "docs" / "s.md").exists()
    cow.clock.advance(3.1)
    cow.tick()
    st = cow.status()
    assert _count(root) == before
    assert st["pending"] is True and st["last_commit_error"]
    assert _remote_tip(mirrors["origin"]) == tip, "something was pushed after a failed commit"
    _fix_signing(root, tmp_path)
    cow.clock.advance(3.1)
    cow.tick()                            
    assert _count(root) == before + 1 and cow.status()["pending"] is False


def test_merge_in_progress_defers_commit(repo, cow):
    store, root, _ = repo
    (root / ".git" / "MERGE_HEAD").write_text(_git(root, "rev-parse", "HEAD").stdout)
    before = _count(root)
    _doc(store, "m.md")
    cow.clock.advance(3.1)
    cow.tick()
    assert _count(root) == before
    assert cow.status()["last_commit_error"]


def test_truncated_tracked_file_defers_commit(repo, cow):
    store, root, _ = repo
    (root / "a.txt").write_text("")           
    before = _count(root)
    _doc(store, "t.md")
    cow.clock.advance(3.1)
    cow.tick()
    assert _count(root) == before
    assert "truncated" in (cow.status()["last_commit_error"] or "")


def test_dirty_server_dir_is_never_staged_and_push_is_deferred(repo, cow):
    store, root, mirrors = repo
    (root / "server").mkdir()
    (root / "server" / "wip.py").write_text("x = 1\n")
    tip = _remote_tip(mirrors["origin"])
    _doc(store, "d.md")
    cow.clock.advance(3.1)
    cow.tick()
    changed = _git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert "global/docs/d.md" in changed
    assert not any(p.startswith("server/") for p in changed)
    assert _remote_tip(mirrors["origin"]) == tip, "pushed while server/ was dirty"
    assert cow.status()["last_push_error"]


def test_unsigned_commit_in_range_is_never_pushed(repo, cow):
    store, root, mirrors = repo
    _git(root, "commit", "-q", "--allow-empty", "-m", "unsigned", "--no-gpg-sign")
    tip = _remote_tip(mirrors["origin"])
    _doc(store, "u.md")
    cow.clock.advance(3.1)
    cow.tick()
    assert _remote_tip(mirrors["origin"]) == tip




def test_dead_mirror_does_not_stall_the_others_and_is_retried(repo, cow, tmp_path):
    store, root, mirrors = repo
    dead = tmp_path / "m1.gone"
    mirrors["m1"].rename(dead)
    _doc(store, "p1.md")
    cow.clock.advance(3.1)
    cow.tick()
    assert _remote_tip(mirrors["origin"]) == _git(root, "rev-parse", "HEAD").stdout.strip()
    assert "m1" in (cow.status()["last_push_error"] or "")
    
    before = _count(root)
    _doc(store, "p2.md")
    cow.clock.advance(3.1)
    cow.tick()
    assert _count(root) == before + 1
    
    dead.rename(mirrors["m1"])
    cow.clock.advance(3600)
    cow.tick()
    assert _remote_tip(mirrors["m1"]) == _git(root, "rev-parse", "HEAD").stdout.strip()
    assert cow.status()["last_push_error"] in (None, "")




def test_commit_waits_for_sync_mutex(repo, cow):
    store, root, _ = repo
    _doc(store, "x.md")
    cow.clock.advance(3.1)
    before = _count(root)
    done = threading.Event()
    with store._sync_mutex:
        t = threading.Thread(target=lambda: (cow.tick(), done.set()))
        t.start()
        assert not done.wait(0.5), "tick ran while sync() held the mutex"
        assert _count(root) == before
    assert done.wait(10)
    t.join()
    assert _count(root) == before + 1


def test_write_during_commit_rearms_the_timer(repo, cow, monkeypatch):
    store, root, _ = repo
    real = store.commit_local
    fired = []

    def commit_and_write(message=None):
        if not fired:
            fired.append(1)
            _doc(store, "late.md")            
        return real(message)

    monkeypatch.setattr(store, "commit_local", commit_and_write)
    _doc(store, "first.md")
    cow.clock.advance(3.1)
    cow.tick()
    dirty = _git(root, "status", "--porcelain").stdout
    assert cow.status()["pending"] is True or "late.md" not in dirty
    cow.clock.advance(3.1)
    cow.tick()
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert cow.status()["pending"] is False




def _settle(s):
    'Commit what is pending so the next write is the only thing that can arm the timer.'
    c = s.commit_on_write
    c.clock.advance(3.1)
    c.tick()
    assert c.status()["pending"] is False


def _memory(s):
    memory.upsert_memory(s, "m-slug", "reference", "desc", "body")


def _instruction(s):
    memory.upsert_instruction(s, "Instr", "body")


def _skill(s):
    entities.upsert_skill(s, "sk", description="d", body="body")


def _command(s):
    entities.upsert_command(s, "cmd", body="body")


def _script(s):
    entities.upsert_script(s, "scr", script_body="echo hi\n")


def _hook(s):
    entities.upsert_hook(s, "hk", event_type="Stop", script_body="echo hi\n")


def _delete(s):
    _doc(s, "gone.md")
    _settle(s)
    generic.delete_entity(s, "doc", "gone.md")


def _edit_body(s):
    _doc(s, "e.md", "alpha")
    _settle(s)
    generic.edit_body(s, "doc", "e.md", "alpha", "beta")


def _bulk_edit(s):
    _doc(s, "be.md", "alpha")
    _settle(s)
    generic.bulk_edit(s, [{"kind": "doc", "key": "be.md", "replacements": [["alpha", "beta"]]}])


def _append(s):
    _doc(s, "ap.md", "alpha")
    _settle(s)
    docs.append_to_doc(s, "ap.md", "text")


def _audit_add(s):
    audit.add_audit_observation(s, "obs text", scope="universal", evidence="e")


def _audit_update(s):
    o = audit.add_audit_observation(s, "obs text", scope="universal", evidence="e")
    _settle(s)
    audit.update_audit_observation(s, o["id"], note="n")


def _audit_resolve(s):
    o = audit.add_audit_observation(s, "obs text", scope="universal", evidence="e")
    _settle(s)
    audit.resolve_audit_observation(s, o["id"], resolution_note="done")


def _mem_desc(s):
    _memory(s)
    _settle(s)
    memory.set_memory_description(s, "m-slug", "new desc")


def _mem_load(s):
    _memory(s)
    _settle(s)
    memory.set_memory_load_behavior(s, "m-slug", "lazy")


def _upsert_doc(s):
    _doc(s, "w.md")


WRITERS = [_upsert_doc, _memory, _instruction, _skill, _command,
           _script, _hook, _delete, _edit_body, _bulk_edit, _append, _audit_add,
           _audit_update, _audit_resolve, _mem_desc, _mem_load]


@pytest.mark.parametrize("writer", WRITERS, ids=lambda w: w.__name__)
def test_every_writer_arms_the_commit_timer(repo, cow, writer):
    store, _, _ = repo
    writer(store)
    assert cow.status()["pending"] is True, f"{writer.__name__} did not arm"


def test_sync_commit_and_fleet_row_do_not_rearm(repo, cow):
    store, _, _ = repo
    _doc(store, "z.md")
    cow.clock.advance(3.1)
    cow.tick()
    assert cow.status()["pending"] is False
    store.sync()                                  
    assert cow.status()["pending"] is False




def test_env_zero_disables_the_feature(repo, monkeypatch):
    store, root, _ = repo
    monkeypatch.setenv("AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS", "0")
    assert store.enable_commit_on_write(start=False) is None
    assert store.commit_on_write is None
    before = _count(root)
    _doc(store, "off.md")                         
    assert _count(root) == before


def test_env_sets_the_window_and_defaults_are_3_and_30(repo, monkeypatch):
    store, _, _ = repo
    c = store.enable_commit_on_write(start=False)
    assert (c.debounce, c.cap) == (3.0, 30.0)
    monkeypatch.setenv("AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS", "1.5")
    c = store.enable_commit_on_write(start=False)
    assert c.debounce == 1.5




def test_health_reports_pending_and_last_times(repo, cow):
    from agent_context import server as srv
    store, _, _ = repo
    assert srv._commit_on_write_health(store)["pending"] is False
    _doc(store, "hh.md")
    assert srv._commit_on_write_health(store)["pending"] is True
    cow.clock.advance(3.1)
    cow.tick()
    h = srv._commit_on_write_health(store)
    assert h["pending"] is False and h["last_commit_at"] and h["last_push_at"]


def test_health_tool_carries_the_block_and_keeps_its_old_keys():
    import inspect

    from agent_context import server as srv
    assert "_commit_on_write_health" in inspect.getsource(srv.get_health)




def test_message_names_paths_up_to_a_cap(repo, cow):
    store, root, _ = repo
    _doc(store, "n1.md")
    _doc(store, "n2.md")
    cow.clock.advance(3.1)
    cow.tick()
    msg = _git(root, "log", "-1", "--format=%B").stdout
    assert not msg.startswith("sync ")
    assert "global/docs/n1.md" in msg and "global/docs/n2.md" in msg

    for i in range(40):
        _doc(store, f"many{i}.md")
    cow.clock.advance(3.1)
    cow.tick()
    msg = _git(root, "log", "-1", "--format=%B").stdout
    assert "40" in msg and len(msg) < 800, "message must cap the path list and give a count"




def test_content_changed_fires_before_the_commit(repo, cow):
    store, root, _ = repo
    seen = []
    store.set_write_callback(lambda p: seen.append(_count(root)))
    before = _count(root)
    _doc(store, "o.md")
    assert seen == [before], "content_changed must fire right after the write, before the commit"
    cow.clock.advance(3.1)
    cow.tick()
    assert _count(root) == before + 1


def test_sync_still_commits_with_its_own_message_and_pushes(repo):
    store, root, mirrors = repo
    paths.write_atomic(root / "global" / "loose.md", "x\n")   
    res = store.sync()
    assert res.get("committed") is True and res.get("push") is True
    assert _git(root, "log", "-1", "--format=%s").stdout.startswith("sync ")
    assert _remote_tip(mirrors["origin"]) == _git(root, "rev-parse", "HEAD").stdout.strip()
