#!/usr/bin/env python3
'python3 test-store-compact-links.py'
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "test_store_compact_archive", os.path.join(HERE, "test-store-compact-archive.py"))
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load spec for test_store_compact_archive")
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)
base = arc.base

SPENT = "consumed 2026-01-02"


def body_of(path):
    return arc.split(path)[1]


def test_483_inbound_links_to_an_archived_handoff_are_rewritten(mod, root):
    h = base.write_handoff(root, "project:Foo", "2026-09-20-x.md", SPENT, 20)
    other = base.write_handoff(root, "global", "old.md", SPENT, 20)
    inbound = ("See [[projects/Foo/docs/handoffs/2026-09-20-x|the x handoff]], "
               "[[projects/Foo/docs/handoffs/2026-09-20-x]] and "
               '`get_doc("handoffs/2026-09-20-x.md")`.')
    with open(other, "a", encoding="utf-8") as f:
        f.write("\n" + inbound + "\n")
    frozen = arc.write_live_doc(root, "archive/notes/older.md", inbound)
    frozen_before = open(frozen, encoding="utf-8").read()
    live = arc.write_live_doc(root, "agent-context-store.md",
                              inbound + "\nIn code: `[[projects/Foo/docs/handoffs/2026-09-20-x]]`.")
    live_meta, _ = arc.split(live)
    res = arc.sweep(mod, root)
    assert not os.path.exists(h) and not os.path.exists(other), res
    assert os.path.isfile(arc.archived_handoff(root, "project:Foo", "2026-09-20-x.md")), res
    meta, body = arc.split(live)
    assert meta == live_meta, meta
    assert ("See [[projects/Foo/docs/archive/handoffs/2026-09-20-x|the x handoff]], "
            "[[projects/Foo/docs/archive/handoffs/2026-09-20-x]] and "
            '`get_doc("archive/handoffs/2026-09-20-x.md")`.') in body, body
    
    assert "In code: `[[projects/Foo/docs/handoffs/2026-09-20-x]]`." in body, body
    
    
    assert open(frozen, encoding="utf-8").read() == frozen_before
    assert inbound in body_of(arc.archived_handoff(root, "global", "old.md"))
    assert res["rewrites"] == [{"file": "global/docs/agent-context-store.md", "changes": [
        {"from": "[[projects/Foo/docs/handoffs/2026-09-20-x|the x handoff]]",
         "to": "[[projects/Foo/docs/archive/handoffs/2026-09-20-x|the x handoff]]"},
        {"from": "[[projects/Foo/docs/handoffs/2026-09-20-x]]",
         "to": "[[projects/Foo/docs/archive/handoffs/2026-09-20-x]]"},
        {"from": 'get_doc("handoffs/2026-09-20-x.md"',
         "to": 'get_doc("archive/handoffs/2026-09-20-x.md"'}]}], res


def test_483_a_root_page_link_to_a_global_handoff_is_rewritten(mod, root):
    
    h = base.write_handoff(root, "global", "2026-09-20-harness-neutral-4-5.md", SPENT, 20)
    page = arc.write_live_doc(
        root, "agent-context-store.md",
        "- [[global/docs/handoffs/2026-09-20-harness-neutral-4-5|2026-09-20-harness-neutral-4-5]]")
    arc.sweep(mod, root)
    assert not os.path.exists(h)
    assert ("- [[global/docs/archive/handoffs/2026-09-20-harness-neutral-4-5|"
            "2026-09-20-harness-neutral-4-5]]") in body_of(page)


def test_483_memories_anchors_and_get_entity_are_rewritten(mod, root):
    base.write_handoff(root, "project:Foo", "p.md", SPENT, 20)
    mem_dir = os.path.join(root, "global", "memory")
    os.makedirs(mem_dir, exist_ok=True)
    mem = os.path.join(mem_dir, "m.md")
    with open(mem, "w", encoding="utf-8") as f:
        f.write('---\ntype: "memory"\nslug: "m"\n---\n\n'
                "[[projects/Foo/docs/handoffs/p#Next action|next]] and "
                "[[handoffs/p.md]] and `get_entity('doc', 'handoffs/p.md', 'Foo')`.\n")
    arc.sweep(mod, root)
    assert ("[[projects/Foo/docs/archive/handoffs/p#Next action|next]] and "
            "[[archive/handoffs/p.md]] and "
            "`get_entity('doc', 'archive/handoffs/p.md', 'Foo')`.") in body_of(mem)


