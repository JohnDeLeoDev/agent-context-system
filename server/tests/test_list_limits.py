'`list_entities` pages (default 50), list-returning tools are capped near 60 KB, and\n`get_version_history` accepts a UUID or `kind` + `key` + `project` and says so when it finds nothing.'
from __future__ import annotations

import json
import subprocess

import pytest
from fixture_signing import signing_config_beside

from agent_context import audit, paging, server
from agent_context import fstools as T


@pytest.fixture
def srv(store, monkeypatch: pytest.MonkeyPatch):
    'The MCP tool functions, bound to a throwaway store.'
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    return server


def _memories(store, prefix: str, n: int) -> None:
    for i in range(n):
        T.upsert_memory(store, f"{prefix}-{i:03}", "reference", f"about {prefix} {i}", "body")


def _call(fn, *a, **k) -> dict:
    return json.loads(fn(*a, **k))




def test_the_default_page_is_fifty_items_with_the_total_and_the_next_offset(store, srv) -> None:
    _memories(store, "m", 60)
    r = _call(srv.list_entities, "memory")
    assert (r["total"], r["shown"], r["next"]) == (60, 50, 50)
    assert len(r["items"]) == 50


def test_the_next_offset_reaches_the_rest_and_then_ends(store, srv) -> None:
    _memories(store, "m", 60)
    r = _call(srv.list_entities, "memory", offset=50)
    assert (r["total"], r["shown"], r["next"]) == (60, 10, None)
    first = _call(srv.list_entities, "memory")
    slugs = [i["slug"] for i in first["items"]] + [i["slug"] for i in r["items"]]
    assert len(set(slugs)) == 60


def test_an_explicit_limit_and_offset_slice_the_list(store, srv) -> None:
    _memories(store, "m", 30)
    r = _call(srv.list_entities, "memory", limit=10, offset=5)
    assert (r["shown"], r["next"]) == (10, 15)


def test_limit_zero_returns_everything(store, srv) -> None:
    _memories(store, "m", 60)
    r = _call(srv.list_entities, "memory", limit=0)
    assert (r["total"], r["shown"], r["next"]) == (60, 60, None)


def test_name_prefix_narrows_memories_before_paging(store, srv) -> None:
    _memories(store, "alpha", 3)
    _memories(store, "beta", 4)
    r = _call(srv.list_entities, "memory", name_prefix="beta")
    assert r["total"] == 4
    assert all(i["slug"].startswith("beta") for i in r["items"])


def test_an_offset_past_the_end_is_an_empty_page_not_an_error(store, srv) -> None:
    _memories(store, "m", 3)
    r = _call(srv.list_entities, "memory", offset=99)
    assert (r["items"], r["total"], r["shown"], r["next"]) == ([], 3, 0, None)


def test_a_negative_limit_or_offset_is_an_error_naming_the_argument(store, srv) -> None:
    assert "limit" in _call(srv.list_entities, "memory", limit=-1)["error"]
    assert "offset" in _call(srv.list_entities, "memory", offset=-1)["error"]


def test_docs_page_and_keep_their_path_prefix(store, srv) -> None:
    for i in range(3):
        T.upsert_doc(store, f"features/f{i}.md", body="x", title=f"F{i}")
    T.upsert_doc(store, "other/o.md", body="x", title="O")
    r = _call(srv.list_entities, "doc", path_prefix="features/", limit=2)
    assert (r["total"], r["shown"], r["next"]) == (3, 2, 2)


def test_an_unknown_kind_still_returns_the_valid_kinds_not_a_page(store, srv) -> None:
    r = _call(srv.list_entities, "bogus")
    assert "error" in r and "memory" in r["error"] and "items" not in r




def test_the_python_list_entities_still_returns_a_plain_list(store) -> None:
    _memories(store, "alpha", 3)
    _memories(store, "beta", 60)
    plain = T.list_entities(store, "memory")
    assert isinstance(plain, list) and len(plain) == 63
    assert len(T.list_entities(store, "memory", name_prefix="alpha")) == 3




