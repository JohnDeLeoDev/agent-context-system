'test audit files.'
import json
import os

from agent_context import fstools as T


def _files(store, archive=False):
    d = T._audit_dir(store, archive)
    return sorted(f for f in os.listdir(d)) if os.path.isdir(d) else []


def test_each_observation_is_its_own_file(store):
    a = T.add_audit_observation(store, "a" * 60, "universal", None, "ev")
    b = T.add_audit_observation(store, "b" * 60, "universal", None, "ev")
    assert (a["id"], b["id"]) == (1, 2)
    assert _files(store) == ["0001.json", "0002.json"]
    on_disk = json.load(open(T._audit_file(store, 2)))
    assert on_disk["observation"] == "b" * 60
    assert [o["id"] for o in T.list_audit_observations(store)] == [1, 2]


def test_update_and_resolve_touch_only_their_file(store):
    T.add_audit_observation(store, "a" * 60, "universal", None, "ev")
    T.add_audit_observation(store, "b" * 60, "universal", None, "ev")
    before = open(T._audit_file(store, 1)).read()
    T.update_audit_observation(store, 2, note="seen again", recurred=True)
    T.resolve_audit_observation(store, 2, resolution_note="done")
    assert open(T._audit_file(store, 1)).read() == before
    two = json.load(open(T._audit_file(store, 2)))
    assert two["status"] == "resolved" and two["recurrences"] == 2 and "seen again" in two["notes"]


def test_legacy_aggregate_is_split_then_removed(store):
    legacy = T._audit_legacy_path(store)
    rows = [{"id": 5, "observation": "x" * 60, "scope": "universal", "status": "open",
             "created_at": "2026-08-01T00:00:00Z"},
            {"id": 7, "observation": "y" * 60, "scope": "universal", "status": "resolved",
             "created_at": "2026-08-01T00:00:00Z", "resolved_date": "2026-08-02T00:00:00Z"}]
    json.dump(rows, open(legacy, "w"))
    json.dump([{"id": 1, "observation": "old", "status": "resolved"}],
              open(T._audit_legacy_path(store, archive=True), "w"))
    got = T.list_audit_observations(store)
    assert [o["id"] for o in got] == [5, 7]
    assert not os.path.exists(legacy)
    assert not os.path.exists(T._audit_legacy_path(store, archive=True))
    assert _files(store) == ["0005.json", "0007.json"]
    assert _files(store, archive=True) == ["0001.json"]
    
    assert T.add_audit_observation(store, "z" * 60, "universal", None, "ev")["id"] == 8


def test_a_reappearing_aggregate_only_brings_newer_or_missing_records(store):
    T.add_audit_observation(store, "a" * 60, "universal", None, "ev")          
    T.resolve_audit_observation(store, 1, resolution_note="first")
    T.archive_audit_observation(store, 1)                                      
    T.add_audit_observation(store, "b" * 60, "universal", None, "ev")          
    stale_two = json.load(open(T._audit_file(store, 2)))
    stale_two["notes"] = "STALE COPY"
    stale_two["updated_at"] = "2000-01-01T00:00:00Z"
    fresh_three = {"id": 3, "observation": "c" * 60, "scope": "universal", "status": "open",
                   "created_at": "2026-08-24T00:00:00Z"}
    re_one = {"id": 1, "observation": "a" * 60, "status": "open"}              
    json.dump([stale_two, fresh_three, re_one], open(T._audit_legacy_path(store), "w"))
    got = {o["id"]: o for o in T.list_audit_observations(store)}
    assert set(got) == {2, 3}                       
    assert got[2].get("notes") != "STALE COPY"      
    assert _files(store, archive=True) == ["0001.json"]


def test_observation_files_are_not_indexed_as_entities(store):
    T.add_audit_observation(store, "a" * 60, "universal", None, "ev")
    store.reload()
    assert store.load_errors == []
    assert not [e for e in store.entities.values() if "audit-observations" in str(e.get("_path", ""))]



def test_digest_rows_are_worst_then_oldest_and_include_triaged(store):
    T.add_audit_observation(store, "old normal. more", "universal", None, "ev",
                            observed_date="2026-01-01")
    T.add_audit_observation(store, "new high", "universal", None, "ev", severity="high")
    T.add_audit_observation(store, "later triaged", "project", "P", "ev")
    T.add_audit_observation(store, "gone", "universal", None, "ev")
    T.resolve_audit_observation(store, 3, status="triaged", resolution_note="looking")
    T.resolve_audit_observation(store, 4, resolution_note="done")
    rows = T._audit_digest_rows(store)
    assert [r["id"] for r in rows] == [2, 1, 3]              
    assert rows[1]["needs_reverify"] is True and rows[1]["summary"] == "old normal."
    assert rows[2]["status"] == "triaged" and rows[2]["project"] == "P"
    assert all(r["id"] != 4 for r in rows)


def test_digest_body_is_a_request_doc_with_one_row_per_observation(store):
    T.add_audit_observation(store, "x" * 30, "universal", None, "ev")
    d = T.audit_digest(store, "Laptop")
    assert d["path"] == "inbox/machines/Laptop/audit-digest.md"
    assert "1 observation(s)" in d["title"]
    assert d["body"].startswith("---\nfrom: agent-context (rendered live)\nto: Laptop\n")
    assert "| #1 | normal | open |" in d["body"]
    assert T.audit_digest(store, "Laptop")["rows"] == d["rows"]
