'Audit-observation ids must not collide, and a collision must not need a human.\n\nTwo mechanisms, because neither is complete on its own: the id floor spans git refs so a\nbehind machine stops re-issuing spent numbers, and the merge repairs a collision that\nhappens anyway. These tests pin both, and pin the cases the repair must REFUSE.'
import json
import subprocess

import pytest
from fixture_signing import signing_config_beside

from agent_context import audit
from agent_context.store import ContextStore


def _git(cwd, *args, check=True):
    return subprocess.run(("git", "-C", str(cwd), *args),
                          capture_output=True, text=True, check=check)


def _obs(oid, text, machine):
    return {"id": oid, "observation": text, "scope": "universal", "project": None,
            "evidence": "e", "status": "open", "severity": "normal",
            "machine": machine, "observed_date": "2026-09-03T00:00:00Z",
            "created_at": "2026-09-03T00:00:00Z"}


def _write_obs(root, oid, text, machine):
    d = root / "global" / "audit-observations"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{oid:04d}.json"
    p.write_text(json.dumps(_obs(oid, text, machine), indent=1) + "\n")
    return p


def _identify(root):
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for k, v in signing_config_beside(root):
        _git(root, "config", k, v)


def _commit(root, msg):
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def _fleet(tmp_path, name):
    'A BARE upstream plus two working clones — `a` is the machine under test, `b`\n    stands in for the other machine. Bare because a push into a checked-out branch is\n    refused, and every one of these scenarios needs the other machine to publish.'
    up, a, b = tmp_path / f"{name}-up", tmp_path / f"{name}-a", tmp_path / f"{name}-b"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(up))
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _identify(seed)
    (seed / "seed.txt").write_text("0\n")
    _commit(seed, "seed")
    _git(seed, "remote", "add", "origin", str(up))
    _git(seed, "push", "-q", "origin", "main")
    for r in (a, b):
        _git(tmp_path, "clone", "-q", str(up), str(r))
        _identify(r)
    return a, b


@pytest.fixture
def collided(tmp_path):
    'Two machines that each filed a DIFFERENT observation as 0281 while apart.'
    a, b = _fleet(tmp_path, "collide")

    _write_obs(b, 281, "m4: lspd wedges Roslyn on a duplicate didOpen", "m4")
    _commit(b, "m4 files 281")
    _git(b, "push", "-q", "origin", "main")

    _write_obs(a, 281, "laptop: throttled self-heal reported as a repair", "laptop")
    _commit(a, "laptop files 281")
    _git(a, "fetch", "-q", "origin")
    return a, b


def test_id_floor_sees_ids_only_present_in_a_ref(collided):
    'The working tree is not the fleet: a fetched-but-unintegrated id is spent.'
    a, _ = collided
    assert audit._audit_id_floor_from_git(ContextStore(str(a))) == 281


def test_id_floor_is_zero_without_git(tmp_path):
    'A cheap prevention must never be able to refuse a filing.'
    assert audit._audit_id_floor_from_git(ContextStore(str(tmp_path / "nope"))) == 0


def test_next_id_clears_a_spent_number(collided, monkeypatch):
    'The end the floor exists for: the next filing on the behind machine must not\n    re-issue 281 just because its own directory is the only thing it consulted.'
    a, _ = collided
    monkeypatch.setattr(audit, "_this_machine", lambda: "laptop")
    store = ContextStore(str(a))
    rec = audit.add_audit_observation(
        store, "a new and unrelated defect worth recording here", scope="universal",
        evidence="fresh evidence")
    assert rec["id"] > 281, rec


def test_collision_is_resolved_by_renumbering_ours(collided):
    'Both records survive; the contested id stays with the published side.'
    a, _ = collided
    store = ContextStore(str(a))
    mrg = store._git("merge", "--no-edit", "origin/main", check=False)
    assert mrg.returncode != 0, "expected the add/add conflict"

    renumbered = store._resolve_observation_collisions()
    assert renumbered == {"global/audit-observations/0281.json": 282}

    kept = json.loads((a / "global/audit-observations/0281.json").read_text())
    moved = json.loads((a / "global/audit-observations/0282.json").read_text())
    assert kept["machine"] == "m4"          
    assert moved["machine"] == "laptop"     
    assert moved["id"] == 282
    assert "Roslyn" in kept["observation"]
    assert "self-heal" in moved["observation"]
    assert store._git("status", "--porcelain").stdout.strip() == ""


