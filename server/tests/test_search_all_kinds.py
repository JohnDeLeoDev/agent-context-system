"search_all carries what search_memories and search_docs did (policy).\n\nThe two tools were folded into search_all to bring the MCP roster back inside its token\nbudget (policy) after run_store_task was added. `kind` returns one kind in that kind's own\nrow shape, and `project` limits rows to global plus the project's scope."
from agent_context import docs, memory
from agent_context import fstools as T


def _seed(store):
    memory.upsert_memory(store, "zebra-note", "reference", "zebra stripes", "zebra body")
    docs.upsert_doc(store, "zebra.md", body="all about the zebra", title="Zebra guide")


def test_kind_memory_is_the_old_search_memories(store):
    _seed(store)
    rows = T.search_all(store, "zebra", kind="memory")
    assert rows == memory.search_memories(store, "zebra")
    assert [r["slug"] for r in rows] == ["zebra-note"]


def test_kind_doc_is_the_old_search_docs(store):
    _seed(store)
    rows = T.search_all(store, "zebra", kind="doc")
    assert rows == docs.search_docs(store, "zebra")
    assert [r["path"] for r in rows] == ["zebra.md"]


def test_no_kind_ranks_both_together(store):
    _seed(store)
    kinds = {r["entity_type"] for r in T.search_all(store, "zebra")}
    assert kinds == {"memory", "doc"}


def test_an_unknown_kind_is_an_error(store):
    assert "error" in T.search_all(store, "zebra", kind="skill")
