'Context graph T2: link cards on reads (criteria 1 to 6, 9, 11).'
import json

import pytest

from agent_context import fstools as T
from agent_context import usage


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    'Empty, isolated read counters: card order depends on them.'
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _fmt(n):
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _size(store, kind, key, scope="global"):
    e = store.get(kind, key, scope=scope)
    assert e is not None, (kind, key, scope)
    return _fmt(len((e.get("body") or "").encode()))


def _mem(store, slug, body, desc=None, project=None, workspace=None):
    return T.upsert_memory(store, slug, "reference", desc or f"about {slug}", body,
                           project=project, workspace=workspace)


def _links(result):
    assert result is not None, "read returned nothing"
    assert "links" in result, f"no links field in {sorted(result)}"
    return result["links"]


def _key_of(card):
    return card.split(" ")[3]




def test_memory_read_carries_out_and_back_cards_in_the_documented_format(store):
    _mem(store, "target", "x" * 9830, desc="the target")
    _mem(store, "source", "see [[target]]\n", desc="the source")

    assert _size(store, "memory", "target") == "9.6 KB"
    assert _links(T.get_memory(store, "source")) == [
        "→ mentions memory target (9.6 KB): the target"]
    assert _links(T.get_memory(store, "target")) == [
        f"← mentions memory source ({_size(store, 'memory', 'source')}): the source"]


def test_doc_skill_and_command_reads_carry_cards(store):
    _mem(store, "m", "memory body\n", desc="a memory")
    T.upsert_doc(store, "guides/a.md", "read [[m]]\n", title="Guide A")
    T.upsert_skill(store, "sk", "a skill", "step one: [[m]]\n")
    T.upsert_command(store, "cmd", "run it, then [[guides/a.md]]\n", description="a command")

    m_card = f"→ mentions memory m ({_size(store, 'memory', 'm')}): a memory"
    cmd_back = f"← mentions command cmd ({_size(store, 'command', 'cmd')}): a command"
    assert _links(T.get_doc(store, "guides/a.md")) == [m_card, cmd_back]
    assert _links(T.get_entity(store, "doc", "guides/a.md")) == [m_card, cmd_back]
    assert _links(T.get_entity(store, "skill", "sk")) == [m_card]
    assert _links(T.get_entity(store, "command", "cmd")) == [
        f"→ mentions doc guides/a.md ({_size(store, 'doc', 'guides/a.md')}): Guide A"]
    assert sorted(_links(T.get_entity(store, "memory", "m"))) == sorted([
        f"← mentions doc guides/a.md ({_size(store, 'doc', 'guides/a.md')}): Guide A",
        f"← mentions skill sk ({_size(store, 'skill', 'sk')}): a skill"])


def test_instructions_hooks_and_scripts_carry_no_cards_but_instructions_are_nodes(store):
    _mem(store, "m", "b\n")
    T.upsert_instruction(store, "Rules", "follow [[m]]\n", project=None)
    T.upsert_script(store, "scr", "#!/bin/sh\n# see [[m]]\n", description="a script")
    T.upsert_hook(store, "hk", "PreToolUse", "#!/bin/sh\nexit 0\n", description="a hook")

    assert _links(T.get_memory(store, "m")) == [
        f"← mentions instruction Rules ({_size(store, 'instruction', 'Rules')})"]
    assert all("links" not in i for i in T.get_instructions(store))
    assert "links" not in T.get_entity(store, "script", "scr")
    assert "links" not in T.get_entity(store, "hook", "hk")




def _two_ledgers(store):
    T.upsert_project(store, "gh:org/mobileapi", "example-api")
    T.upsert_project(store, "gh:org/other", "Other")
    T.upsert_doc(store, "maintainability/LEDGER.md", "m ledger\n", title="example-api ledger",
                 project="example-api")
    T.upsert_doc(store, "maintainability/LEDGER.md", "o ledger\n", title="Other ledger",
                 project="Other")


def test_a_project_link_resolves_to_its_own_projects_copy(store):
    _two_ledgers(store)
    _mem(store, "api-note", "see [[maintainability/LEDGER.md]]\n", project="example-api")

    links = _links(T.get_memory(store, "api-note", project="example-api"))

    assert len(links) == 1 and links[0].endswith(": example-api ledger"), links


def test_a_pointer_with_a_project_argument_resolves_in_that_project(store):
    'test a pointer with a project argument resolves in that project.'
    T.upsert_project(store, "gh:org/mobileapi", "example-api")
    T.upsert_project(store, "gh:org/mobileapp", "example-app")
    T.upsert_doc(store, "maintainability/LEDGER.md", "api\n", title="example-api ledger",
                 project="example-api")
    T.upsert_doc(store, "maintainability/LEDGER.md", "app\n", title="example-app ledger",
                 project="example-app")
    _mem(store, "api-note", 'compare `get_doc("maintainability/LEDGER.md", "example-app")`\n',
         project="example-api")
    _mem(store, "g-note", 'see `get_doc("maintainability/LEDGER.md", "example-api")`\n')

    api = _links(T.get_memory(store, "api-note", project="example-api"))
    glob = _links(T.get_memory(store, "g-note"))

    assert len(api) == 1 and api[0].endswith(": example-app ledger"), api
    assert len(glob) == 1 and glob[0].endswith(": example-api ledger"), glob


