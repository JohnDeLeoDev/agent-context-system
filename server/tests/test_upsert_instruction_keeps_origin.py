'Calling upsert_instruction again without origin used to overwrite it with None: the\nMCP tool default is origin=None ("not given"), and the old code stored that value\nverbatim instead of carrying the existing one.'
from agent_context import memory as M


def test_upsert_instruction_keeps_origin_when_not_passed(store):
    M.upsert_instruction(store, "Title", "body one", origin="user")
    M.upsert_instruction(store, "Title", "body two")
    e = store.get("instruction", "Title", None)
    assert e.get("origin") == "user"