def test_sync_resolves_the_collision_end_to_end(collided):
    a, _ = collided
    res = ContextStore(str(a)).sync()
    assert res["pull"] is True
    assert res["pull_via"] == "merge+renumber"
    assert res["renumbered"] == {"global/audit-observations/0281.json": 282}


def test_repair_declines_an_edit_vs_edit_conflict(tmp_path):
    'Both machines editing the SAME observation is a real content conflict with a\n    real question in it. Renumbering would fork one record into two, keeping both\n    answers and flagging neither.'
    a, b = _fleet(tmp_path, "editwar")
    _write_obs(b, 300, "the original text", "laptop")
    _commit(b, "seed 300")
    _git(b, "push", "-q", "origin", "main")
    _git(a, "pull", "-q", "--no-rebase", "origin", "main")

    _write_obs(b, 300, "m4 rewrote it this way", "m4")
    _commit(b, "m4 edit")
    _git(b, "push", "-q", "origin", "main")

    _write_obs(a, 300, "the laptop rewrote it that way", "laptop")
    _commit(a, "laptop edit")
    _git(a, "fetch", "-q", "origin")

    store = ContextStore(str(a))
    assert store._git("merge", "--no-edit", "origin/main", check=False).returncode != 0
    assert store._resolve_observation_collisions() is None
    store._git("merge", "--abort", check=False)


def test_repair_declines_when_anything_else_conflicts(tmp_path):
    'One unrelated conflict means this is an ordinary divergence that happens to\n    include observations, and the whole thing belongs to a human.'
    a, b = _fleet(tmp_path, "mixed")

    _write_obs(b, 281, "m4 side", "m4")
    (b / "seed.txt").write_text("m4 seed\n")
    _commit(b, "m4 files 281 and edits seed")
    _git(b, "push", "-q", "origin", "main")

    _write_obs(a, 281, "laptop side", "laptop")
    (a / "seed.txt").write_text("laptop seed\n")
    _commit(a, "laptop files 281 and edits seed")
    _git(a, "fetch", "-q", "origin")

    store = ContextStore(str(a))
    assert store._git("merge", "--no-edit", "origin/main", check=False).returncode != 0
    assert store._resolve_observation_collisions() is None
    store._git("merge", "--abort", check=False)


def test_mixed_conflict_renumbers_the_observation_before_the_abort(tmp_path):
    'test mixed conflict renumbers the observation before the abort.'
    a, b = _fleet(tmp_path, "mixed2")

    _write_obs(b, 281, "m4 side", "m4")
    (b / "seed.txt").write_text("m4 seed\n")
    _commit(b, "m4 files 281 and edits seed")
    _git(b, "push", "-q", "origin", "main")

    _write_obs(a, 281, "laptop side", "laptop")
    (a / "seed.txt").write_text("laptop seed\n")
    _commit(a, "laptop files 281 and edits seed")
    _git(a, "fetch", "-q", "origin")

    store = ContextStore(str(a))
    res = store.sync()
    assert res["pull"] is False                       
    assert res["renumbered_after_abort"] == {"global/audit-observations/0281.json": 282}
    assert not (a / "global/audit-observations/0281.json").exists()
    moved = json.loads((a / "global/audit-observations/0282.json").read_text())
    assert moved["machine"] == "laptop" and moved["id"] == 282
    assert store._git("status", "--porcelain").stdout.strip() == ""
    assert "renumber audit observation" in store._git("log", "-1", "--format=%s").stdout

    
    assert store._git("merge", "--no-edit", "origin/main", check=False).returncode != 0
    unmerged = store._git("diff", "--name-only", "--diff-filter=U").stdout.split()
    assert unmerged == ["seed.txt"]
    store._git("merge", "--abort", check=False)