def test_an_ambiguous_global_link_makes_no_edge_and_is_reported(store):
    _two_ledgers(store)
    _mem(store, "g-note", 'see [[maintainability/LEDGER.md]] or get_doc("maintainability/LEDGER.md")\n')

    assert _links(T.get_memory(store, "g-note")) == []
    f = T.check_integrity(store)
    hits = [h for h in f.get("ambiguous_links", []) if h.get("source") == "g-note"]
    assert hits, f.get("ambiguous_links")
    assert all(h["target"] == "maintainability/LEDGER.md" for h in hits), hits
    assert set(hits[0]["candidates"]) == {"project:example-api", "project:Other"}, hits
    assert f["summary"].get("ambiguous_links", 0) >= 1


def test_a_project_link_prefers_its_workspace_then_global(store):
    T.upsert_project(store, "gh:org/p", "P", workspace="W")
    T.upsert_doc(store, "board.md", "ws\n", title="Workspace board", workspace="W")
    T.upsert_doc(store, "board.md", "g\n", title="Global board")
    _mem(store, "g-only", "b\n", desc="global only")
    _mem(store, "p-note", "see [[board.md]] and [[g-only]]\n", project="P")

    links = _links(T.get_memory(store, "p-note", project="P"))

    assert sorted(links) == sorted([
        f"→ mentions doc board.md ({_size(store, 'doc', 'board.md', scope='ws:W')}): Workspace board",
        f"→ mentions memory g-only ({_size(store, 'memory', 'g-only')}): global only"]), links


def test_a_global_link_falls_back_to_a_unique_match_in_any_scope(store):
    T.upsert_project(store, "gh:org/p", "P")
    _mem(store, "p-only", "b\n", desc="only in P", project="P")
    _mem(store, "g-src", "see [[p-only]]\n")

    assert _links(T.get_memory(store, "g-src")) == [
        f"→ mentions memory p-only ({_size(store, 'memory', 'p-only', scope='project:P')}): only in P"]


def test_a_project_link_never_resolves_into_another_project(store):
    T.upsert_project(store, "gh:org/p", "P")
    T.upsert_project(store, "gh:org/q", "Q")
    _mem(store, "q-only", "b\n", project="Q")
    _mem(store, "p-src", "see [[q-only]]\n", project="P")

    assert _links(T.get_memory(store, "p-src", project="P")) == []




def test_cards_cap_at_twelve_with_out_links_first_by_backlink_count(store):
    for i in range(14):
        _mem(store, f"t{i:02}", "b\n")
    _mem(store, "hub", "".join(f"[[t{i:02}]]\n" for i in range(14)))
    for i in range(3):
        _mem(store, f"x{i}", "[[t05]]\n")
    for i in range(2):
        _mem(store, f"y{i}", "[[t09]]\n")
    _mem(store, "fan", "[[hub]]\n")

    links = _links(T.get_memory(store, "hub"))

    assert all(c.startswith("→ ") for c in links[:12]), links
    assert [_key_of(c) for c in links[:12]] == [
        "t05", "t09", "t00", "t01", "t02", "t03", "t04", "t06", "t07", "t08", "t10", "t11"]
    assert links[12:] == ['+3 more: explore("memory", "hub")']


def test_backlinks_follow_out_links_ordered_by_reads(store):
    _mem(store, "out-one", "b\n")
    _mem(store, "center", "[[out-one]]\n")
    for s in ("r-a", "r-b", "r-c"):
        _mem(store, s, "[[center]]\n")
    for _ in range(3):
        T.get_memory(store, "r-c")
    T.get_memory(store, "r-a")

    links = _links(T.get_memory(store, "center"))

    assert [c.split(" ")[0] + " " + _key_of(c) for c in links] == [
        "→ out-one", "← r-c", "← r-a", "← r-b"]


def test_no_overflow_line_at_exactly_twelve(store):
    for i in range(12):
        _mem(store, f"t{i:02}", "b\n")
    _mem(store, "hub", "".join(f"[[t{i:02}]]\n" for i in range(12)))

    links = _links(T.get_memory(store, "hub"))

    assert len(links) == 12 and not any(c.startswith("+") for c in links), links




