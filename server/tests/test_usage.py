"Read-side telemetry: counting, isolation, persistence, and the guards that keep\na cold-slot report from gutting an index it has no evidence about.\n\nEvery test points AGENT_CONTEXT_USAGE_FILE at a tmp path and resets the module's\nin-memory state, so nothing here touches this machine's real counters."
import time

import pytest

from agent_context import fstools as T
from agent_context import usage


@pytest.fixture
def counters(tmp_path, monkeypatch):
    'Isolated, empty telemetry with the write-behind timer disabled.'
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})
    monkeypatch.setattr(usage, "_FLUSH_SECS", 0.0)
    return usage


def test_reads_and_hits_are_counted_separately(counters):
    counters.record("memory", "global", "slug-a", read=True)
    counters.record("memory", "global", "slug-a", hit=True)
    counters.record("memory", "global", "slug-b", hit=True)
    assert counters.stats("memory", "global", "slug-a") == {
        "reads": 1, "hits": 1, "last_read": pytest.approx(time.time(), abs=10)}
    b = counters.stats("memory", "global", "slug-b")
    assert (b["reads"], b["hits"]) == (0, 1)
    assert counters.stats("memory", "global", "never")["reads"] == 0


def test_scope_is_part_of_the_key(counters):
    'The same slug at two scopes is two different memories; conflating them would\n    make a project memory look warm because a global one was read.'
    counters.record("memory", "global", "dup", read=True)
    assert counters.stats("memory", "project:P", "dup")["reads"] == 0


def test_counts_survive_a_restart(counters, monkeypatch):
    counters.record("doc", "global", "x.md", read=True)
    counters.flush()
    monkeypatch.setattr(usage, "_data", None)          
    assert counters.stats("doc", "global", "x.md")["reads"] == 1


def test_a_flush_merges_with_the_file_instead_of_overwriting_it(counters, tmp_path):
    'test a flush merges with the file instead of overwriting it.'
    import json
    f = tmp_path / "usage.json"
    counters.record("memory", "global", "mine", read=True)
    counters.flush()

    
    on_disk = json.loads(f.read_text())
    on_disk["e"]["memory\tglobal\ttheirs"] = {"r": 5, "h": 2, "t": 1.0}
    f.write_text(json.dumps(on_disk))

    counters.record("memory", "global", "mine", read=True)
    counters.flush()

    got = json.loads(f.read_text())["e"]
    assert got["memory\tglobal\ttheirs"]["r"] == 5, "the other process's counts were clobbered"
    assert got["memory\tglobal\tmine"]["r"] == 2, "our own two reads must both survive"


def test_an_unreadable_file_does_not_restart_the_observation_window(tmp_path, monkeypatch):
    "A fresh `since` is indistinguishable from 'tracked for a month and nothing read\n    this', and every tiering decision keys off that difference. A file that exists but\n    cannot be parsed keeps counting from its mtime rather than from now."
    import os
    f = tmp_path / "usage.json"
    f.write_text("{ truncated not json")
    a_day_ago = time.time() - 86400
    os.utime(f, (a_day_ago, a_day_ago))
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(f))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_pending", {})

    assert usage.tracking_since() == pytest.approx(a_day_ago, abs=2)


def test_a_tmpdir_window_start_never_reaches_the_live_forensic_log(tmp_path,
                                                                   monkeypatch):
    'The rule now is that the subject and the record must share a regime: a tmpdir usage\n    file written to a NON-tmp log is a test leaking into production. (The hermetic case\n    — both under tmp — is what test_usage_window_forensics.py exercises, and it must\n    keep working; that is the other half of this behavior.)'
    monkeypatch.delenv("AGENT_CONTEXT_USAGE_FILE", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)          
    monkeypatch.setattr(usage, "_state_dir", lambda: tmp_path)   

    written = []
    real_open = open

    def spy(path, *a, **kw):
        written.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", spy)
    usage._forensics("absent", time.time())
    monkeypatch.undo()

    assert not any("forensics" in p for p in written), (
        f"a tmpdir window start was recorded in the live log: {written!r}")


def test_telemetry_never_raises_on_a_bad_path(tmp_path, monkeypatch):
    'A read must not fail because telemetry cannot write.'
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "nope" / "deep" / "u.json"))
    monkeypatch.setattr(usage, "_data", None)
    usage.record("memory", "global", "s", read=True)   
    usage.flush()


def test_get_memory_records_a_read_and_search_records_hits(store, counters):
    T.upsert_memory(store, "warm", "reference", "d", "body about pelicans", project=None)
    assert counters.stats("memory", "global", "warm")["reads"] == 0

    T.get_memory(store, "warm")
    assert counters.stats("memory", "global", "warm")["reads"] == 1

    T.search_memories(store, "pelicans")
    st = counters.stats("memory", "global", "warm")
    assert (st["reads"], st["hits"]) == (1, 1)         


def test_cold_report_is_silent_until_the_window_is_long_enough(store, counters):
    'The single most important guard here: a freshly instrumented machine has no\n    evidence about anything, and reporting every memory as cold would invite an\n    audit pass to demote the whole index.'
    T.upsert_memory(store, "untouched", "reference", "d", "b", project=None)
    assert usage.tracking_since() == pytest.approx(time.time(), abs=10)
    assert T._cold_always_loaded(store) == []


