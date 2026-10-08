'Context graph T5: typed links on upsert, cards, superseded entities, ambiguity (Phase 3\nitems 1, 2 and 6).\n\n- A relation key holds a list of "[[target]]" strings. A bare "target" is accepted on read.\n- A typed target resolves like a body wikilink (memory, doc, doc title), then as a skill,\n  command or script by name. A hook is not a graph node, so naming one draws no card.\n- One card per neighbor: a typed relation wins over a body mention, and typed out-link\n  cards sort before mention out-link cards.\n- Superseded means some entity names it under `supersedes`. It leaves other entities\'\n  cards and explore expansion, and reading it returns `superseded_by`.'
import inspect
import os
from pathlib import Path

import pytest

from agent_context import fstools as T
from agent_context import server, usage
from agent_context.store import parse_frontmatter

TYPED = ("supersedes", "part_of", "sibling", "enforced_by", "contradicts")


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    'Empty, isolated read counters: card order depends on them.'
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _got(result):
    assert result is not None, "read returned nothing"
    return result


def _fmt(n):
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _size(store, kind, key):
    e = store.get(kind, key)
    assert e is not None, (kind, key)
    body = e.get("script_body") if kind in ("script", "hook") else e.get("body")
    return _fmt(len((body or "").encode()))


def _mem(store, slug, body="b\n", desc=None, **kw):
    return T.upsert_memory(store, slug, "reference", desc or f"about {slug}", body, **kw)


def _front(store, kind, key):
    e = store.get(kind, key)
    assert e is not None, (kind, key)
    return parse_frontmatter(Path(e["_path"]).read_text(encoding="utf-8"))[0]


def _links(result):
    result = _got(result)
    assert "links" in result, f"no links field in {sorted(result)}"
    return result["links"]


def _keys(cards):
    return [c.split(" ")[3] for c in cards]




def test_upsert_memory_writes_each_relation_as_a_frontmatter_list_of_wikilinks(store):
    _mem(store, "old")
    _mem(store, "hub")
    r = _mem(store, "new", links={"supersedes": ["old"], "part_of": ["[[hub]]"]})
    assert "error" not in r, r
    fm = _front(store, "memory", "new")
    assert fm["supersedes"] == ["[[global/memory/old.md]]"]
    assert fm["part_of"] == ["[[global/memory/hub.md]]"]


def test_upsert_doc_and_upsert_skill_accept_links(store):
    _mem(store, "m", desc="a memory")
    assert "error" not in T.upsert_doc(store, "guides/a.md", "body\n", title="Guide A",
                                       links={"part_of": ["m"]})
    assert "error" not in T.upsert_skill(store, "sk", "a skill", "steps\n",
                                         links={"enforced_by": ["m"]})
    assert _front(store, "doc", "guides/a.md")["part_of"] == ["[[global/memory/m.md]]"]
    assert _front(store, "skill", "sk")["enforced_by"] == ["[[global/memory/m.md]]"]
    m_size = _size(store, "memory", "m")
    assert _links(T.get_doc(store, "guides/a.md")) == [f"→ part_of memory m ({m_size}): a memory"]
    assert _links(T.get_entity(store, "skill", "sk")) == [
        f"→ enforced_by memory m ({m_size}): a memory"]


@pytest.mark.parametrize("tool", ["upsert_memory", "upsert_doc", "upsert_skill"])
def test_the_mcp_tools_take_a_links_parameter(tool):
    assert "links" in inspect.signature(getattr(server, tool)).parameters


def test_links_replace_only_the_listed_relations_and_an_empty_list_removes_the_key(store):
    for s in ("a", "b", "c"):
        _mem(store, s)
    _mem(store, "n", links={"supersedes": ["a"], "part_of": ["b"]})
    _mem(store, "n", links={"part_of": ["c"]})
    fm = _front(store, "memory", "n")
    assert fm["supersedes"] == ["[[global/memory/a.md]]"]
    assert fm["part_of"] == ["[[global/memory/c.md]]"]
    _mem(store, "n", links={"part_of": []})
    fm = _front(store, "memory", "n")
    assert "part_of" not in fm
    assert fm["supersedes"] == ["[[global/memory/a.md]]"]


