'Patch-style write tools: set_memory_description / edit_memory_body /\nedit_doc_body / append_to_doc — targeted edits without full-body rewrites.'
from agent_context import fstools as T



def test_set_memory_description_preserves_body_type_metadata(store):
    T.upsert_memory(store, "m", "reference", "old desc", "line1\nline2\nline3",
                    project=None, metadata={"k": "v"})
    r = T.set_memory_description(store, "m", "new desc")
    assert "error" not in r
    assert r["description"] == "new desc"
    e = store.get("memory", "m")
    assert e["body"] == "line1\nline2\nline3"   
    assert e["memory_type"] == "reference"      
    assert e["metadata"] == '{"k": "v"}'        
    assert e["description"] == "new desc"


def test_set_memory_description_missing(store):
    assert "error" in T.set_memory_description(store, "ghost", "x")



def test_edit_memory_body_unique_replace(store):
    T.upsert_memory(store, "m", "reference", "d", "alpha beta gamma", project=None)
    r = T.edit_memory_body(store, "m", "beta", "DELTA")
    assert "error" not in r
    assert store.get("memory", "m")["body"] == "alpha DELTA gamma"


def test_edit_memory_body_missing_string(store):
    T.upsert_memory(store, "m", "reference", "d", "alpha beta", project=None)
    r = T.edit_memory_body(store, "m", "zzz", "x")
    assert "error" in r and "not found" in r["error"]
    assert store.get("memory", "m")["body"] == "alpha beta"  


def test_edit_memory_body_non_unique_string(store):
    T.upsert_memory(store, "m", "reference", "d", "x and x again", project=None)
    r = T.edit_memory_body(store, "m", "x", "y")
    assert "error" in r and "not unique" in r["error"]
    assert store.get("memory", "m")["body"] == "x and x again"  


def test_edit_memory_body_missing_memory(store):
    assert "error" in T.edit_memory_body(store, "ghost", "a", "b")



def test_edit_doc_body_unique_replace(store):
    T.upsert_doc(store, "d.md", "one two three", title="D")
    r = T.edit_doc_body(store, "d.md", "two", "TWO")
    assert "error" not in r
    assert store.get("doc", "d.md")["body"] == "one TWO three"


def test_edit_doc_body_non_unique(store):
    T.upsert_doc(store, "d.md", "dup dup", title="D")
    assert "not unique" in T.edit_doc_body(store, "d.md", "dup", "x")["error"]


def test_edit_doc_body_preserves_extra_frontmatter(store):
    
    T.upsert_doc(store, "inbox/x.md", "hello world", title="X")
    store.get("doc", "inbox/x.md")["status"] = "open"
    
    T.edit_doc_body(store, "inbox/x.md", "world", "there")
    e = store.get("doc", "inbox/x.md")
    assert e["body"] == "hello there"
    assert e.get("status") == "open"   



def test_append_to_doc_adds_newline(store):
    T.upsert_doc(store, "d.md", "first line", title="D")
    T.append_to_doc(store, "d.md", "second line")
    assert store.get("doc", "d.md")["body"] == "first line\nsecond line"


def test_append_to_doc_when_trailing_newline(store):
    
    
    T.upsert_doc(store, "notes.txt", "first\n", title="Notes")
    T.append_to_doc(store, "notes.txt", "second")
    assert store.get("doc", "notes.txt")["body"] == "first\nsecond"


def test_append_to_doc_missing(store):
    assert "error" in T.append_to_doc(store, "ghost.md", "x")


def test_edit_instruction_body_unique_replace(store):
    T.upsert_instruction(store, "Rules", "alpha beta gamma", load_behavior="always")
    r = T.edit_instruction_body(store, "Rules", "beta", "BETA")
    assert "error" not in r
    assert store.get("instruction", "Rules")["body"] == "alpha BETA gamma"


def test_edit_instruction_body_non_unique_and_missing(store):
    T.upsert_instruction(store, "Rules", "dup dup", load_behavior="always")
    assert "not unique" in T.edit_instruction_body(store, "Rules", "dup", "x")["error"]
    assert "not found" in T.edit_instruction_body(store, "Nope", "a", "b")["error"]


def test_edit_instruction_body_preserves_load_behavior(store):
    
    T.upsert_instruction(store, "Rules", "hello world", load_behavior="always", sort_order=5)
    T.edit_instruction_body(store, "Rules", "world", "there")
    e = store.get("instruction", "Rules")
    assert e["body"] == "hello there"
    assert e.get("load_behavior") == "always"
