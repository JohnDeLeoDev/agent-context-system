'check_integrity scans descriptions as well as bodies for emphatic capitals.'
from agent_context import fstools as T


def test_capitals_flagged_in_description_of_always_memory(store):
    T.upsert_memory(store, "loud-desc", "feedback", "You MUST read this first", "A clean body.",
                    load_behavior="always")
    rows = T.check_integrity(store)["emphatic_capitals"]
    row = next(r for r in rows if r["key"] == "loud-desc")
    assert row["words"] == {"MUST": 1}


def test_capitals_in_description_of_lazy_memory_not_flagged(store):
    T.upsert_memory(store, "quiet-desc", "feedback", "You MUST read this first", "A clean body.",
                    load_behavior="lazy")
    keys = {r["key"] for r in T.check_integrity(store)["emphatic_capitals"]}
    assert "quiet-desc" not in keys
