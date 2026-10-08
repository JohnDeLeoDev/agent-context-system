'Context graph T4: unaddressable_large_bodies skips docs that are not markdown.'
from agent_context import fstools as T

KB16 = 16 * 1024


def _flat_keys(store):
    rows = T.check_integrity(store)["unaddressable_large_bodies"]
    return sorted((r["type"], r["key"]) for r in rows)


def test_large_docs_that_are_not_markdown_are_not_reported(store):
    flat = "x" * (KB16 + 1)
    T.upsert_doc(store, "bench/data.json", flat, title="Benchmark data")
    T.upsert_doc(store, "parity/SIMULATORS.txt", flat, title="Simulators")
    T.upsert_doc(store, "notes/upper.MD", flat, title="Upper-case extension")
    T.upsert_doc(store, "notes/plain.md", flat, title="Plain")

    assert _flat_keys(store) == [("doc", "notes/plain.md"), ("doc", "notes/upper.MD")]
