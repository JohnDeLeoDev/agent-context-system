'`git rebase --autostash` sweeps up uncommitted TRACKED files; server/ is the one\ndirectory sync() deliberately never autocommits, so an aborted rebase or a failed\nstash re-apply silently reverts in-progress server work. The guard defers the\nwhole network phase until server/ is clean.'
import subprocess

import pytest
from fixture_signing import signing_config

from agent_context import store as store_mod
from agent_context.store import ContextStore


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo_store(tmp_path):
    root = tmp_path / "ctx"
    (root / "global").mkdir(parents=True)
    (root / "server" / "src").mkdir(parents=True)
    (root / "server" / "src" / "code.py").write_text("x = 1\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    
    
    
    key = tmp_path / "sign_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t",
                    "-f", str(key)], check=True)
    
    
    
    
    key.chmod(0o600)
    _git(root, "config", "gpg.format", "ssh")
    _git(root, "config", "user.signingkey", str(key.with_suffix(".pub")))
    _git(root, "config", "commit.gpgsign", "true")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    store_mod._SERVER_DIRTY_LOGGED = False
    return root, ContextStore(root=str(root))


def test_dirty_server_defers_the_network_phase(repo_store):
    root, st = repo_store
    body = "x = 2  # in-progress edit\n"
    (root / "server" / "src" / "code.py").write_text(body)

    res = st.sync(push=True)

    assert res["pull"] is False
    assert "server/" in res["pull_skipped"]
    assert "fetch" not in res and "push" not in res
    
    assert (root / "server" / "src" / "code.py").read_text() == body


def test_clean_server_does_not_defer(repo_store):
    root, st = repo_store
    (root / "global" / "note.md").write_text("a doc write\n")

    res = st.sync(push=False)

    assert "pull_skipped" not in res
    assert res["pull"] is True   


def test_deferral_streak_is_counted_and_reset(repo_store):
    'A forgotten dirty tree must not stay invisible: the streak is what the sync\n    loop turns into a watchdog-visible failure after _DEFER_STALL_CYCLES.'
    root, st = repo_store
    (root / "server" / "src" / "code.py").write_text("x = 2\n")

    assert st.sync(push=False)["pull_skip_streak"] == 1
    assert st.sync(push=False)["pull_skip_streak"] == 2

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "release")
    assert "pull_skip_streak" not in st.sync(push=False)
    assert store_mod._SERVER_DIRTY_STREAK == 0

    (root / "server" / "src" / "code.py").write_text("x = 3\n")
    assert st.sync(push=False)["pull_skip_streak"] == 1   


def _with_remote(tmp_path, root):
    'Give `root` an origin holding one extra commit that conflicts with a local\n    edit to global/note.md. Returns the sibling clone used to advance the remote.'
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "main")

    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "o")
    for k, v in signing_config(tmp_path):
        _git(other, "config", k, v)
    (other / "global").mkdir(exist_ok=True)   
    (other / "global" / "note.md").write_text("theirs\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "remote edit")
    _git(other, "push", "-q", "origin", "main")
    return other


def test_a_conflicting_merge_reports_the_error_and_the_gap(tmp_path, repo_store):
    'Integration is a merge now, not a rebase-then-merge (see sync): rebase rewrote\n    shas, and rewritten shas are what strand mirrors. So there is one error to\n    report, not two — a merge conflict here means two machines really did change the\n    same hunk, which is the one case that needs a human.'
    root, st = repo_store
    _with_remote(tmp_path, root)

    
    (root / "global" / "note.md").write_text("ours\n")
    res = st.sync(push=False)

    assert res["pull"] is False
    assert res["pull_via"] == "merge"
    assert res["pull_error"]
    assert res["base"].endswith("main")
    assert res["behind"] == 1
    
    assert (root / "global" / "note.md").read_text() == "ours\n"
    assert not (root / ".git" / "rebase-merge").exists()
    assert not (root / ".git" / "MERGE_HEAD").exists()


def test_integration_never_rebases(tmp_path, repo_store):
    'Asserted on the SHA, because that is the property mirrors care about — a test\n    that merely checked for a merge commit would still pass if some future change\n    reintroduced a rebase for the fast-forward case.'
    root, st = repo_store
    _with_remote(tmp_path, root)

    (root / "global" / "mine.md").write_text("mine\n")   
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "local edit")
    before = _git(root, "rev-parse", "HEAD").stdout.strip()

    res = st.sync(push=False)

    assert res["pull"] is True
    after_local = _git(root, "rev-list", "HEAD", "--not", "origin/main").stdout.split()
    assert before in after_local, "integration rewrote the local commit's sha"
    assert (root / "global" / "note.md").read_text() == "theirs\n"   
    assert (root / "global" / "mine.md").read_text() == "mine\n"     


def test_merge_fallback_heals_a_rebase_that_can_never_succeed(tmp_path, repo_store):
    'Once a clone holds a commit whose replay conflicts, `rebase` can NEVER succeed:\n    it replays that commit onto the tip every cycle, and the commit can only stop\n    conflicting once it is IN the remote — which needs the push that sync() gates on\n    the rebase succeeding. rp span six hours in that loop, and merging by hand did\n    not break it, because the offending commit was still in origin/main..HEAD.\n\n    A merge compares the two TIPS, so it succeeds the moment the divergence is\n    reconciled in content — and the push that follows publishes the local commits and\n    ends the loop for good.'
    root, st = repo_store
    _git(root, "config", "user.email", "t@example.com")
    _with_remote(tmp_path, root)

    
    (root / "global" / "note.md").write_text("ours\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "local edit that will not replay")
    
    
    _git(root, "fetch", "-q", "origin")
    _git(root, "-c", "core.editor=true", "merge", "-q", "--no-edit", "-X", "theirs",
         "origin/main")
    assert (root / "global" / "note.md").read_text() == "theirs\n"

    res = st.sync(push=True)

    
    
    assert res["pull"] is True
    assert res["pull_via"] == "merge"
    assert "pull_error" not in res
    assert res["push"] is True and "push_error" not in res
    
    
    _git(root, "fetch", "-q", "origin")
    ahead = _git(root, "rev-list", "--count", "origin/main..HEAD")
    assert ahead.stdout.strip() == "0"
