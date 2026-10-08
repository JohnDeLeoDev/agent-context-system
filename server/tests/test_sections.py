"Context graph T4: section addressing (Phase 2, criteria 1 to 5), plus the card\ndescription trim found by T2's review."
import json
from collections.abc import Callable
from typing import Any

import pytest

from agent_context import fstools as T
from agent_context import refs, server, usage

KB16 = 16 * 1024


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    'Empty, isolated read counters: card order depends on them.'
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _fmt(n):
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict:
    'Call a reader with keyword arguments it may not declare yet, and require a dict.'
    out = fn(*args, **kwargs)
    assert isinstance(out, dict), out
    return out


def _mem(store, slug, body, desc=None):
    return T.upsert_memory(store, slug, "reference", desc or f"about {slug}", body)


BODY = (
    "intro line\n"
    "# Top\n"
    "top text\n"
    "## Alpha Section\n"
    "alpha text\n"
    "### Alpha child\n"
    "child text\n"
    "## Beta\n"
    "beta text\n"
    "```\n## Fenced Heading\n```\n"
    "~~~md\n# Tilde Fenced\n~~~\n"
    "## beta\n"
    "second beta"  
)
BETA = "## Beta\nbeta text\n```\n## Fenced Heading\n```\n~~~md\n# Tilde Fenced\n~~~\n"




def test_a_section_read_returns_the_heading_through_its_subsections(store):
    _mem(store, "led", BODY)

    out = _call(T.get_memory, store, "led", section="  alpha   SECTION ")

    assert out["body"] == "## Alpha Section\nalpha text\n### Alpha child\nchild text\n"
    assert out["section"] == "Alpha Section"
    assert "ambiguous" not in out


def test_a_section_ends_at_the_next_heading_of_the_same_or_higher_level(store):
    _mem(store, "led", BODY)

    assert _call(T.get_memory, store, "led", section="Alpha child")["body"] == \
        "### Alpha child\nchild text\n"
    assert _call(T.get_memory, store, "led", section="top")["body"] == \
        BODY[BODY.index("# Top"):]


def test_a_section_read_keeps_every_other_field(store):
    _mem(store, "other", "b\n")
    _mem(store, "led", "## Alpha Section\nsee [[other]]\n")

    full = _call(T.get_memory, store, "led")
    part = _call(T.get_memory, store, "led", section="Alpha Section")

    assert {k: v for k, v in part.items() if k not in ("body", "section")} == \
        {k: v for k, v in full.items() if k != "body"}


def test_duplicate_headings_return_the_first_and_count_the_matches(store):
    _mem(store, "led", BODY)

    out = _call(T.get_memory, store, "led", section="BETA")

    assert out["body"] == BETA
    assert out["section"] == "Beta"
    assert out["ambiguous"] == 2


@pytest.mark.parametrize("name", ["Fenced Heading", "Tilde Fenced"])
def test_headings_inside_fences_are_not_sections(store, name):
    _mem(store, "led", BODY)

    out = _call(T.get_memory, store, "led", section=name)

    assert "error" in out and "body" not in out, out


def test_an_unclosed_fence_hides_every_heading_after_it(store):
    _mem(store, "led", "## Open\ntext\n```\n## Hidden")

    assert "error" in _call(T.get_memory, store, "led", section="Hidden")
    assert _call(T.get_memory, store, "led", section="Open")["body"] == \
        "## Open\ntext\n```\n## Hidden"


@pytest.mark.parametrize(("line", "name", "found"), [
    ("## Closed ##", "closed", True),
    ("###### Six", "six", True),
    ("   ## Indented three", "indented three", True),
    ("    ## Indented four", "indented four", False),
    ("##NoSpace", "nospace", False),
    ("####### Seven", "seven", False),
])
def test_heading_grammar(store, line, name, found):
    _mem(store, "led", f"lead\n{line}\ntext\n")

    out = _call(T.get_memory, store, "led", section=name)

    assert ("error" not in out) is found, out


