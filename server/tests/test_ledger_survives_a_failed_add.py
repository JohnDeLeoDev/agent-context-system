'A staging that fails keeps the write ledger (policy).'
import subprocess

from fixture_signing import signing_config

from agent_context import docs
from agent_context.store import ContextStore


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True)


def _store(tmp_path, monkeypatch):
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
    return ContextStore(root=str(root)), root


def test_a_failed_add_keeps_the_entries_and_the_next_commit_takes_them(tmp_path, monkeypatch):
    store, root = _store(tmp_path, monkeypatch)
    docs.upsert_doc(store, "held.md", body="x")
    rel = "global/docs/held.md"
    assert rel in store.ledger.snapshot()

    real_git = store._git

    def busy_index(*args, **kw):
        if args and args[0] == "add":
            return subprocess.CompletedProcess(args, 128, "", "fatal: Unable to create "
                                               "'.git/index.lock': File exists.")
        return real_git(*args, **kw)

    monkeypatch.setattr(store, "_git", busy_index)
    assert store.commit_local("first try").get("committed") is not True
    assert rel in store.ledger.snapshot()
    assert rel not in store.outside_edits

    monkeypatch.setattr(store, "_git", real_git)
    assert store.commit_local("second try").get("committed") is True
    assert rel in _git(root, "ls-files").stdout
    assert rel not in store.ledger.snapshot()
