'Context graph T5: upkeep findings in check_integrity (Phase 3 item 3).'
import time

import pytest

from agent_context import fstools as T
from agent_context import usage

KEYS = ("orphans", "split_candidates", "superseded_still_linked", "enforced_by_shared")


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


class _Fleet:
    'A fleet usage view with a chosen window and read counts.'

    def __init__(self, days, reads=None):
        self.since = time.time() - days * 86400
        self.machines = ["fixture"]
        self._reads = reads or {}

    @property
    def days(self):
        return max(0.0, (time.time() - self.since) / 86400)

    def stats(self, kind, scope, name):
        return {"reads": self._reads.get((kind, name), 0), "hits": 0, "last_read": None}


def _mem(store, slug, body="b\n", **kw):
    return T.upsert_memory(store, slug, "reference", f"about {slug}", body, **kw)


def test_every_new_finding_is_present_and_counted(store):
    f = T.check_integrity(store)
    for k in KEYS:
        assert k in f, k
        assert f["summary"][k] == len(f[k]), k




def test_orphans_say_nothing_before_usage_has_enough_evidence(store, monkeypatch):
    _mem(store, "lonely")
    monkeypatch.setattr(usage, "load_fleet", lambda *a, **k: _Fleet(days=29.5))
    assert T.check_integrity(store)["orphans"] == []


def test_orphans_are_memories_and_docs_with_no_backlinks_and_no_reads(store, monkeypatch):
    _mem(store, "lonely")
    _mem(store, "read-once")
    _mem(store, "linked")
    _mem(store, "linker", body="[[linked]]\n")
    T.upsert_doc(store, "lonely.md", "b\n", title="Lonely doc")
    T.upsert_skill(store, "sk", "a skill", "b\n")
    fleet = _Fleet(days=31, reads={("memory", "read-once"): 2, ("memory", "linker"): 1})
    monkeypatch.setattr(usage, "load_fleet", lambda *a, **k: fleet)
    f = T.check_integrity(store)
    assert sorted((r["type"], r["scope"], r["key"]) for r in f["orphans"]) == [
        ("doc", "global", "lonely.md"), ("memory", "global", "lonely")]
    assert f["summary"]["orphans"] == 2











def test_split_candidates_are_bodies_over_16_kb_with_three_backlinks(store):
    big = "x" * (16 * 1024 + 1)
    _mem(store, "big3", body=big)
    _mem(store, "big2", body=big)
    _mem(store, "edge3", body="x" * (16 * 1024))
    for i in range(3):
        _mem(store, f"r{i}", body="[[big3]] [[edge3]]\n")
    for i in range(2):
        _mem(store, f"q{i}", body="[[big2]]\n")
    rows = T.check_integrity(store)["split_candidates"]
    assert [(r["type"], r["scope"], r["key"], r["backlinks"]) for r in rows] == [
        ("memory", "global", "big3", 3)]
    assert rows[0]["bytes"] == 16 * 1024 + 1




def test_superseded_still_linked_names_who_still_links_the_old_entity(store):
    _mem(store, "old")
    _mem(store, "reader", body="[[old]]\n")
    _mem(store, "new", body="replaces [[old]]\n", links={"supersedes": ["old"]})
    _mem(store, "old2")
    _mem(store, "new2", links={"supersedes": ["old2"]})
    rows = T.check_integrity(store)["superseded_still_linked"]
    assert len(rows) == 1, rows
    r = rows[0]
    assert (r["type"], r["scope"], r["key"]) == ("memory", "global", "old")
    assert r["superseded_by"] == ["memory new"]
    assert r["linked_from"] == ["memory reader"]




def test_enforced_by_shared_names_a_target_two_entities_claim(store):
    T.upsert_hook(store, "guard", "PreToolUse", "exit 0\n", description="a hook")
    T.upsert_script(store, "checker", "#!/bin/sh\nexit 0\n", description="a script")
    _mem(store, "rule-a", links={"enforced_by": ["guard"]})
    T.upsert_doc(store, "rule-b.md", "b\n", title="Rule B", links={"enforced_by": ["[[guard]]"]})
    _mem(store, "rule-c", links={"enforced_by": ["other-guard"]})
    _mem(store, "rule-d", links={"enforced_by": ["checker"]})
    _mem(store, "rule-e", links={"enforced_by": ["[[checker]]"]})
    rows = T.check_integrity(store)["enforced_by_shared"]
    assert sorted((r["target"], sorted(r["sources"])) for r in rows) == [
        ("guard", ["doc rule-b.md", "memory rule-a"]),
        ("script checker", ["memory rule-d", "memory rule-e"])]