def test_an_unknown_relation_is_refused_naming_the_relations_and_nothing_is_written(store):
    _mem(store, "x")
    r = _mem(store, "n", links={"replaces": ["x"]})
    assert "error" in r, r
    for rel in TYPED:
        assert rel in r["error"], r["error"]
    assert store.get("memory", "n") is None

    _mem(store, "n", body="original\n")
    r = _mem(store, "n", body="changed\n", links={"mentions": ["x"]})
    assert "error" in r, "mentions come from body links; it is not a frontmatter relation"
    assert _got(T.get_memory(store, "n"))["body"] == "original"

    assert "error" in T.upsert_doc(store, "d.md", "b\n", title="D", links={"bogus": ["x"]})
    assert store.get("doc", "d.md") is None
    assert "error" in T.upsert_skill(store, "sk", "a skill", "b\n", links={"bogus": ["x"]})
    assert store.get("skill", "sk") is None


def test_a_relation_value_that_is_not_a_list_of_targets_is_refused(store):
    _mem(store, "x")
    assert "error" in _mem(store, "n", links={"part_of": "x"})
    assert "error" in _mem(store, "n", links={"part_of": [3]})
    assert "error" in _mem(store, "n", links=["part_of", "x"])
    assert store.get("memory", "n") is None


def test_a_dangling_target_warns_and_the_write_goes_through(store):
    _mem(store, "resolvable-target")
    r = _mem(store, "n", links={"part_of": ["no-such-thing", "resolvable-target"]})
    assert "error" not in r, r
    assert "no-such-thing" in r.get("warning", ""), r
    assert "resolvable-target" not in r.get("warning", ""), r
    assert _front(store, "memory", "n")["part_of"] == ["[[no-such-thing]]", "[[global/memory/resolvable-target.md]]"]
    assert _keys(_links(T.get_memory(store, "n"))) == ["resolvable-target"]

    r = T.upsert_doc(store, "d.md", "b\n", title="D", links={"sibling": ["ghost-doc"]})
    assert "ghost-doc" in r.get("warning", ""), r
    r = T.upsert_skill(store, "sk", "a skill", "b\n", links={"part_of": ["ghost-skill"]})
    assert "ghost-skill" in r.get("warning", ""), r




def test_cards_show_the_relation_on_both_ends(store):
    _mem(store, "old", desc="the old one")
    _mem(store, "new", desc="the new one", links={"supersedes": ["old"]})
    assert _links(T.get_memory(store, "new")) == [
        f"→ supersedes memory old ({_size(store, 'memory', 'old')}): the old one"]
    assert _links(T.get_memory(store, "old")) == [
        f"← supersedes memory new ({_size(store, 'memory', 'new')}): the new one"]

    _mem(store, "s1", desc="one")
    _mem(store, "s2", desc="two", links={"sibling": ["s1"]})
    assert _links(T.get_memory(store, "s2")) == [
        f"→ sibling memory s1 ({_size(store, 'memory', 's1')}): one"]
    assert _links(T.get_memory(store, "s1")) == [
        f"← sibling memory s2 ({_size(store, 'memory', 's2')}): two"]


def test_one_card_per_neighbor_and_the_typed_relation_wins_over_a_mention(store):
    _mem(store, "t", desc="target")
    _mem(store, "s", body="see [[t]]\n", links={"part_of": ["t"]})
    assert _links(T.get_memory(store, "s")) == [
        f"→ part_of memory t ({_size(store, 'memory', 't')}): target"]
    assert _links(T.get_memory(store, "t")) == [
        f"← part_of memory s ({_size(store, 'memory', 's')}): about s"]


def test_typed_out_link_cards_sort_before_mention_out_link_cards(store):
    _mem(store, "popular")
    _mem(store, "fan1", body="[[popular]]\n")
    _mem(store, "fan2", body="[[popular]]\n")
    _mem(store, "quiet")
    _mem(store, "s", body="[[popular]]\n", links={"part_of": ["quiet"]})
    cards = _links(T.get_memory(store, "s"))
    assert [c.split(" ")[:4] for c in cards] == [
        ["→", "part_of", "memory", "quiet"], ["→", "mentions", "memory", "popular"]]


