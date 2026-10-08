'agent_definition: the storage kind existed from the start; the tool surface did not.'
from agent_context import fstools as T


def test_upsert_and_get_agent_definition(store):
    T.upsert_agent_definition(store, "worker-explore", "Read-only fan-out search",
                              "You search. You do not edit.", model="sonnet",
                              effort="low", tools="Read, Grep, Glob")
    got = T.get_entity(store, "agent_definition", "worker-explore")
    assert got["name"] == "worker-explore"
    assert got["model"] == "sonnet"
    assert got["effort"] == "low"
    assert got["tools"] == "Read, Grep, Glob"
    assert "You search" in got["body"]


def test_omitted_fields_are_carried_forward_not_blanked(store):
    T.upsert_agent_definition(store, "worker-review", "Reviews a diff", "Body v1",
                              model="sonnet", effort="medium", tools="Read, Grep")
    
    T.upsert_agent_definition(store, "worker-review", "Reviews a diff, read-only")
    got = T.get_entity(store, "agent_definition", "worker-review")
    assert got["description"] == "Reviews a diff, read-only"
    assert got["model"] == "sonnet"
    assert got["effort"] == "medium"
    assert got["tools"] == "Read, Grep"
    assert got["body"] == "Body v1"


def test_listed_without_body_and_editable_in_place(store):
    T.upsert_agent_definition(store, "worker-impl", "Implements", "step one\nstep two",
                              model="sonnet", effort="high")
    rows = T.list_entities(store, "agent_definition")
    assert [r["name"] for r in rows] == ["worker-impl"]
    assert "body" not in rows[0]
    assert rows[0]["effort"] == "high"

    T.edit_body(store, "agent_definition", "worker-impl", "step two", "step three")
    assert "step three" in T.get_entity(store, "agent_definition", "worker-impl")["body"]


def test_delete_and_unknown_kind_message(store):
    T.upsert_agent_definition(store, "gone", "temp", "body")
    T.delete_entity(store, "agent_definition", "gone")
    assert T.get_entity(store, "agent_definition", "gone") is None
    err = T.get_entity(store, "nonsense", "x")
    assert "agent_definition" in err["error"]


def test_upsert_skill_syncs_the_body_frontmatter_description(store):
    'test upsert skill syncs the body frontmatter description.'
    body = (
        "---\nname: sk\ndescription: A very long original description that nobody wants\n"
        "  wrapped across two folded YAML lines like the vendored skills do\n"
        "allowed-tools: Read\n---\n\n# sk\n\nbody text\n"
    )
    T.upsert_skill(store, "sk", "original", body)
    T.upsert_skill(store, "sk", "short and correct", None)
    got = T.get_entity(store, "skill", "sk")
    assert got["description"] == "short and correct"
    assert "description: short and correct\n" in got["body"]
    assert "folded YAML lines" not in got["body"]      
    assert "allowed-tools: Read" in got["body"]        
    assert "body text" in got["body"]                  


def test_body_without_frontmatter_is_left_alone(store):
    T.upsert_skill(store, "plain", "desc", "no frontmatter here\n")
    T.upsert_skill(store, "plain", "new desc", None)
    got = T.get_entity(store, "skill", "plain")
    assert got["body"].strip() == "no frontmatter here"  
    assert got["description"] == "new desc"
