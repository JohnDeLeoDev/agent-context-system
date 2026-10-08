"rename_project: a project's record, directory, entities, pointers and history move together.\n\nThe battery builds one project holding every entity kind, points at it from other scopes\nin every addressing form, commits, renames, and checks what each reader sees afterwards."
from __future__ import annotations

import json
import os
import subprocess

import pytest
from fixture_signing import signing_config_beside

from agent_context import fstools as T
from agent_context import server
from agent_context.store import ContextStore, stable_uuid

OLD, NEW = "oldproj", "New Helper"
REMOTE = "git@github.com:acme/helper.git"


def _git(root: str, *args: str) -> str:
    config = [a for k, v in signing_config_beside(root) for a in ("-c", f"{k}={v}")]
    return subprocess.run(
        ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", *config, *args],
        check=True, capture_output=True, text=True).stdout


def _commit(store, msg: str) -> None:
    _git(store.root, "add", "-A")
    _git(store.root, "commit", "-q", "-m", msg)


@pytest.fixture
def full(store):
    'A project holding every entity kind, pointed at from global and another project.'
    _git(store.root, "init", "-q")
    T.upsert_project(store, REMOTE, OLD, stack="rust", workspace=None)
    T.upsert_project(store, "git@github.com:acme/other.git", "Other")
    T.upsert_instruction(store, "oldproj Instructions", "Use cargo.", project=OLD)
    T.upsert_memory(store, "mem-a", "project", "a memory", "Body of a.", project=OLD)
    T.upsert_doc(store, "design/plan.md", "The plan. See [[projects/oldproj/memory/mem-a]].",
                 project=OLD, title="Plan")
    T.upsert_doc(store, f"{OLD}.md", "Root page.", project=OLD, title=OLD)
    T.upsert_skill(store, "build", "Build it", "Run cargo build.", project=OLD)
    T.upsert_command(store, "ship", "Ship it.", project=OLD, description="ship")
    T.upsert_agent_definition(store, "helper-worker", "A worker", "Work.", project=OLD)
    T.upsert_hook(store, "guard", "PreToolUse", "#!/bin/sh\nexit 0\n", project=OLD,
                  language="sh", description="a guard")
    T.upsert_script(store, "wt-new", "print('x')\n", project=OLD, language="python",
                    description="make a worktree")
    T.upsert_memory(
        store, "points-at-it", "reference", "pointers",
        'Read get_doc("design/plan.md", "oldproj") and get_memory("mem-a", project="oldproj") '
        'and get_entity("script", "wt-new", "oldproj"), [[projects/oldproj/docs/design/plan.md]], '
        'the root [[oldproj]]. History: the program was called oldproj.')
    T.upsert_doc(store, "notes/other.md", "Also get_memory('mem-a', project='oldproj').",
                 project="Other", links={"depends_on": ["project:oldproj::memory:mem-a"]})
    assert "error" not in T.add_audit_observation(
        store, "The helper build is slow on every run.", scope="project", project=OLD,
        evidence="design/plan.md:1 says so")
    _commit(store, "seed")
    return store


def _files_under(root: str, rel: str) -> list[str]:
    base = os.path.join(root, rel)
    return sorted(os.path.relpath(os.path.join(d, f), base)
                  for d, _dn, fs in os.walk(base) for f in fs)


def test_rename_moves_every_entity_kind_and_keeps_the_id(full):
    before = _files_under(full.root, f"projects/{OLD}")
    old_id = full.project_entity(OLD)["uuid"]
    r = T.rename_project(full, OLD, NEW)
    assert "error" not in r, r
    assert not os.path.exists(os.path.join(full.root, "projects", OLD))
    after = _files_under(full.root, f"projects/{NEW}")
    assert after == sorted(f.replace(f"docs/{OLD}.md", f"docs/{NEW}.md") for f in before)
    ent = full.project_entity(NEW)
    assert ent["uuid"] == old_id and ent["canonical_remote"] == REMOTE and ent["stack"] == "rust"
    assert full.project_entity(OLD) is None
    assert r["moved_entities"] == {"instruction": 1, "memory": 1, "doc": 2, "skill": 1,
                                   "command": 1, "agent_definition": 1, "hook": 1, "script": 1}
    
    assert not [e for e in full.entities.values() if e.get("scope") == f"project:{OLD}"]
    assert T.get_memory(full, "mem-a", project=NEW)["body"] == "Body of a."
    assert T.get_doc(full, "design/plan.md", project=NEW)
    assert T.get_doc(full, f"{NEW}.md", project=NEW)
    for kind, key in (("skill", "build"), ("command", "ship"), ("hook", "guard"),
                      ("script", "wt-new"), ("agent_definition", "helper-worker")):
        got = T.get_entity(full, kind, key, project=NEW)
        assert got and "error" not in got, (kind, got)
    assert [i["title"] for i in T.get_instructions(full, NEW) if i.get("scope") == f"project:{NEW}"] \
        == ["oldproj Instructions"]
    assert not full.load_errors


