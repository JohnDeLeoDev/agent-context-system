"Round-trip persistence + CRUD lifecycle for fstools entity types the existing\nsuite doesn't cover directly (memory/doc/skill/command create→get→list→delete).\n\nThe round-trip tests upsert entities with content designed to STRESS frontmatter\nserialization — colons, quotes, `#`, a leading `---` fence, tabs, multiline, and\nunicode — then force a fresh read from disk via `store.reload()` and assert every\nfield survives. The serialize→file→parse path is exactly where escaping/frontmatter\nbugs bite, and it was previously exercised only incidentally. `fstools.py` is the\nlargest module in the server; this closes its biggest untested behaviors."
from narrow import notnone

from agent_context import fstools as T


TRICKY_DESC = 'Colon: here, "quotes", a #hash, an — em-dash, and © unicode'
TRICKY_BODY = (
    "---\n"                              
    "key: value  # not real frontmatter\n"
    "## heading\n"
    "a [[wikilink-target]] and `code: with colon`\n"
    "trailing\ttabs and spaces   \n"
    "final ünïcode line ✅"
)





def test_memory_roundtrips_through_disk(store):
    T.upsert_memory(store, "rt-mem", "reference", TRICKY_DESC, TRICKY_BODY, project=None)
    store.reload()  
    m = T.get_memory(store, "rt-mem", project=None)
    assert m is not None
    assert m["description"] == TRICKY_DESC
    assert m["body"] == TRICKY_BODY
    assert m["memory_type"] == "reference"


def test_doc_roundtrips_through_disk(store):
    T.upsert_doc(store, "rt-doc.md", TRICKY_BODY, title=TRICKY_DESC)
    store.reload()
    d = T.get_doc(store, "rt-doc.md", project=None)
    assert d is not None
    assert d["title"] == TRICKY_DESC
    assert d["body"] == TRICKY_BODY


def test_command_roundtrips_through_disk(store):
    T.upsert_command(store, "rt-cmd", TRICKY_BODY, description=TRICKY_DESC)
    store.reload()
    c = T.get_command(store, "rt-cmd", project=None)
    assert c is not None
    assert c["description"] == TRICKY_DESC
    assert c["body"] == TRICKY_BODY


def test_skill_roundtrips_and_lists(store):
    T.upsert_skill(store, "rt-skill", TRICKY_DESC, TRICKY_BODY, project=None)
    store.reload()
    s = T.get_skill(store, "rt-skill", project=None)
    assert s is not None
    assert s["description"] == TRICKY_DESC
    assert s["body"] == TRICKY_BODY
    assert "rt-skill" in {x["name"] for x in T.list_skills(store)}





def test_delete_memory_removes_and_stays_gone(store):
    T.upsert_memory(store, "gone", "reference", "d", "body", project=None)
    assert T.get_memory(store, "gone", project=None) is not None
    T.delete_memory(store, "gone", project=None)
    assert T.get_memory(store, "gone", project=None) is None
    store.reload()
    assert T.get_memory(store, "gone", project=None) is None  


def test_delete_doc_removes_and_stays_gone(store):
    T.upsert_doc(store, "trash.md", "body", title="t")
    assert T.get_doc(store, "trash.md", project=None) is not None
    T.delete_doc(store, "trash.md", project=None)
    store.reload()
    assert T.get_doc(store, "trash.md", project=None) is None


def test_command_crud_lifecycle(store):
    assert T.get_command(store, "clife", project=None) is None
    T.upsert_command(store, "clife", "the body", description="desc")
    got = notnone(T.get_command(store, "clife", project=None))
    assert got["body"] == "the body"
    assert got["description"] == "desc"
    assert "clife" in {c["name"] for c in T.list_commands(store)}
    T.delete_command(store, "clife", project=None)
    assert T.get_command(store, "clife", project=None) is None


def test_reupsert_does_not_persist_private_index_keys(store):
    'test reupsert does not persist private index keys.'
    from agent_context import fstools as T
    T.upsert_doc(store, "x.md", "one", project=None)
    e = store.get("doc", "x.md", None)
    assert "_mtime" in e  
    
    T.edit_doc_body(store, "x.md", "one", "two", project=None)
    path = store.get("doc", "x.md", None)["_path"]
    head = open(path, encoding="utf-8").read().split("---")[1]
    assert "_mtime" not in head and "_path" not in head


def test_hook_description_roundtrips_and_an_omitted_one_is_kept(store):
    T.upsert_hook(store, "h1", "PreToolUse", "#!/bin/sh\nexit 0\n", description="blocks X")
    assert notnone(T.get_hook(store, "h1"))["description"] == "blocks X"
    assert [h["description"] for h in T.list_hooks(store)] == ["blocks X"]
    T.upsert_hook(store, "h1", "PreToolUse", "#!/bin/sh\nexit 1\n")   
    store.reload()
    h = notnone(T.get_hook(store, "h1"))
    assert h["description"] == "blocks X"
    assert "exit 1" in h["script_body"]



def test_hook_metadata_only_upsert_keeps_the_body(store):
    T.upsert_hook(store, "h2", "Stop", "#!/bin/sh\necho keep\n", description="v1")
    r = notnone(T.upsert_hook(store, "h2", "Stop", None, description="v2"))
    assert r["description"] == "v2" and "echo keep" in r["script_body"]
    assert "error" in notnone(T.upsert_hook(store, "nope", "Stop", None, description="x"))
