'Search keywords, doc descriptions, the search-miss log and check_integrity summary.'
from agent_context import fstools as T
from agent_context import usage


def test_memory_keywords_are_searched_and_carried(store):
    T.upsert_memory(store, "op-agent", "reference", "ssh and signing keys", "auth via the agent",
                    project=None, load_behavior="lazy", keywords=["authenticate", "login"])
    T.upsert_memory(store, "other", "reference", "unrelated", "authenticate appears in a body",
                    project=None, load_behavior="lazy")
    res = T.search_memories(store, "authenticate", project=None)
    assert res[0]["slug"] == "op-agent"
    
    T.upsert_memory(store, "op-agent", "reference", "ssh and signing keys", "new body",
                    project=None)
    assert T.get_memory(store, "op-agent")["keywords"] == ["authenticate", "login"]
    assert T.search_memories(store, "login", project=None)[0]["slug"] == "op-agent"


def test_memory_keywords_patch_and_clear(store):
    T.upsert_memory(store, "m", "reference", "desc", "body", project=None, load_behavior="lazy")
    T.set_memory_keywords(store, "m", ["zebra"])
    assert T.search_memories(store, "zebra", project=None)[0]["slug"] == "m"
    T.set_memory_keywords(store, "m", [])
    assert "keywords" not in T.get_memory(store, "m")
    assert T.search_memories(store, "zebra", project=None) == []


def test_doc_description_and_keywords(store):
    T.upsert_doc(store, "guide.md", "body text", title="Guide",
                 description="Read before touching the gateway.", keywords=["quokka"])
    d = T.get_doc(store, "guide.md")
    assert d["description"] == "Read before touching the gateway."
    assert d["keywords"] == ["quokka"]
    row = T.search_docs(store, "quokka", project=None)[0]
    assert row["path"] == "guide.md" and row["title"] == "Guide"
    assert row["description"] == "Read before touching the gateway."
    
    T.upsert_doc(store, "guide.md", "new body")
    assert T.get_doc(store, "guide.md")["description"] == "Read before touching the gateway."
    T.upsert_doc(store, "guide.md", description="")
    assert "description" not in T.get_doc(store, "guide.md")
    assert T.get_doc(store, "guide.md")["keywords"] == ["quokka"]


def test_doc_description_over_limit_is_refused(store):
    out = T.upsert_doc(store, "long.md", "body", description="x" * 141)
    assert "error" in out and T.get_doc(store, "long.md") is None


def test_search_miss_is_logged_and_hit_is_not(store, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("AGENT_CONTEXT_USAGE", "1")
    T.upsert_memory(store, "deploy-note", "reference", "deploy the router", "steps",
                    project=None, load_behavior="lazy")
    T.search_all(store, "deploy router")
    assert usage.misses() == []
    T.search_all(store, "zzzz nothing")
    T.search_all(store, "ZZZZ  nothing")
    rows = usage.misses()
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["rows"] == 0


def test_check_integrity_summary_mode(store):
    T.upsert_memory(store, "m", "reference", "desc", "see [[no-such-target]]",
                    project=None, load_behavior="lazy")
    full = T.check_integrity(store)
    brief = T.check_integrity(store, summary=True)
    assert brief["summary"] == full["summary"]
    assert brief["dangling_links"] == full["dangling_links"] and brief["dangling_links"]
    for key in ("split_candidates", "graph_coverage", "bootstrap_footprint"):
        assert key not in brief
    assert all(v for k, v in brief.items() if k not in ("summary", "omitted"))
