'An entity write is refused when a remote already holds a newer version of it.'
import subprocess

import pytest
from fixture_signing import signing_config_beside

from agent_context.store import ContextStore, StaleWriteError


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
    (seed / "global" / "memory").mkdir(parents=True)
    (seed / "global" / ".keep").write_text("")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "remote", "add", "origin", str(up))
    _git(seed, "push", "-q", "origin", "main")
    for r in (a, b):
        _git(tmp_path, "clone", "-q", str(up), str(r))
        _identify(r)
    return a, b


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_STALE_GUARD", "1")


def test_a_write_to_an_entity_changed_elsewhere_is_refused(tmp_path, guard_on):
    a, b = _fleet(tmp_path)
    
    sb = ContextStore(str(b))
    sb.upsert("memory", "shared-note", {"description": "b's version"}, body="from b")
    _git(b, "add", "-A")
    _git(b, "commit", "-qm", "b writes")
    _git(b, "push", "-q", "origin", "main")

    
    sa = ContextStore(str(a))
    with pytest.raises(StaleWriteError) as e:
        sa.upsert("memory", "shared-note", {"description": "a's version"}, body="from a")
    assert "origin/main" in str(e.value)
    
    assert "sync_materialization" not in str(e.value)
    assert "re-read the entity" in str(e.value)
    
    assert not (a / "global" / "memory" / "shared-note.md").exists()

    
    _git(a, "pull", "-q", "--no-rebase", "origin", "main")
    sa.reload()
    sa.upsert("memory", "shared-note", {"description": "a's version"}, body="from a")
    assert (a / "global" / "memory" / "shared-note.md").read_text().endswith("from a\n")


def test_an_unrelated_remote_change_does_not_block_a_write(tmp_path, guard_on):
    a, b = _fleet(tmp_path)
    sb = ContextStore(str(b))
    sb.upsert("memory", "other-note", {"description": "x"}, body="x")
    _git(b, "add", "-A")
    _git(b, "commit", "-qm", "b writes another")
    _git(b, "push", "-q", "origin", "main")

    sa = ContextStore(str(a))
    sa.upsert("memory", "my-note", {"description": "y"}, body="y")     
    assert (a / "global" / "memory" / "my-note.md").exists()


def test_the_guard_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_STALE_GUARD", "0")
    a, b = _fleet(tmp_path)
    sb = ContextStore(str(b))
    sb.upsert("memory", "shared-note", {"description": "b"}, body="b")
    _git(b, "add", "-A")
    _git(b, "commit", "-qm", "b writes")
    _git(b, "push", "-q", "origin", "main")
    ContextStore(str(a)).upsert("memory", "shared-note", {"description": "a"}, body="a")


def test_the_guard_fetches_at_most_once_a_minute(tmp_path, guard_on, monkeypatch):
    a, _ = _fleet(tmp_path)
    sa = ContextStore(str(a))
    calls = []
    orig = sa._git

    def spy(*args, **kw):
        if args and args[0] == "fetch":
            calls.append(args)
        return orig(*args, **kw)
    monkeypatch.setattr(sa, "_git", spy)
    sa.upsert("memory", "n1", {"description": "1"}, body="1")
    sa.upsert("memory", "n2", {"description": "2"}, body="2")
    assert len(calls) == 1
