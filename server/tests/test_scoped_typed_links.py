'Exact cross-scope targets for typed context-graph relations.'

from agent_context import fstools as T


def _project(store, name):
    T.upsert_project(store, f"gh:org/{name.lower()}", name)


def _cards(result):
    assert "error" not in result, result
    return result["links"]


def test_qualified_project_target_selects_the_named_project_and_kind(store):
    _project(store, "App")
    _project(store, "Api")
    T.upsert_doc(store, "auth.md", "app\n", project="App", title="App auth")
    T.upsert_doc(store, "auth.md", "api\n", project="Api", title="API auth")
    written = T.upsert_doc(
        store, "client.md", "client\n", project="App", title="Client",
        links={"client_of": ["project:Api::doc:auth.md"]},
    )

    assert "error" not in written, written
    assert "warning" not in written, written
    assert any("→ client_of doc auth.md" in c and "API auth" in c
               for c in _cards(T.get_doc(store, "client.md", project="App")))
    assert any("← client_of doc client.md" in c
               for c in _cards(T.get_doc(store, "auth.md", project="Api")))
    assert not any("client_of" in c for c in _cards(T.get_doc(store, "auth.md", project="App")))


def test_qualified_global_target_can_share_the_sources_name(store):
    _project(store, "App")
    T.upsert_script(store, "wt-sweep", "global\n", description="global implementation")
    T.upsert_script(store, "wt-sweep", "shim\n", project="App", description="app shim")

    result = T.set_entity_links(store, "script", "wt-sweep",
                                {"depends_on": ["global::script:wt-sweep"]}, project="App")

    assert "error" not in result, result
    assert "warning" not in result, result
    app_view = T.explore(store, "script", "wt-sweep", project="App")
    global_view = T.explore(store, "script", "wt-sweep")
    assert app_view is not None and global_view is not None
    assert any("→ depends_on script wt-sweep" in c and "global implementation" in c
               for c in app_view["cards"])
    assert any("← depends_on script wt-sweep" in c and "app shim" in c
               for c in global_view["cards"])


def test_qualified_workspace_target_is_exact(store):
    T.upsert_project(store, "gh:org/app", "App", workspace="W")
    T.upsert_doc(store, "policy.md", "workspace\n", workspace="W", title="W policy")
    T.upsert_doc(store, "policy.md", "global\n", title="Global policy")
    written = T.upsert_memory(store, "rule", "reference", "rule", "body\n", project="App",
                              links={"depends_on": ["workspace:W::doc:policy.md"]})

    assert "error" not in written, written
    cards = _cards(T.get_memory(store, "rule", project="App"))
    assert any("→ depends_on doc policy.md" in c and "W policy" in c for c in cards)
    assert not any("Global policy" in c for c in cards)


def test_missing_qualified_target_warns_and_is_reported(store):
    _project(store, "App")
    result = T.upsert_doc(store, "client.md", "body\n", project="App", title="Client",
                          links={"client_of": ["project:Missing::doc:auth.md"]})

    assert "project:Missing::doc:auth.md" in result.get("warning", ""), result
    assert not any("client_of" in c for c in _cards(T.get_doc(store, "client.md", project="App")))
    findings = T.check_integrity(store)
    assert any(row["target"] == "project:Missing::doc:auth.md"
               for row in findings["dangling_typed_links"])


def test_malformed_qualified_target_is_refused_without_a_write(store):
    _project(store, "App")
    for target in ("project:Api::auth.md", "project:::doc:auth.md",
                   "workspace:W::bogus:auth.md", "global::doc:"):
        result = T.upsert_doc(store, "client.md", "body\n", project="App", title="Client",
                              links={"sibling": [target]})
        assert "error" in result, (target, result)
        assert T.get_doc(store, "client.md", project="App") is None


def test_explicit_self_link_is_refused_but_other_scope_same_key_is_allowed(store):
    _project(store, "App")
    _project(store, "Api")
    T.upsert_doc(store, "same.md", "other\n", project="Api", title="Other")
    self_link = T.upsert_doc(store, "same.md", "self\n", project="App", title="Self",
                             links={"sibling": ["project:App::doc:same.md"]})
    assert "error" in self_link, self_link
    assert T.get_doc(store, "same.md", project="App") is None

    other_link = T.upsert_doc(store, "same.md", "self\n", project="App", title="Self",
                              links={"sibling": ["project:Api::doc:same.md"]})
    assert "error" not in other_link, other_link
    assert "warning" not in other_link, other_link


def test_legacy_bare_links_and_explicit_body_pointers_keep_their_behavior(store):
    _project(store, "App")
    _project(store, "Api")
    T.upsert_doc(store, "auth.md", "app\n", project="App", title="App auth")
    T.upsert_doc(store, "auth.md", "api\n", project="Api", title="API auth")
    T.upsert_doc(store, "client.md", 'get_doc("auth.md", "Api")\n', project="App",
                 title="Client", links={"sibling": ["auth.md"]})

    cards = _cards(T.get_doc(store, "client.md", project="App"))
    assert any("→ sibling doc auth.md" in c and "App auth" in c for c in cards)
    assert any("→ mentions doc auth.md" in c and "API auth" in c for c in cards)