def _fat(n: int, size: int = 400) -> list[dict]:
    return [{"id": i, "text": "x" * size} for i in range(n)]


def test_a_page_over_the_byte_cap_is_cut_on_an_item_boundary(store) -> None:
    items = _fat(100)
    r = paging.page(items, limit=0, max_bytes=5000)
    assert r["truncated"] is True
    assert 0 < r["shown"] < 100 and r["total"] == 100
    assert r["items"] == items[:r["shown"]]
    assert r["next"] == r["shown"]
    assert len(json.dumps(r["items"], separators=(",", ":"))) <= 5000


def test_one_item_larger_than_the_cap_is_still_returned_whole() -> None:
    items = _fat(3, size=9000)
    r = paging.page(items, limit=0, max_bytes=1000)
    assert r["shown"] == 1 and r["items"] == items[:1] and r["next"] == 1


def test_a_page_under_the_cap_is_not_marked_truncated() -> None:
    r = paging.page(_fat(3), limit=0, max_bytes=50_000)
    assert "truncated" not in r and r["shown"] == 3


def test_cap_leaves_a_small_value_alone() -> None:
    assert paging.cap([1, 2, 3], max_bytes=1000) == [1, 2, 3]
    assert paging.cap({"a": 1}, max_bytes=1000) == {"a": 1}


def test_cap_turns_an_oversize_list_into_a_truncated_envelope() -> None:
    items = _fat(100)
    r = paging.cap(items, max_bytes=5000)
    assert r["truncated"] is True and r["total"] == 100
    assert r["items"] == items[:r["shown"]] and r["next"] == r["shown"]


def test_cap_trims_the_named_list_inside_a_dict_and_keeps_the_rest() -> None:
    value = {"observations": _fat(100), "total": 100, "hint": "h"}
    r = paging.cap(value, max_bytes=5000, key="observations")
    assert r["hint"] == "h" and r["truncated"] is True
    assert r["shown"] == len(r["observations"]) < 100 and r["next"] == r["shown"]


def test_the_default_cap_is_about_sixty_kilobytes() -> None:
    assert 50_000 <= paging.MAX_BYTES <= 70_000
    assert paging.DEFAULT_LIMIT == 50


def test_list_entities_limit_zero_is_still_capped(
        store, srv, monkeypatch: pytest.MonkeyPatch) -> None:
    _memories(store, "m", 60)
    monkeypatch.setattr(paging, "MAX_BYTES", 3000)
    r = _call(srv.list_entities, "memory", limit=0)
    assert r["truncated"] is True and r["shown"] < 60 and r["next"] == r["shown"]


def test_the_cap_applies_to_the_other_list_tools(
        store, srv, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(30):
        T.upsert_project(store, f"git@github.com:acme/repo-{i}.git", f"Project{i:02}")
        T.upsert_doc(store, f"d/doc{i}.md", body="needle " * 20, title=f"Doc {i}")
        T.upsert_memory(store, f"needle-{i}", "reference", "needle desc", "needle body")
        T.add_audit_observation(store, f"observation number {i} " + "word " * 10,
                                scope="universal", evidence=f"evidence {i}")
    monkeypatch.setattr(paging, "MAX_BYTES", 2500)
    assert _call(srv.list_entities, "project", limit=0)["truncated"] is True
    
    assert _call(srv.search_all, "needle", limit=30, kind="doc")["truncated"] is True
    assert _call(srv.search_all, "needle", limit=30, kind="memory")["truncated"] is True
    assert _call(srv.search_all, "needle", limit=30)["truncated"] is True
    audit_r = _call(srv.list_audit_observations, status="open")
    assert audit_r["truncated"] is True and audit_r["shown"] == len(audit_r["observations"])


def test_a_small_result_from_the_other_list_tools_keeps_its_old_shape(store, srv) -> None:
    T.upsert_project(store, "git@github.com:acme/one.git", "One")
    
    page = _call(srv.list_entities, "project")
    assert [p["display_name"] for p in page["items"]] == ["One"] and "truncated" not in page
    assert audit.list_view(store)["observations"] == []




def _git(root: str, *args: str) -> None:
    config = [a for k, v in signing_config_beside(root) for a in ("-c", f"{k}={v}")]
    subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t",
                    *config, *args], check=True, capture_output=True)