def test_a_bare_target_in_hand_written_yaml_is_accepted_on_read(store):
    _mem(store, "t", desc="target")
    _mem(store, "s")
    p = Path(_got(store.get("memory", "s"))["_path"])
    p.write_text(p.read_text(encoding="utf-8").replace("\n---\n", "\npart_of:\n  - t\n---\n", 1),
                 encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    store.reload()
    assert _links(T.get_memory(store, "s")) == [
        f"→ part_of memory t ({_size(store, 'memory', 't')}): target"]


def test_a_typed_target_may_be_a_skill_command_or_script_but_not_a_hook(store):
    T.upsert_skill(store, "sk", "a skill", "steps\n")
    T.upsert_command(store, "cmd", "run it\n", description="a command")
    T.upsert_script(store, "scr", "#!/bin/sh\nexit 0\n", description="a script")
    T.upsert_hook(store, "hk", "PreToolUse", "exit 0\n", description="a hook")
    _mem(store, "rule", links={"part_of": ["sk"], "sibling": ["cmd"], "enforced_by": ["scr"]})
    cards = _links(T.get_memory(store, "rule"))
    assert sorted(cards) == sorted([
        f"→ part_of skill sk ({_size(store, 'skill', 'sk')}): a skill",
        f"→ sibling command cmd ({_size(store, 'command', 'cmd')}): a command",
        f"→ enforced_by script scr ({_size(store, 'script', 'scr')}): a script"])

    r = _mem(store, "rule2", links={"enforced_by": ["hk"]})
    assert "hk" in r.get("warning", ""), r
    assert _links(T.get_memory(store, "rule2")) == []




def test_a_superseded_entity_leaves_other_cards_and_explore(store):
    _mem(store, "old")
    _mem(store, "other")
    _mem(store, "reader", body="see [[old]] and [[other]]\n")
    _mem(store, "new", links={"supersedes": ["old"]})

    assert _keys(_links(T.get_memory(store, "reader"))) == ["other"]
    assert _keys(_got(T.explore(store, "memory", "reader"))["cards"]) == ["other"]

    
    out = _got(T.explore(store, "memory", "new", depth=2))
    assert [c.split(" ")[:4] for c in out["cards"]] == [["→", "supersedes", "memory", "old"]]
    assert _got(T.explore(store, "memory", "new", rel="supersedes"))["cards"] == [
        f"→ supersedes memory old ({_size(store, 'memory', 'old')}): about old"]
    assert _got(T.explore(store, "memory", "new", rel="mentions"))["cards"] == []

    
    start = _got(T.explore(store, "memory", "old"))
    assert start["start"] == "memory old"
    assert sorted(_keys(start["cards"])) == ["new", "reader"]


def test_reading_a_superseded_entity_returns_superseded_by(store):
    _mem(store, "old")
    _mem(store, "other")
    _mem(store, "new", links={"supersedes": ["old"]})
    assert _got(T.get_memory(store, "old"))["superseded_by"] == ["memory new"]
    assert _got(T.get_entity(store, "memory", "old"))["superseded_by"] == ["memory new"]
    assert "superseded_by" not in _got(T.get_memory(store, "other"))
    assert "superseded_by" not in _got(T.get_memory(store, "new"))

    T.upsert_doc(store, "old.md", "b\n", title="Old doc")
    T.upsert_doc(store, "new.md", "b\n", title="New doc", links={"supersedes": ["old.md"]})
    assert _got(T.get_doc(store, "old.md"))["superseded_by"] == ["doc new.md"]
    assert _got(T.get_entity(store, "doc", "old.md"))["superseded_by"] == ["doc new.md"]

    T.upsert_skill(store, "old-skill", "an old skill", "b\n")
    T.upsert_skill(store, "new-skill", "a new skill", "b\n", links={"supersedes": ["old-skill"]})
    assert _got(T.get_entity(store, "skill", "old-skill"))["superseded_by"] == ["skill new-skill"]




def test_ambiguous_links_reports_a_same_scope_slug_that_is_also_a_doc_title(store):
    _mem(store, "deploy", desc="the memory")
    T.upsert_doc(store, "runbooks/deploy.md", "b\n", title="deploy")
    _mem(store, "src", body="see [[deploy]]\n")
    _mem(store, "solo")
    _mem(store, "src2", body="see [[solo]]\n")

    rows = T.check_integrity(store)["ambiguous_links"]
    mine = [r for r in rows if r["source"] == "src"]
    assert len(mine) == 1, rows
    assert mine[0]["target"] == "deploy"
    assert mine[0]["candidates"] == ["global"]
    assert sorted(mine[0]["matches"]) == ["doc runbooks/deploy.md", "memory deploy"]
    assert not [r for r in rows if r["source"] == "src2"]
    
    assert [c.split(" ")[2:4] for c in _links(T.get_memory(store, "src"))] == [["memory", "deploy"]]