def test_a_section_that_matches_nothing_is_an_error_with_the_toc(store):
    _mem(store, "led", BODY)

    for name in ("Gamma", ""):
        out = _call(T.get_memory, store, "led", section=name)
        assert "led" in out.get("error", "") and "body" not in out, out
        assert [(t[0], t[1]) for t in out["toc"]] == [
            ("Top", 1), ("Alpha Section", 2), ("Alpha child", 3), ("Beta", 2), ("beta", 2)]


def test_every_reader_accepts_section(store):
    body = "# Guide\nintro\n## Setup\nsteps"
    want = "## Setup\nsteps"
    _mem(store, "m", body)
    T.upsert_doc(store, "guides/a.md", body, title="Guide A")
    T.upsert_skill(store, "sk", "a skill", body)
    T.upsert_command(store, "cmd", body, description="a command")

    assert _call(T.get_doc, store, "guides/a.md", section="setup")["body"] == want
    for kind, key in (("memory", "m"), ("doc", "guides/a.md"), ("skill", "sk"),
                      ("command", "cmd")):
        out = _call(T.get_entity, store, kind, key, section="setup")
        assert out["body"] == want and out["section"] == "Setup", (kind, out)


def test_section_on_a_kind_without_sections_is_an_error(store):
    T.upsert_script(store, "scr", "#!/bin/sh\n# Setup\necho hi\n", description="a script")
    T.upsert_hook(store, "hk", "PreToolUse", "#!/bin/sh\n# Setup\nexit 0\n", description="a hook")

    for kind, key in (("script", "scr"), ("hook", "hk")):
        out = _call(T.get_entity, store, kind, key, section="Setup")
        assert "section" in out.get("error", ""), (kind, out)
        assert "script_body" not in out, out


def test_a_section_read_of_a_missing_entity_is_none(store):
    assert T.get_memory(store, "nope", section="x") is None


def _tool(monkeypatch, store, name, **kwargs):
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    return json.loads(getattr(server, name)(**kwargs))


def test_the_tools_accept_section(store, monkeypatch):
    _mem(store, "led", BODY)
    T.upsert_doc(store, "guides/a.md", BODY, title="Guide A")

    assert _tool(monkeypatch, store, "get_memory", slug="led", section="beta")["body"] == BETA
    assert _tool(monkeypatch, store, "get_doc", path="guides/a.md", section="beta")["body"] == BETA
    got = _tool(monkeypatch, store, "get_entity", kind="doc", key="guides/a.md", section="beta")
    assert got["body"] == BETA and got["ambiguous"] == 2




def _sized(total, head="## H\n"):
    'A body of exactly `total` UTF-8 bytes that opens with `head`.'
    return head + "x" * (total - len(head.encode()))


def test_a_body_over_16_kb_carries_a_toc_with_section_bytes(store):
    one = "# One\n" + "a" * 9000 + "\n"
    two = "## Two\n" + "b" * 9000 + "\n"
    three = "# Three\n" + "c" * 100
    _mem(store, "big", "lead\n" + one + two + three)

    out = _call(T.get_memory, store, "big")

    assert out["body"] == "lead\n" + one + two + three
    assert out["toc"] == [
        ["One", 1, len((one + two).encode())],
        ["Two", 2, len(two.encode())],
        ["Three", 1, len(three.encode())],
    ]


def test_the_toc_threshold_is_16_kb_of_utf8(store):
    _mem(store, "at", _sized(KB16))
    _mem(store, "over", _sized(KB16 + 1))
    
    _mem(store, "wide", "## Wide\n" + "é" * 9000)

    assert "toc" not in _call(T.get_memory, store, "at")
    assert [t[0] for t in _call(T.get_memory, store, "over")["toc"]] == ["H"]
    wide = _call(T.get_memory, store, "wide")["toc"]
    assert wide == [["Wide", 2, len(b"## Wide\n") + 18000]]


def test_a_large_body_without_headings_carries_an_empty_toc(store):
    _mem(store, "flat", "x" * (KB16 + 1))

    assert _call(T.get_memory, store, "flat")["toc"] == []


def test_a_small_body_and_a_section_read_carry_no_toc(store):
    _mem(store, "small", BODY)
    _mem(store, "big", _sized(KB16 + 1, head="## H\n") + "\n## Tail\nt\n")

    assert "toc" not in _call(T.get_memory, store, "small")
    assert "toc" not in _call(T.get_memory, store, "big", section="Tail")