def test_cold_report_flags_only_unread_always_loaded_memories(store, counters, monkeypatch):
    T.upsert_memory(store, "cold-one", "reference", "d", "b", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "read-one", "reference", "d", "b", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "lazy-one", "reference", "d", "b", project=None,
                    load_behavior="lazy")
    T.get_memory(store, "read-one")

    
    old = time.time() - (T._COLD_MIN_DAYS + 5) * 86400
    monkeypatch.setattr(usage, "tracking_since", lambda: old)
    for e in store.entities.values():
        if e.get("type") == "memory":
            e["updated_at"] = "2000-01-01T00:00:00Z"

    slugs = {d["slug"] for d in T._cold_always_loaded(store)}
    assert "cold-one" in slugs          
    assert "read-one" not in slugs      
    assert "lazy-one" not in slugs      


def test_a_search_hit_alone_keeps_a_memory_out_of_the_cold_list(store, counters, monkeypatch):
    'Findable-and-relevant is use. Demoting on body-fetches alone would punish\n    memories that do their job by surfacing in search.'
    T.upsert_memory(store, "matched", "reference", "d", "body about pelicans", project=None)
    T.search_memories(store, "pelicans")
    old = time.time() - (T._COLD_MIN_DAYS + 5) * 86400
    monkeypatch.setattr(usage, "tracking_since", lambda: old)
    for e in store.entities.values():
        if e.get("type") == "memory":
            e["updated_at"] = "2000-01-01T00:00:00Z"
    assert T._cold_always_loaded(store) == []


def test_usage_report_is_coldest_first_and_qualifies_its_zeros(store, counters):
    T.upsert_memory(store, "hot", "reference", "d", "b", project=None)
    T.upsert_memory(store, "cold", "reference", "d", "b", project=None)
    T.get_memory(store, "hot")

    rep = T.usage_report(store)
    assert rep["enough_evidence"] is False        
    assert rep["memories"] == 2
    assert rep["coldest_first"][0]["slug"] == "cold"
    assert rep["coldest_first"][0]["reads"] == 0
    assert rep["coldest_first"][-1]["slug"] == "hot"
    assert rep["always_loaded_bytes"] > 0



def _snapshot(store, machine, since_days_ago, entries):
    import json
    import os
    d = os.path.join(store.root, "machines", machine)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "usage.json"), "w") as fh:
        json.dump({"since": time.time() - since_days_ago * 86400, "e": entries}, fh)


def test_fleet_view_sums_every_machine_and_takes_the_oldest_window(store, counters):
    'test fleet view sums every machine and takes the oldest window.'
    T.upsert_memory(store, "shared", "reference", "d", "b", project=None)
    key = "memory\tglobal\tshared"
    _snapshot(store, "machine-a", 40, {key: {"r": 2, "h": 1, "t": 1}})
    _snapshot(store, "machine-b", 3, {key: {"r": 1, "h": 0, "t": 2}})
    counters.record("memory", "global", "shared", hit=True)      

    rep = T.usage_report(store)
    assert rep["machines_reporting"] == 3                        
    assert rep["tracking_days"] == pytest.approx(40, abs=0.1)    
    assert rep["enough_evidence"] is True
    row = next(r for r in rep["coldest_first"] if r["slug"] == "shared")
    assert (row["reads"], row["hits"]) == (3, 2)

    mine = T.usage_report(store, per_machine=True)
    assert mine["machines_reporting"] == 1
    assert mine["enough_evidence"] is False
    assert next(r for r in mine["coldest_first"] if r["slug"] == "shared")["reads"] == 0


def test_missing_or_corrupt_snapshots_are_skipped_not_fatal(store, counters):
    import os
    os.makedirs(os.path.join(store.root, "machines", "empty-dir"))
    os.makedirs(os.path.join(store.root, "machines", "broken"))
    open(os.path.join(store.root, "machines", "broken", "usage.json"), "w").write("{not json")
    open(os.path.join(store.root, "machines", "not-a-dir.toml"), "w").write('type = "machine"\n')
    rep = T.usage_report(store)
    assert rep["machines_reporting"] == 1                        


def test_cold_report_reaches_its_threshold_from_another_machines_window(store, counters):
    'The whole point: no single machine has to stay up for 30 days.'
    T.upsert_memory(store, "cold-one", "reference", "d", "b", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "read-elsewhere", "reference", "d", "b", project=None,
                    load_behavior="always")
    for e in store.entities.values():
        if e.get("type") == "memory":
            e["updated_at"] = "2000-01-01T00:00:00Z"
    assert T._cold_always_loaded(store) == []                    
    _snapshot(store, "machine-a", T._COLD_MIN_DAYS + 5,
              {"memory\tglobal\tread-elsewhere": {"r": 1, "h": 0, "t": 1}})
    slugs = {d["slug"] for d in T._cold_always_loaded(store)}
    assert slugs == {"cold-one"}                                 


def test_publish_snapshot_writes_only_on_change(store, counters, monkeypatch):
    monkeypatch.setattr(usage, "_last_publish", 0.0)
    p = usage.snapshot_path(store.root, "me")
    
    assert usage.publish_snapshot(store.root, "me", force=True) is True
    assert p.exists()
    assert usage.publish_snapshot(store.root, "me", force=True) is False   
    counters.record("doc", "global", "x.md", read=True)
    assert usage.publish_snapshot(store.root, "me", force=True) is True
    import json
    assert json.load(open(p))["e"]["doc\tglobal\tx.md"]["r"] == 1
    
    counters.record("doc", "global", "x.md", read=True)
    assert usage.publish_snapshot(store.root, "me") is False


def test_snapshot_files_are_not_indexed_as_entities(store, counters):
    _snapshot(store, "machine-a", 1, {})
    store.reload()
    assert store.load_errors == []
    assert not [e for e in store.entities.values() if "usage.json" in str(e.get("_path", ""))]