def test_no_false_edges(store):
    for slug in ("real-one", "in-code", "in-fence", "fence-ptr", "rider-mcp-tools",
                 "anchored", "aliased"):
        _mem(store, slug, "b\n")
    body = (
        'prose [[real-one]], again [[real-one]], and get_memory("real-one")\n'
        "inline `[[in-code]]` span\n"
        '```\n[[in-fence]]\nget_memory("fence-ptr")\n```\n'
        'placeholders [[<slug>]], get_memory("<slug>"), get_doc("...")\n'
        "prose-shaped [[Rider MCP tools]]\n"
        'self [[src]] and get_memory("src")\n'
        "anchored [[anchored#Some Heading]] and aliased [[aliased|shown text]]\n")
    _mem(store, "src", body)

    links = _links(T.get_memory(store, "src"))

    assert sorted(_key_of(c) for c in links) == ["aliased", "anchored", "real-one"], links




def test_pointer_calls_make_mentions_edges(store):
    _mem(store, "m", "b\n", desc="a memory")
    T.upsert_doc(store, "guides/a.md", "doc\n", title="Guide A")
    T.upsert_skill(store, "sk", "a skill", "body\n")
    _mem(store, "src", 'Read `get_doc("guides/a.md")`, then get_memory(\'m\') and '
                       '`get_entity("skill", "sk")`.\n')

    assert sorted(_links(T.get_memory(store, "src"))) == sorted([
        f"→ mentions doc guides/a.md ({_size(store, 'doc', 'guides/a.md')}): Guide A",
        f"→ mentions memory m ({_size(store, 'memory', 'm')}): a memory",
        f"→ mentions skill sk ({_size(store, 'skill', 'sk')}): a skill"])




def test_a_dangling_target_makes_no_card_and_is_still_reported(store):
    _mem(store, "src", 'see [[nope-missing]] and get_doc("missing.md")\n')

    assert _links(T.get_memory(store, "src")) == []
    f = T.check_integrity(store)
    assert any(d["target"] == "nope-missing" for d in f["dangling_links"]), f["dangling_links"]
    assert any(p["target"] == "missing.md" for p in f["broken_pointers"]), f["broken_pointers"]




_COMMON = {"id", "scope", "project", "origin", "created_at", "updated_at"}


def test_existing_read_fields_are_unchanged(store):
    _mem(store, "m", "b [[guides/a.md]]\n")
    T.upsert_doc(store, "guides/a.md", "doc [[m]]\n", title="Guide A")
    T.upsert_skill(store, "sk", "a skill", "body [[m]]\n")
    T.upsert_command(store, "cmd", "body [[m]]\n", description="a command")

    mem, doc = T.get_memory(store, "m"), T.get_doc(store, "guides/a.md")
    assert mem is not None and doc is not None
    assert set(mem) - {"links"} == _COMMON | {
        "slug", "memory_type", "description", "load_behavior", "body"}
    assert set(doc) - {"links"} == _COMMON | {
        "path", "title", "load_behavior", "body"}
    assert set(T.get_entity(store, "skill", "sk")) - {"links"} == _COMMON | {
        "name", "description", "body", "files", "allowed_tools", "disable_model_invocation"}
    assert set(T.get_entity(store, "command", "cmd")) - {"links"} == _COMMON | {
        "name", "description", "body", "allowed_tools", "disable_model_invocation",
        "argument_hint"}


def test_search_results_carry_no_cards(store):
    _mem(store, "zebra-a", "zebracorn [[zebra-b]]\n", desc="zebracorn a")
    _mem(store, "zebra-b", "zebracorn\n", desc="zebracorn b")
    T.upsert_doc(store, "zebra.md", "zebracorn [[zebra-a]]\n", title="Zebracorn doc")
    T.get_memory(store, "zebra-a")

    for rows in (T.search_all(store, "zebracorn"), T.search_memories(store, "zebracorn"),
                 T.search_docs(store, "zebracorn")):
        assert rows and all("links" not in r for r in rows), rows


def test_bootstrap_payload_is_byte_identical_after_the_graph_is_built(store):
    _mem(store, "m", "see [[n]]\n")
    _mem(store, "n", "b\n")
    T.upsert_instruction(store, "Rules", "follow [[m]]\n", project=None)
    before = json.dumps(T.get_session_context(store, "/no/project"), sort_keys=True, default=str)

    T.get_memory(store, "m")
    T.get_memory(store, "n")
    after = json.dumps(T.get_session_context(store, "/no/project"), sort_keys=True, default=str)

    assert after == before


def test_cards_and_explore_are_not_reads(store):
    _mem(store, "a", "[[b]]\n")
    _mem(store, "b", "[[c]]\n")
    _mem(store, "c", "b\n")
    explore = getattr(T, "explore", None)
    assert explore is not None, "fstools has no explore"

    _links(T.get_memory(store, "a"))
    explore(store, "memory", "a", depth=2)
    explore(store, "memory", "c", depth=2)

    assert usage.stats("memory", "global", "a")["reads"] == 1
    for slug in ("b", "c"):
        assert usage.stats("memory", "global", slug) == {"reads": 0, "hits": 0, "last_read": None}
