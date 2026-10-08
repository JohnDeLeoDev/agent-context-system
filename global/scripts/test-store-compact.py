#!/usr/bin/env python3
'Acceptance tests for store-compact.py: the spent-handoff rule and scope_arg.\n\nEach test builds a throwaway store under TMPDIR, points the module at it, and checks\nwhat scan() reports. STORE_COMPACT_PATH overrides which copy of store-compact.py is\nloaded, so a deliberately broken copy can prove a test is able to fail.\n\n    python3 test-store-compact.py'
import importlib.util
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.environ.get("STORE_COMPACT_PATH") or os.path.join(HERE, "store-compact.py")


def load():
    spec = importlib.util.spec_from_file_location("store_compact", TARGET)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load spec for store_compact from %s" % TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def stamp(days_ago):
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def docs_dir(root, scope):
    if scope == "global":
        return os.path.join(root, "global", "docs")
    kind, name = scope.split(":", 1)
    return os.path.join(root, "workspaces" if kind == "ws" else "projects", name, "docs")


def write_handoff(root, scope, name, status, days_ago):
    d = os.path.join(docs_dir(root, scope), "handoffs")
    os.makedirs(d, exist_ok=True)
    text = ("---\n"
            'type: "doc"\n'
            f'path: "handoffs/{name}"\n'
            f'updated_at: "{stamp(days_ago)}"\n'
            "---\n\n# Handoff: test\n\n")
    if status is not None:
        text += f"**Status:** {status} · **Opened:** 2026-01-01 · **Machine:** m\n\n"
    text += "## Next action\nnothing\n"
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def scan_store(mod, root):
    mod.STORE = root
    mod.OBS_DIR = os.path.join(root, "global/audit-observations")
    mod.OBS_ARCHIVE_DIR = os.path.join(root, "global/audit-observations-archive")
    mod.OBS_LEGACY = os.path.join(root, "global/audit-observations.json")
    return mod.scan()


def spent(work):
    return [w for w in work if w["kind"] == "spent-handoff"]



def test_consumed_old_handoff_is_reported(mod, root):
    write_handoff(root, "global", "2026-01-01-a.md", "consumed 2026-01-02", 10)
    _, work = scan_store(mod, root)
    items = spent(work)
    assert len(items) == 1, work
    assert items[0]["ref"] == "global/handoffs/2026-01-01-a.md", items[0]
    assert "store-compact.py --apply" in items[0]["fix"], items[0]
    assert set(items[0]) == {"kind", "ref", "detail", "fix", "saving"}, items[0]


def test_stale_old_handoff_is_reported(mod, root):
    write_handoff(root, "global", "b.md", "stale 2026-01-02: already fixed elsewhere", 30)
    _, work = scan_store(mod, root)
    assert [w["ref"] for w in spent(work)] == ["global/handoffs/b.md"], work



def test_open_handoff_is_never_reported(mod, root):
    write_handoff(root, "global", "c.md", "open", 400)
    _, work = scan_store(mod, root)
    assert spent(work) == [], work



def test_recent_consumed_handoff_is_not_reported(mod, root):
    write_handoff(root, "global", "d.md", "consumed", 6.9)
    _, work = scan_store(mod, root)
    assert spent(work) == [], work


def test_seven_day_boundary_is_reported(mod, root):
    write_handoff(root, "global", "e.md", "consumed", 7.05)
    _, work = scan_store(mod, root)
    assert len(spent(work)) == 1, work



def test_521_status_date_ages_a_handoff_whose_metadata_was_just_edited(mod, root):
    write_handoff(root, "global", "j.md", "consumed (done 2026-01-02), next: `handoffs/x.md`", 0)
    write_handoff(root, "global", "k.md", "consumed, done 2026-01-03 (landed) · consumed 2026-01-02", 0)
    _, work = scan_store(mod, root)
    assert len(spent(work)) == 2, work


def test_521_recent_status_date_keeps_a_handoff_with_an_old_updated_at(mod, root):
    write_handoff(root, "global", "l.md", f"consumed {stamp(2)[:10]}", 30)
    _, work = scan_store(mod, root)
    assert spent(work) == [], work



def test_malformed_handoffs_are_ignored(mod, root):
    write_handoff(root, "global", "f.md", None, 30)
    d = os.path.join(docs_dir(root, "global"), "handoffs")
    with open(os.path.join(d, "g.md"), "w", encoding="utf-8") as f:
        f.write("**Status:** consumed 2026-01-02\n")
    with open(os.path.join(d, "h.txt"), "w", encoding="utf-8") as f:
        f.write("**Status:** consumed 2026-01-02\n")
    _, work = scan_store(mod, root)
    assert spent(work) == [], work


def test_empty_store_does_not_crash(mod, root):
    reap, work = scan_store(mod, root)
    assert reap == [] and work == [], (reap, work)



def test_workspace_and_project_handoffs_are_reported(mod, root):
    write_handoff(root, "ws:example-workspace", "w.md", "consumed 2026-01-02", 20)
    write_handoff(root, "project:Foo", "p.md", "consumed 2026-01-02", 20)
    _, work = scan_store(mod, root)
    by_ref = {w["ref"]: w for w in spent(work)}
    assert set(by_ref) == {"ws:example-workspace/handoffs/w.md", "project:Foo/handoffs/p.md"}, work
    assert 'workspace="example-workspace"' in by_ref["ws:example-workspace/handoffs/w.md"]["fix"], by_ref
    assert 'project="Foo"' in by_ref["project:Foo/handoffs/p.md"]["fix"], by_ref


def test_scope_arg_names_workspaces_correctly(mod, root):
    assert mod.scope_arg("global") == ""
    assert mod.scope_arg("project:Foo") == ', project="Foo"'
    assert mod.scope_arg("ws:example-workspace") == ', workspace="example-workspace"', mod.scope_arg("ws:example-workspace")



def test_apply_never_deletes_a_handoff(mod, root):
    path = write_handoff(root, "global", "i.md", "consumed 2026-01-02", 90)
    reap, _ = scan_store(mod, root)
    mod.apply_reap(reap)
    assert os.path.exists(path)


def test_husk_memory_is_still_reported(mod, root):
    d = os.path.join(root, "global", "memory")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "old-thing.md"), "w", encoding="utf-8") as f:
        f.write('---\ntype: "memory"\nslug: "old-thing"\n'
                'description: "RETIRED: replaced by new-thing"\n'
                'load_behavior: "always"\nmemory_type: "feedback"\n'
                f'updated_at: "{stamp(1)}"\n---\n\nbody\n')
    _, work = scan_store(mod, root)
    assert [w["ref"] for w in work if w["kind"] == "husk-description"] == \
        ["global/old-thing"], work


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        mod = load()
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
