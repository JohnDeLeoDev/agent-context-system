#!/usr/bin/env python3
'python3 test-store-compact-scope.py'
import importlib.util
import os
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


def test_a_pointer_into_another_project_does_not_keep_a_third_projects_handoff(mod, root):
    
    g = base.write_handoff(root, "global", "foo.md", "consumed 2026-01-02", 20)
    a = base.write_handoff(root, "project:ProjA", "foo.md", "consumed 2026-01-02", 20)
    arc.write_live_doc(root, "notes.md", 'See `get_doc("handoffs/foo.md", project="ProjB")`.',
                       scope="project:ProjB")
    res = arc.sweep(mod, root)
    assert not os.path.exists(a) and os.path.isfile(g), res
    assert [k["ref"] for k in res["kept"]] == ["global/handoffs/foo.md"], res


def test_a_scoped_pointer_keeps_only_its_own_scope_and_global(mod, root):
    g = base.write_handoff(root, "global", "foo.md", "consumed 2026-01-02", 20)
    a = base.write_handoff(root, "project:ProjA", "foo.md", "consumed 2026-01-02", 20)
    c = base.write_handoff(root, "project:ProjC", "foo.md", "consumed 2026-01-02", 20)
    arc.write_live_doc(root, "notes.md", 'See `get_doc("handoffs/foo.md", "ProjA")`.')
    arc.sweep(mod, root)
    assert os.path.isfile(a) and os.path.isfile(g)
    assert not os.path.exists(c)


def test_an_unscoped_pointer_resolves_in_its_own_scope_and_global(mod, root):
    g = base.write_handoff(root, "global", "foo.md", "consumed 2026-01-02", 20)
    a = base.write_handoff(root, "project:ProjA", "foo.md", "consumed 2026-01-02", 20)
    b = base.write_handoff(root, "project:ProjB", "foo.md", "consumed 2026-01-02", 20)
    arc.write_live_doc(root, "notes.md", "See [[handoffs/foo]].", scope="project:ProjA")
    arc.sweep(mod, root)
    assert os.path.isfile(a) and os.path.isfile(g)
    assert not os.path.exists(b)


def test_a_workspace_pointer_keeps_the_workspace_handoff(mod, root):
    w = base.write_handoff(root, "ws:example-workspace", "w.md", "consumed 2026-01-02", 20)
    arc.write_live_doc(root, "notes.md", 'See `get_doc("handoffs/w.md", workspace="example-workspace")`.')
    arc.sweep(mod, root)
    assert os.path.isfile(w)


def test_an_interrupted_move_is_finished_by_the_next_run(mod, root):
    src = base.write_handoff(root, "global", "x.md", "consumed 2026-01-02", 20)
    text = open(src, encoding="utf-8").read()
    dst = arc.archived_handoff(root, "global", "x.md")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    base.scan_store(mod, root)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(mod._rekey(text, "global", "archive/handoffs/x.md"))
    res = mod.apply_handoffs()
    assert not os.path.exists(src), res
    assert res["moved"] == ["global/handoffs/x.md"] and res["kept"] == [], res


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