def test_483_a_doc_path_another_scope_still_holds_is_not_rewritten(mod, root):
    base.write_handoff(root, "global", "foo.md", "open", 20)
    base.write_handoff(root, "project:A", "foo.md", SPENT, 20)
    notes = arc.write_live_doc(root, "notes.md",
                               '`get_doc("handoffs/foo.md")`, [[handoffs/foo.md]], '
                               "[[projects/A/docs/handoffs/foo]]", scope="project:B")
    res = arc.sweep(mod, root)
    body = body_of(notes)
    assert '`get_doc("handoffs/foo.md")`, [[handoffs/foo.md]], ' in body, body
    assert "[[projects/A/docs/archive/handoffs/foo]]" in body, body
    assert len(res["rewrites"]) == 1 and len(res["rewrites"][0]["changes"]) == 1, res


def test_483_a_kept_handoff_has_no_links_rewritten(mod, root):
    h = base.write_handoff(root, "global", "a.md", SPENT, 20)
    notes = arc.write_live_doc(root, "notes.md",
                               'Read `get_doc("handoffs/a.md")`. [[global/docs/handoffs/a|a]]')
    res = arc.sweep(mod, root)
    assert os.path.isfile(h) and res["rewrites"] == [], res
    assert "[[global/docs/handoffs/a|a]]" in body_of(notes)


def test_483_an_interrupted_move_still_rewrites_the_links(mod, root):
    src = base.write_handoff(root, "global", "x.md", SPENT, 20)
    notes = arc.write_live_doc(root, "notes.md", "See [[global/docs/handoffs/x]].")
    dst = arc.archived_handoff(root, "global", "x.md")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    base.scan_store(mod, root)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(mod._rekey(open(src, encoding="utf-8").read(), "global", "archive/handoffs/x.md"))
    mod.apply_handoffs()
    assert not os.path.exists(src)
    assert "See [[global/docs/archive/handoffs/x]]." in body_of(notes)


def test_483_report_mode_lists_the_rewrites_and_changes_nothing(mod, root):
    store = os.path.join(root, "store")
    h = base.write_handoff(store, "global", "a.md", SPENT, 20)
    doc = arc.write_live_doc(store, "plan.md", "See [[global/docs/handoffs/a|a]].")
    before = open(doc, encoding="utf-8").read()
    env = dict(os.environ, AGENT_CONTEXT_STORE=store)
    p = subprocess.run([sys.executable, base.TARGET, "--json"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["would_rewrite"] == [{"file": "global/docs/plan.md", "changes": [
        {"from": "[[global/docs/handoffs/a|a]]",
         "to": "[[global/docs/archive/handoffs/a|a]]"}]}], out
    p = subprocess.run([sys.executable, base.TARGET],
                       capture_output=True, text=True, env=env, timeout=60)
    assert ("global/docs/plan.md: [[global/docs/handoffs/a|a]] -> "
            "[[global/docs/archive/handoffs/a|a]]") in p.stdout, p.stdout
    assert os.path.isfile(h) and open(doc, encoding="utf-8").read() == before


def test_483_apply_json_reports_the_rewrites(mod, root):
    store = os.path.join(root, "store")
    base.write_handoff(store, "global", "a.md", SPENT, 20)
    doc = arc.write_live_doc(store, "plan.md", "See [[global/docs/handoffs/a|a]].")
    env = dict(os.environ, AGENT_CONTEXT_STORE=store)
    p = subprocess.run([sys.executable, base.TARGET, "--apply", "--json"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    archived = json.loads(p.stdout)["archived"]
    assert archived["handoffs"] == 1, archived
    assert archived["rewrites"] == [{"file": "global/docs/plan.md", "changes": [
        {"from": "[[global/docs/handoffs/a|a]]",
         "to": "[[global/docs/archive/handoffs/a|a]]"}]}], archived
    assert "See [[global/docs/archive/handoffs/a|a]]." in body_of(doc)


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        mod = base.load()
        with tempfile.TemporaryDirectory() as root:
            try:
                fn(mod, root)
                print(f"ok    {name}")
            except Exception:
                failed += 1
                print(f"FAIL  {name}")
                traceback.print_exc(limit=1)
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