def test_docs_skills_and_commands_carry_a_toc(store):
    body = _sized(KB16 + 1, head="## Only\n")
    T.upsert_doc(store, "big.md", body, title="Big")
    T.upsert_skill(store, "sk", "a skill", body)
    T.upsert_command(store, "cmd", body, description="a command")

    assert [t[0] for t in _call(T.get_doc, store, "big.md")["toc"]] == ["Only"]
    for kind, key in (("doc", "big.md"), ("skill", "sk"), ("command", "cmd")):
        assert [t[0] for t in _call(T.get_entity, store, kind, key)["toc"]] == \
            ["Only"], kind




def _anchors(store):
    f = T.check_integrity(store)
    assert "dangling_anchors" in f, sorted(f)
    return f["dangling_anchors"], f["summary"]["dangling_anchors"]


def test_a_section_link_is_an_edge_to_the_entity(store):
    _mem(store, "target", BODY, desc="the target")
    _mem(store, "src", "see [[target#Alpha Section]] and [[target#Nope|shown]]\n")

    links = _call(T.get_memory, store, "src")["links"]

    assert links == [f"→ mentions memory target ({_fmt(len(BODY.encode()))}): the target"]


def test_anchors_that_match_no_heading_are_reported(store):
    _mem(store, "target", BODY)
    T.upsert_doc(store, "guides/a.md", "## Setup\nsteps\n", title="Guide A")
    _mem(store, "src", (
        "ok [[target#Alpha Section]], [[target#alpha   section|shown]], "
        "[[target#Alpha Section#Alpha child]], [[guides/a.md#setup]], [[target#^abc123]]\n"
        "bad [[target#Nope]], [[target#Gone|shown]], [[target#Fenced Heading]], "
        "[[target#Alpha Section#Missing]], [[guides/a.md#Teardown]]\n"
        "code `[[target#In Code]]`\n"
        "```\n[[target#In Fence]]\n```\n"))

    rows, count = _anchors(store)

    assert sorted((r["source"], r["target"], r["anchor"]) for r in rows) == sorted([
        ("src", "target", "Nope"), ("src", "target", "Gone"),
        ("src", "target", "Fenced Heading"), ("src", "target", "Alpha Section#Missing"),
        ("src", "guides/a.md", "Teardown")])
    assert all(r["source_type"] == "memory" and r["scope"] == "global" for r in rows), rows
    assert count == 5


def test_an_anchor_on_a_dangling_target_is_a_dangling_link_not_a_dangling_anchor(store):
    _mem(store, "src", "see [[no-such-target#Heading]]\n")

    rows, _count = _anchors(store)

    assert rows == []
    assert any(d["target"] == "no-such-target"
               for d in T.check_integrity(store)["dangling_links"])


def test_an_archived_docs_anchors_are_not_reported(store):
    _mem(store, "target", BODY)
    T.upsert_doc(store, "archive/old.md", "see [[target#Nope]]\n", title="Old")

    assert _anchors(store) == ([], 0)


def test_a_dangling_anchor_clears_once_the_heading_exists(store):
    _mem(store, "target", "## One\ntext\n")
    _mem(store, "src", "see [[target#Two]]\n")
    assert len(_anchors(store)[0]) == 1

    T.edit_body(store, "memory", "target", "## One\n", "## One\n## Two\n")

    assert _anchors(store) == ([], 0)


def test_scan_refs_output_is_unchanged_by_anchors():
    body = "[[a#Heading|shown]] [[b#^block]] [[c]] `get_memory(\"d\")`"

    assert refs._scan_refs(body) == (["a", "b", "c"], [("memory", "d")])
    assert refs._scan_wikilinks(body) == ["a", "b", "c"]




def _card_for(store, target):
    _mem(store, "src", f"[[{target}]]\n")
    return _call(T.get_memory, store, "src")["links"]


def test_a_card_for_a_large_target_counts_its_sections(store):
    body = "## One\n## Two\n```\n## Fenced\n```\n### Three\n" + "x" * KB16
    _mem(store, "big", body)
    size = _fmt(len(body.encode()))

    assert _card_for(store, "big") == [f"→ mentions memory big ({size}, 3 sections): about big"]
    assert _call(T.explore, store, "memory", "src")["cards"] == \
        [f"→ mentions memory big ({size}, 3 sections): about big"]


