'Scoped graph relations must also be links to real Obsidian vault files.'

from agent_context import fstools as T


def test_scoped_doc_link_is_written_as_vault_path_and_still_resolves(store, tmp_path):
    T.upsert_project(store, "gh:org/app", "App")
    T.upsert_project(store, "gh:org/api", "Api")
    T.upsert_doc(store, "features/auth.md", "API contract\n", project="Api")
    result = T.upsert_doc(
        store, "client.md", "App client\n", project="App",
        links={"client_of": ["project:Api::doc:features/auth.md"]},
    )

    assert "error" not in result, result
    source = tmp_path / "ctx/projects/App/docs/client.md"
    target = tmp_path / "ctx/projects/Api/docs/features/auth.md"
    assert target.is_file()
    raw = source.read_text()
    assert "[[projects/Api/docs/features/auth.md]]" in raw
    assert "project:Api::doc:features/auth.md" not in raw
    view = T.get_doc(store, "client.md", project="App")
    assert view is not None
    assert any("→ client_of doc features/auth.md" in card for card in view["links"])


def test_vault_path_input_selects_exact_project_even_with_same_key(store):
    T.upsert_project(store, "gh:org/app", "App")
    T.upsert_project(store, "gh:org/api", "Api")
    T.upsert_doc(store, "auth.md", "local\n", project="App", title="Local")
    T.upsert_doc(store, "auth.md", "server\n", project="Api", title="Server")
    T.upsert_doc(store, "client.md", "client\n", project="App", title="Client")

    result = T.set_entity_links(
        store, "doc", "client.md",
        {"client_of": ["projects/Api/docs/auth.md"]}, project="App",
    )

    assert "error" not in result, result
    assert "warning" not in result, result
    view = T.get_doc(store, "client.md", project="App")
    assert view is not None
    cards = view["links"]
    assert any("→ client_of doc auth.md" in card and "Server" in card for card in cards)
    assert not any("client_of" in card and "Local" in card for card in cards)
