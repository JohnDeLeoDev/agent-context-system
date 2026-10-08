'Context overhaul Phase 1 item 1: lazy is the one load_behavior default.\n\nThe index read a missing field as always while path-derived notes defaulted to lazy,\nso 86 memories were always-loaded only by omission. Now a missing field reads as lazy,\nthe MCP write path refuses a memory with no field, and check_integrity reports one.'
from agent_context import index as I
from agent_context import integrity, memory, shaping


def test_missing_field_reads_as_lazy():
    assert I._is_lazy({"type": "memory"})
    assert I._is_lazy({"type": "memory", "load_behavior": "lazy"})
    assert not I._is_lazy({"type": "memory", "load_behavior": "always"})
    assert shaping._mem({"uuid": "u", "slug": "s"}, body=False)["load_behavior"] == "lazy"


def test_required_write_refuses_new_memory_without_field(store):
    r = memory.upsert_memory(store, "no-tier", "reference", "d", "b",
                             require_load_behavior=True)
    assert "load_behavior is required" in r["error"]
    assert store.get("memory", "no-tier") is None


def test_required_write_accepts_explicit_or_carried_field(store):
    r = memory.upsert_memory(store, "tiered", "reference", "d", "b", load_behavior="always",
                             require_load_behavior=True)
    assert r["load_behavior"] == "always"
    r = memory.upsert_memory(store, "tiered", "reference", "d", "b2",
                             require_load_behavior=True)
    assert "error" not in r
    assert r["load_behavior"] == "always"


def test_invalid_value_refused(store):
    r = memory.upsert_memory(store, "bad", "reference", "d", "b", load_behavior="sometimes")
    assert "must be 'always' or 'lazy'" in r["error"]
    memory.upsert_memory(store, "good", "reference", "d", "b", load_behavior="lazy")
    r = memory.set_memory_load_behavior(store, "good", None)
    assert "error" in r
    assert store.get("memory", "good")["load_behavior"] == "lazy"


def test_integrity_reports_memory_without_field(store):
    memory.upsert_memory(store, "legacy", "reference", "d", "b")
    memory.upsert_memory(store, "explicit", "reference", "d", "b", load_behavior="lazy")
    found = integrity.check_integrity(store)["missing_load_behavior"]
    assert [f["slug"] for f in found] == ["legacy"]