def test_a_card_says_one_section_in_the_singular(store):
    body = "## Only\n" + "x" * KB16
    _mem(store, "big", body)

    assert _card_for(store, "big") == [
        f"→ mentions memory big ({_fmt(len(body.encode()))}, 1 section): about big"]


def test_cards_for_small_or_headless_targets_are_unchanged(store):
    _mem(store, "small", BODY)
    _mem(store, "flat", "x" * (KB16 + 1))
    _mem(store, "src", "[[small]] [[flat]]\n")

    assert sorted(_call(T.get_memory, store, "src")["links"]) == sorted([
        f"→ mentions memory small ({_fmt(len(BODY.encode()))}): about small",
        f"→ mentions memory flat ({_fmt(KB16 + 1)}): about flat"])


def test_the_section_count_follows_an_edit(store):
    body = "## One\n" + "x" * KB16
    _mem(store, "big", body)
    _card_for(store, "big")

    T.edit_body(store, "memory", "big", "## One\n", "## One\n## Two\n")

    size = _fmt(len(body.encode()) + len("## Two\n"))
    assert _call(T.get_memory, store, "src")["links"] == [
        f"→ mentions memory big ({size}, 2 sections): about big"]




def test_large_bodies_without_headings_are_reported(store):
    flat = "x" * (KB16 + 1)
    _mem(store, "flat", flat)
    T.upsert_doc(store, "flat.md", flat, title="Flat")
    T.upsert_skill(store, "flat-skill", "a skill", flat)
    T.upsert_command(store, "flat-cmd", flat, description="a command")
    _mem(store, "fenced", "```\n## Hidden\n```\n" + flat)
    
    _mem(store, "small", "x" * KB16)
    _mem(store, "headed", "## H\n" + flat)
    T.upsert_doc(store, "archive/flat.md", flat, title="Old flat")
    T.upsert_script(store, "flat-script", flat, description="a script")
    T.upsert_instruction(store, "Flat rules", flat, project=None)

    f = T.check_integrity(store)
    rows = f.get("unaddressable_large_bodies")

    assert rows is not None, sorted(f)
    assert sorted((r["type"], r["scope"], r["key"], r["bytes"]) for r in rows) == sorted([
        ("memory", "global", "flat", KB16 + 1),
        ("doc", "global", "flat.md", KB16 + 1),
        ("skill", "global", "flat-skill", KB16 + 1),
        ("command", "global", "flat-cmd", KB16 + 1),
        ("memory", "global", "fenced", len("```\n## Hidden\n```\n") + KB16 + 1)])
    assert f["summary"]["unaddressable_large_bodies"] == 5


def test_the_new_findings_follow_ambiguous_links(store):
    keys = list(T.check_integrity(store))

    i = keys.index("ambiguous_links")
    assert keys[i + 1:i + 3] == ["dangling_anchors", "unaddressable_large_bodies"]




_CUT = 71  


def _desc_of(store, desc):
    _mem(store, "t", "b\n", desc=desc)
    _mem(store, "s", "[[t]]\n")
    card = _call(T.get_memory, store, "s")["links"][0]
    return card.split("): ", 1)[1]


@pytest.mark.parametrize("cluster", [
    "é",                   
    "\U0001F469‍\U0001F4BB",  
    "\U0001F44D\U0001F3FD",      
    "\U0001F1FA\U0001F1F8",      
    "❤️",              
    "1️⃣",             
])
def test_a_trimmed_description_never_splits_a_cluster(store, cluster):
    for offset in range(1, len(cluster)):
        before = "a" * (_CUT - offset)
        got = _desc_of(store, before + cluster * 3 + "z" * 10)

        assert got == before + "…", (offset, got)


def test_a_cut_that_falls_between_clusters_keeps_the_whole_cluster(store):
    flag = "\U0001F1FA\U0001F1F8"
    before = "a" * (_CUT - 2)

    assert _desc_of(store, before + flag * 3 + "z" * 10) == before + flag + "…"