def test_moved_entities_get_the_ids_and_scope_a_write_there_would_give(full):
    T.rename_project(full, OLD, NEW)
    scope = f"project:{NEW}"
    for e in full.entities.values():
        if e.get("scope") != scope or e["type"] == "project":
            continue
        key = e.get({"memory": "slug", "doc": "path", "instruction": "title"}.get(e["type"], "name"))
        assert e["uuid"] == stable_uuid(e["type"], scope, key), (e["type"], key)
    
    fresh = ContextStore(root=full.root)
    assert {e["uuid"] for e in fresh.entities.values() if e.get("scope") == scope} \
        == {e["uuid"] for e in full.entities.values() if e.get("scope") == scope}
    integ = T.check_integrity(fresh)
    assert not integ.get("uuid_scope_mismatch"), integ.get("uuid_scope_mismatch")
    assert not integ.get("filename_key_mismatch"), integ.get("filename_key_mismatch")


def test_pointers_elsewhere_are_readdressed_and_history_mentions_are_not(full):
    r = T.rename_project(full, OLD, NEW)
    body = T.get_memory(full, "points-at-it")["body"]
    assert 'get_doc("design/plan.md", "New Helper")' in body
    assert 'get_memory("mem-a", project="New Helper")' in body
    assert 'get_entity("script", "wt-new", "New Helper")' in body
    assert "[[projects/New Helper/docs/design/plan.md]]" in body
    assert "[[New Helper]]" in body
    assert "the program was called oldproj." in body
    other = open(full.get("doc", "notes/other.md", "Other")["_path"], encoding="utf-8").read()
    assert "get_memory('mem-a', project='New Helper')" in other
    assert "oldproj" not in other.split("\n---", 1)[0]
    inner = T.get_doc(full, "design/plan.md", project=NEW)["body"]
    assert "[[projects/New Helper/memory/mem-a]]" in inner
    assert len(r["pointers_rewritten"]) == 2


def test_audit_observations_follow_the_project(full):
    r = T.rename_project(full, OLD, NEW)
    assert r["audit_observations"]
    obs = T.list_audit_observations(full, project=NEW)
    rows = obs["observations"] if isinstance(obs, dict) else obs
    assert rows and all(o["project"] == NEW for o in rows)


def test_history_follows_the_move(full):
    T.rename_project(full, OLD, NEW)
    _commit(full, "rename")
    h = T.get_version_history(full, "memory", key="mem-a", project=NEW)
    assert [v["change_summary"] for v in h] == ["rename", "seed"]
    s = T.get_version_history(full, "script", key="wt-new", project=NEW)
    assert len(s) == 2
    root = T.get_version_history(full, "doc", key=f"{NEW}.md", project=NEW)
    assert [v["change_summary"] for v in root] == ["rename", "seed"]


@pytest.mark.parametrize("bad", ["", " x", "a/b", "a:b", ".hidden", "global", "x\ny"])
def test_bad_names_are_refused_and_nothing_moves(full, bad):
    r = T.rename_project(full, OLD, bad)
    assert "error" in r
    assert os.path.isdir(os.path.join(full.root, "projects", OLD))


def test_a_taken_name_is_refused_whatever_its_case(full):
    for taken in ("Other", "other", OLD.upper()):
        r = T.rename_project(full, OLD, taken)
        assert "error" in r, taken
    assert os.path.isdir(os.path.join(full.root, "projects", OLD))
    assert full.project_entity(OLD)