def test_renumber_after_abort_leaves_an_edit_war_alone(tmp_path):
    'A stage-1 ancestor means both machines edited ONE observation. That is a real\n    conflict with a real question in it, and the pre-abort path must not fork it into\n    two records any more than the mid-merge repair does.'
    a, b = _fleet(tmp_path, "editwar2")
    _write_obs(b, 300, "the original text", "laptop")
    _commit(b, "seed 300")
    _git(b, "push", "-q", "origin", "main")
    _git(a, "pull", "-q", "--no-rebase", "origin", "main")

    _write_obs(b, 300, "m4 rewrote it this way", "m4")
    _commit(b, "m4 edit")
    _git(b, "push", "-q", "origin", "main")

    _write_obs(a, 300, "the laptop rewrote it that way", "laptop")
    _commit(a, "laptop edit")
    _git(a, "fetch", "-q", "origin")

    store = ContextStore(str(a))
    res = store.sync()
    assert res["pull"] is False
    assert "renumbered_after_abort" not in res
    assert (a / "global/audit-observations/0300.json").exists()
    assert not (a / "global/audit-observations/0301.json").exists()








def _archive_obs(root, oid, text):
    d = root / "global" / "audit-observations-archive"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{oid:04d}.json").write_text(json.dumps(_obs(oid, text, "laptop"), indent=1) + "\n")


def test_id_floor_reads_every_ref_not_only_the_first(collided):
    '`git ls-tree` takes exactly ONE tree-ish, and passing it 46 refnames does not\n    fail -- git reads the first and treats the rest as pathspecs. So the floor was\n    whichever ref sorted first alphabetically. On the live laptop that was\n    `refs/backup/main-pre-resign`: floor 355 against a true max of 371, and every\n    caller believing it had asked the whole fleet.'
    a, _ = collided
    
    
    root_commit = _git(a, "rev-list", "--max-parents=0", "HEAD").stdout.split()[0]
    _git(a, "update-ref", "refs/backup/old", root_commit)
    assert audit._audit_id_floor_from_git(ContextStore(str(a))) == 281


def test_renumber_seeds_above_local_ids_when_the_floor_is_blind(collided, monkeypatch):
    'The floor answers "what have the refs spent" -- half the question. A machine\n    whose refs carry no observation files gets 0 from it, and seeding a renumber from\n    that alone is what minted ids 1, 2 and 3 while 371 were live.'
    a, _ = collided
    monkeypatch.setattr(audit, "_audit_id_floor_from_git", lambda store: 0)
    store = ContextStore(str(a))
    assert store._git("merge", "--no-edit", "origin/main", check=False).returncode != 0
    renumbered = store._resolve_observation_collisions()
    assert renumbered == {"global/audit-observations/0281.json": 282}, (
        "a blind floor must not send the renumber back to id 1")


def test_renumber_never_claims_an_archived_id(collided, monkeypatch):
    'The free-id probe looked only in the directory it was writing into, so an\n    active record could take an id the archive already held. 0001, 0002 and 0003 each\n    name two unrelated observations on the live fleet because of it.'
    a, _ = collided
    _archive_obs(a, 282, "an archived record already holding 282")
    _archive_obs(a, 283, "and another holding 283")
    _commit(a, "archive 282 and 283")
    monkeypatch.setattr(audit, "_audit_id_floor_from_git", lambda store: 0)
    store = ContextStore(str(a))
    assert store._git("merge", "--no-edit", "origin/main", check=False).returncode != 0
    renumbered = store._resolve_observation_collisions()
    assert renumbered == {"global/audit-observations/0281.json": 284}
    
    for oid, txt in ((282, "already holding 282"), (283, "another holding 283")):
        rec = json.loads((a / f"global/audit-observations-archive/{oid:04d}.json").read_text())
        assert txt in rec["observation"] and rec["id"] == oid
