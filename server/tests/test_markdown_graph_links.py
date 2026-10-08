'Existing local Markdown documentation links participate in the context graph.'

from agent_context import fstools as T
from agent_context import graph


def test_relative_markdown_links_connect_catalog_to_feature_docs(store):
    T.upsert_doc(store, "features/route.md", "# Route\n", title="Route")
    T.upsert_doc(store, "features/README.md", "[Route](./route.md)\n", title="Features")

    catalog = store.get("doc", "features/README.md")
    route = store.get("doc", "features/route.md")
    g = graph.graph_for(store)

    assert route["uuid"] in g.out[catalog["uuid"]]
    assert "mentions" in g.rels[(catalog["uuid"], route["uuid"])]
    read = T.get_doc(store, "features/README.md")
    assert read is not None
    assert any("features/route.md" in card for card in read["links"])


def test_markdown_links_resolve_parent_paths_and_section_anchors(store):
    T.upsert_doc(store, "features/route.md", "# Route\n## Safety\n", title="Route")
    T.upsert_doc(store, "features/deep/README.md", "[Safety](../route.md#Safety)\n", title="Deep")

    deep = store.get("doc", "features/deep/README.md")
    route = store.get("doc", "features/route.md")
    assert route["uuid"] in graph.graph_for(store).out[deep["uuid"]]
    assert graph.dangling_anchors(store) == []


def test_code_images_and_external_links_do_not_make_doc_edges(store):
    T.upsert_doc(store, "features/route.md", "# Route\n", title="Route")
    T.upsert_doc(store, "features/README.md", """`[inline](./route.md)`
```md
[fenced](./route.md)
```
![image](./route.md)
[external](https://example.com/route.md)
""", title="Features")

    catalog = store.get("doc", "features/README.md")
    assert graph.graph_for(store).out.get(catalog["uuid"], []) == []
