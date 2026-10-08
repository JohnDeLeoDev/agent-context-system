'Every case here runs on throwaway repos under tmp_path; the live store is never touched.'
import subprocess

import pytest
from fixture_signing import signing_config

from agent_context.server import sync_verdict
from agent_context.store import ContextStore


def _git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=check)


def _bare(tmp_path, name):
    p = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(p)], check=True)
    return p


def _commit(root, name, body, msg):
    (root / name).write_text(body)
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def fleet(tmp_path):
    'A local store with three bare mirrors, all agreeing on one commit.\n\n    Returns (store, root, {name: bare_path}).'
    root = tmp_path / "ctx"
    (root / "global").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 *signing_config(tmp_path)):
        _git(root, "config", k, v)
    _commit(root, "a.txt", "one\n", "one")

    mirrors = {n: _bare(tmp_path, f"{n}.git") for n in ("origin", "m1", "m2")}
    for name, path in mirrors.items():
        _git(root, "remote", "add", name, str(path))
        _git(root, "push", "-q", name, "main")
    _git(root, "fetch", "--all", "-q")
    return ContextStore(root=str(root)), root, mirrors


def _refs(root, branch="main"):
    'The refs sync() actually passes in — `<remote>/<branch>` for each configured\n    remote, exactly as _remote_mains builds them. Scraping `for-each-ref\n    refs/remotes` instead is wrong in a way worth naming: `refs/remotes/m1/HEAD`\n    shortens to a bare `m1`, which is not a branch tip at all.'
    remotes = _git(root, "remote").stdout.split()
    return [f"{r}/{branch}" for r in remotes
            if _git(root, "rev-parse", "--verify", "--quiet", f"{r}/{branch}",
                    check=False).returncode == 0]


def test_rebase_orphan_is_offered_for_a_lease_pinned_force(fleet, tmp_path):
    'test rebase orphan is offered for a lease pinned force.'
    store, root, _ = fleet
    orphan_sha = _commit(root, "b.txt", "two\n", "two")
    _git(root, "push", "-q", "m1", "main")          

    
    _git(root, "commit", "--amend", "--no-edit", "-q", "--date", "2030-01-01T00:00:00")
    new_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    assert new_sha != orphan_sha
    _git(root, "push", "-q", "origin", "main")
    _git(root, "fetch", "--all", "-q")

    merged, orphans, stranded = store._reconcile_diverged(_refs(root), "main")

    assert orphans == {"m1": orphan_sha}
    assert merged == [] and stranded == []


def test_a_mirror_with_unique_work_is_merged_not_forced(fleet, tmp_path):
    'The case the old base picker discarded silently. A tip carrying commits no\n    other remote has must be INTEGRATED — after which it is an ancestor of HEAD and\n    its next push fast-forwards, so no force is needed or offered.'
    store, root, mirrors = fleet

    
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(mirrors["m1"]), str(other)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 *signing_config(tmp_path)):
        _git(other, "config", k, v)
    _commit(other, "theirs.txt", "theirs\n", "theirs")
    _git(other, "push", "-q", "origin", "main")

    _commit(root, "mine.txt", "mine\n", "mine")     
    _git(root, "fetch", "--all", "-q")

    merged, orphans, stranded = store._reconcile_diverged(_refs(root), "main")

    assert merged == ["m1/main"]
    assert orphans == {} and stranded == []
    
    assert _git(root, "merge-base", "--is-ancestor", "m1/main", "HEAD",
                check=False).returncode == 0
    assert (root / "theirs.txt").exists() and (root / "mine.txt").exists()


def test_a_conflicting_mirror_is_stranded_for_a_human_and_leaves_a_clean_tree(fleet, tmp_path):
    'The one split that cannot be settled mechanically. It must be NAMED, and the\n    working tree must be left clean — an aborted merge, not a half-applied one.'
    store, root, mirrors = fleet

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(mirrors["m1"]), str(other)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 *signing_config(tmp_path)):
        _git(other, "config", k, v)
    _commit(other, "shared.txt", "theirs\n", "theirs")
    _git(other, "push", "-q", "origin", "main")

    _commit(root, "shared.txt", "mine\n", "mine")   
    _git(root, "fetch", "--all", "-q")

    merged, orphans, stranded = store._reconcile_diverged(_refs(root), "main")

    assert stranded == ["m1/main"]
    assert merged == [] and orphans == {}
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert not (root / ".git" / "MERGE_HEAD").exists()


def test_a_contained_tip_is_left_entirely_alone(fleet):
    'A mirror that is merely BEHIND is not a split — it needs no merge and no\n    force, only the ordinary push. Touching it would be churn on every cycle.'
    store, root, _ = fleet
    _commit(root, "c.txt", "three\n", "three")
    _git(root, "fetch", "--all", "-q")
    head_before = _git(root, "rev-parse", "HEAD").stdout.strip()

    merged, orphans, stranded = store._reconcile_diverged(_refs(root), "main")

    assert (merged, orphans, stranded) == ([], {}, [])
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before


def test_verdict_reports_a_stranded_mirror_with_no_streak_tolerance():
    'A conflicting mirror never self-heals, so one cycle is enough to report it —\n    and the reason must carry the remedy, since a phone alert is often all a human\n    gets.'
    reason = sync_verdict({"pull": True, "push": True, "stranded_remotes": ["m1/main"]})
    assert reason and "m1/main" in reason
    assert "git merge m1/main" in reason


def test_verdict_reports_an_orphaned_origin_that_cannot_be_leased():
    'origin fans out to every mirror URL and a lease sha matches only one of them,\n    so sync deliberately does not force it — which makes saying so mandatory.'
    reason = sync_verdict({"pull": True, "push": True,
                           "orphan_needs_human": {"origin": "a" * 40}})
    assert reason and "origin" in reason and "aaaaaaaa" in reason


def test_a_settled_split_is_still_a_healthy_cycle():
    'Merging a straggler and force-with-leasing an orphan ARE the reconciliation\n    working. Reporting them as failures would page a human for a self-heal.'
    assert sync_verdict({"pull": True, "push": True, "diverged_remotes": True,
                         "merged_stragglers": ["m1/main"],
                         "rebase_orphans": {"m2": "b" * 40}}) is None
