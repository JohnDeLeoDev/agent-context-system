'store.search ranking quality (build 7): length-normalized, field-weighted,\ncoverage-scored — so agents can lazy-load and still retrieve the RIGHT context.\n\nThe old scoring was raw term counts, which buried short precise memories under long\ndocs that merely repeated a term, and let one-term keyword-spam outrank matching all\nquery terms. These pin the improved behavior.'
from agent_context import fstools as T


def test_short_precise_memory_outranks_long_repetitive_doc(store):
    T.upsert_memory(store, "deploy-note", "reference",
                    "how to deploy the router", "deploy steps here", project=None)
    
    T.upsert_doc(store, "noise.md",
                 ("deploy deploy deploy unrelated filler content. " * 40), title="noise")
    res = T.search_memories(store, "deploy router", project=None)
    assert res and res[0]["slug"] == "deploy-note"


def test_matching_all_terms_beats_one_term_spam(store):
    T.upsert_memory(store, "both", "reference",
                    "alpha and beta together", "alpha beta", project=None)
    T.upsert_memory(store, "spam", "reference",
                    "alpha alpha alpha alpha alpha", "alpha alpha alpha alpha", project=None)
    res = T.search_memories(store, "alpha beta", project=None)
    slugs = [r["slug"] for r in res]
    assert slugs and slugs[0] == "both"


def test_title_hit_outweighs_body_hit(store):
    T.upsert_memory(store, "titlematch", "reference", "gamma keyword here", "unrelated", project=None)
    T.upsert_memory(store, "bodymatch", "reference", "unrelated", "gamma gamma gamma gamma gamma", project=None)
    res = T.search_memories(store, "gamma", project=None)
    assert res[0]["slug"] == "titlematch"


def test_no_match_returns_nothing(store):
    T.upsert_memory(store, "x", "reference", "apples", "oranges", project=None)
    assert T.search_memories(store, "zzzznomatch", project=None) == []


def test_stem_shapes():
    from agent_context.store import _stem
    assert _stem("memories") == "memor"
    assert _stem("memory") == "memor"
    assert _stem("running") == "run"
    assert _stem("deployment") == "deploy"
    assert _stem("uses") == "use"
    assert _stem("was") == "was"        


def test_query_inflections_reach_the_stem(store):
    T.upsert_memory(store, "mem-note", "reference", "memory index budget", "one memory row", project=None)
    T.upsert_memory(store, "run-note", "reference", "how the loop runs", "it runs", project=None)
    assert [r["slug"] for r in T.search_memories(store, "memories", project=None)] == ["mem-note"]
    assert [r["slug"] for r in T.search_memories(store, "running", project=None)] == ["run-note"]
    assert T.search_memories(store, "deployments", project=None) == []


def test_task_shaped_query_ignores_common_words(store):
    T.upsert_memory(store, "op-note", "reference", "1password gateway", "x", project=None)
    T.upsert_memory(store, "noise", "reference", "how to use the thing",
                    "how to use it, how to use it", project=None)
    res = T.search_memories(store, "how to use 1password", project=None)
    assert res and res[0]["slug"] == "op-note"


def test_query_of_only_common_words_still_searches(store):
    T.upsert_memory(store, "noise", "reference", "how to use the thing", "x", project=None)
    assert T.search_memories(store, "how to use", project=None)


def test_live_doc_outranks_its_archive(store):
    T.upsert_doc(store, "archive/zeta-history.md", "zeta zeta", title="zeta zeta history")
    T.upsert_doc(store, "zeta.md", "zeta", title="zeta")
    res = T.search_docs(store, "zeta", project=None)
    assert [r["path"] for r in res][:2] == ["zeta.md", "archive/zeta-history.md"]
