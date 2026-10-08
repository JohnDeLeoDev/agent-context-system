'Generated graph links may target Markdown nested inside skills.'

from pathlib import Path

from agent_context import fstools as T


def test_nested_skill_reference_is_a_real_graph_target(store):
    T.upsert_project(store, "github.com:org/p", "P")
    reference = Path(store.root) / "projects" / "P" / "skills" / "guide" / "references" / "detail.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("# Detail\n")
    T.upsert_doc(
        store,
        "P.md",
        "[[projects/P/skills/guide/references/detail]]\n"
        "[[projects/P/skills/guide/references/missing]]\n",
        title="P",
        project="P",
    )
    dangling = {item["target"] for item in T.check_integrity(store)["dangling_links"]}
    assert "projects/P/skills/guide/references/detail" not in dangling
    assert "projects/P/skills/guide/references/missing" in dangling