def test_unknown_project_is_refused(full):
    assert "error" in T.rename_project(full, "nope", NEW)


def test_project_can_be_named_by_remote(full):
    assert "error" not in T.rename_project(full, REMOTE, NEW)
    assert full.project_entity(NEW)


def test_dry_run_reports_and_writes_nothing(full, monkeypatch):
    monkeypatch.setattr(server, "_store", full)
    head = _git(full.root, "status", "--porcelain")
    out = json.loads(server.upsert_project(REMOTE, OLD, rename_to=NEW, dry_run=True))
    assert out["dry_run"] is True and out["result"]["renamed"] == {"from": OLD, "to": NEW}
    actions = {c["path"]: c["action"] for c in out["would_change"]}
    assert actions[f"projects/{OLD}/project.toml"] == "delete"
    assert actions[f"projects/{NEW}/project.toml"] == "create"
    assert os.path.isdir(os.path.join(full.root, "projects", OLD))
    assert not os.path.exists(os.path.join(full.root, "projects", NEW))
    assert _git(full.root, "status", "--porcelain") == head


def _snapshot(root: str) -> dict[str, bytes]:
    out = {}
    for d, dns, fs in os.walk(root):
        dns[:] = [n for n in dns if n != ".git"]
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = open(p, "rb").read()
    return out


def test_an_unreadable_entity_file_refuses_before_anything_moves(full):
    bad = os.path.join(full.root, "projects", OLD, "docs", "broken.bin.meta.json")
    with open(bad, "w") as fh:
        fh.write("{not json")
    before = _snapshot(full.root)
    with pytest.raises(ValueError, match="broken.bin.meta.json"):
        T.rename_project(full, OLD, NEW)
    assert _snapshot(full.root) == before


def test_a_failure_part_way_puts_everything_back(full, monkeypatch):
    before = _snapshot(full.root)
    from agent_context import audit as A
    monkeypatch.setattr(A, "_audit_write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        T.rename_project(full, OLD, NEW)
    assert _snapshot(full.root) == before
    assert full.project_entity(OLD) and not full.project_entity(NEW)


def test_the_root_page_never_overwrites_a_doc_named_like_the_new_project(full):
    T.upsert_doc(full, f"{NEW}.md", "Already here.", project=OLD, title="x")
    r = T.rename_project(full, OLD, NEW)
    assert "error" in r and f"docs/{NEW}.md" in r["error"]
    assert os.path.isdir(os.path.join(full.root, "projects", OLD))


def test_an_obsidian_style_root_page_gets_its_new_path_and_title(full):
    p = os.path.join(full.root, "projects", OLD, "docs", f"{OLD}.md")
    text = open(p, encoding="utf-8").read()
    text = text.replace(f'path: "{OLD}.md"', f"path: {OLD}.md").replace(
        f'title: "{OLD}"', f"title: {OLD}")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    T.rename_project(full, OLD, NEW)
    doc = T.get_doc(full, f"{NEW}.md", project=NEW)
    assert doc and doc["title"] == NEW
    integ = T.check_integrity(ContextStore(root=full.root))
    assert not integ.get("filename_key_mismatch") and not integ.get("uuid_scope_mismatch")


def test_typed_links_in_script_sidecars_elsewhere_follow(full):
    T.upsert_script(full, "sc", "echo\n", project="Other", language="sh", description="d")
    r = T.set_entity_links(full, "script", "sc", {"depends_on": [f"projects/{OLD}/memory/mem-a.md"]},
                           project="Other")
    assert "error" not in r, r
    T.rename_project(full, OLD, NEW)
    side = open(full.get("script", "sc", "Other")["_path"] + ".meta.toml", encoding="utf-8").read()
    assert f"projects/{NEW}/memory/mem-a.md" in side and f"projects/{OLD}/" not in side
    assert not T.check_integrity(full).get("dangling_typed_links")


@pytest.mark.parametrize("bad", ['A"B', "A'B", "A]B", "A[B", "A|B", "A#B"])
def test_names_that_would_break_a_pointer_are_refused(full, bad):
    assert "error" in T.rename_project(full, OLD, bad)


def test_a_case_only_change_says_so(full):
    r = T.rename_project(full, OLD, OLD.upper())
    assert "error" in r and "case" in r["error"]