@pytest.fixture
def edited(store):
    'A memory committed three times, and a project-scoped memory committed twice.'
    _git(store.root, "init", "-q")
    T.upsert_memory(store, "hist-one", "reference", "v1", "body one")
    _git(store.root, "add", "-A")
    _git(store.root, "commit", "-q", "-m", "create hist-one")
    for n in (2, 3):
        T.upsert_memory(store, "hist-one", "reference", f"v{n}", f"body {n}")
        _git(store.root, "add", "-A")
        _git(store.root, "commit", "-q", "-m", f"edit hist-one v{n}")
    T.upsert_project(store, "git@github.com:acme/scoped.git", "Scoped")
    T.upsert_memory(store, "hist-proj", "reference", "p1", "b1", project="Scoped")
    _git(store.root, "add", "-A")
    _git(store.root, "commit", "-q", "-m", "create hist-proj")
    T.upsert_memory(store, "hist-proj", "reference", "p2", "b2", project="Scoped")
    _git(store.root, "add", "-A")
    _git(store.root, "commit", "-q", "-m", "edit hist-proj")
    return store


def _uuid(store, slug: str) -> str:
    return store.get("memory", slug, None)["uuid"]


def test_history_by_uuid_lists_every_commit_that_touched_the_file(edited) -> None:
    h = T.get_version_history(edited, "memory", _uuid(edited, "hist-one"))
    assert [v["change_summary"] for v in h] == ["edit hist-one v3", "edit hist-one v2",
                                                "create hist-one"]


def test_history_by_kind_and_key_matches_the_uuid_form(edited) -> None:
    by_uuid = T.get_version_history(edited, "memory", _uuid(edited, "hist-one"))
    by_name = T.get_version_history(edited, "memory", key="hist-one")
    assert by_name == by_uuid and len(by_name) == 3


def test_history_by_kind_key_and_project_finds_a_project_scoped_entity(edited) -> None:
    h = T.get_version_history(edited, "memory", key="hist-proj", project="Scoped")
    assert [v["change_summary"] for v in h] == ["edit hist-proj", "create hist-proj"]


def test_the_mcp_tool_accepts_a_uuid_string_and_the_named_form(edited, srv) -> None:
    uid = _uuid(edited, "hist-one")
    assert len(_call(srv.get_version_history, "memory", uid)) == 3
    assert len(_call(srv.get_version_history, "memory", key="hist-one")) == 3


def test_a_numeric_id_is_still_accepted_and_reports_that_nothing_matches(edited, srv) -> None:
    r = _call(srv.get_version_history, "memory", 12345)
    assert "error" in r and "12345" in r["error"]


def test_an_unknown_uuid_or_key_is_an_error_naming_what_was_tried(edited, srv) -> None:
    assert "no-such-uuid" in _call(srv.get_version_history, "memory", "no-such-uuid")["error"]
    r = _call(srv.get_version_history, "memory", key="no-such-slug", project="Scoped")
    assert "no-such-slug" in r["error"] and "Scoped" in r["error"]


def test_history_needs_a_uuid_or_a_key(edited, srv) -> None:
    assert "entity_id" in _call(srv.get_version_history, "memory")["error"]


def test_a_uuid_of_the_wrong_kind_is_not_returned(edited) -> None:
    r = T.get_version_history(edited, "doc", _uuid(edited, "hist-one"))
    assert isinstance(r, dict) and "error" in r


def test_the_history_of_an_entity_with_no_commits_is_an_empty_list(store) -> None:
    T.upsert_memory(store, "fresh", "reference", "d", "b")
    _git(store.root, "init", "-q")
    assert T.get_version_history(store, "memory", _uuid(store, "fresh")) == []
    assert T.get_version_history(store, "memory", key="fresh") == []

