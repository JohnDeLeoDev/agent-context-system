#!/usr/bin/env python3
'python3 test-store-compact-archive.py'
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "test_store_compact", os.path.join(HERE, "test-store-compact.py"))
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load spec for test_store_compact")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)


NS = uuid.UUID("a6f7c2e0-0000-5000-a000-000000000001")


def stable_uuid(typ, scope, key):
    return str(uuid.uuid5(NS, f"{typ}|{scope}|{key}"))


def write_obs(root, oid, status, days_ago, resolved=True):
    d = os.path.join(root, "global", "audit-observations")
    os.makedirs(d, exist_ok=True)
    rec = {"id": oid, "status": status, "created_at": base.stamp(days_ago),
           "observation": f"obs {oid}"}
    if resolved:
        rec["resolved_date"] = base.stamp(days_ago)
    with open(os.path.join(d, f"{oid:04d}.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)


def active_ids(root):
    d = os.path.join(root, "global", "audit-observations")
    return sorted(int(f[:4]) for f in os.listdir(d)) if os.path.isdir(d) else []


def archived_ids(root):
    d = os.path.join(root, "global", "audit-observations-archive")
    return sorted(int(f[:4]) for f in os.listdir(d)) if os.path.isdir(d) else []


def archived_handoff(root, scope, name):
    return os.path.join(base.docs_dir(root, scope), "archive", "handoffs", name)


def write_live_doc(root, rel, body, scope="global"):
    path = os.path.join(base.docs_dir(root, scope), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'---\ntype: "doc"\npath: "{rel}"\n---\n\n{body}\n')
    return path


def split(path):
    text = open(path, encoding="utf-8").read()
    end = text.find("\n---\n", 4)
    return text[4:end].splitlines(), text[end + 5:]


def sweep(mod, root):
    base.scan_store(mod, root)
    return mod.apply_handoffs()



def test_resolved_and_discarded_old_observations_are_archived(mod, root):
    write_obs(root, 1, "resolved", 40)
    write_obs(root, 2, "discarded", 40)
    write_obs(root, 3, "open", 400, resolved=False)
    write_obs(root, 4, "triaged", 400)
    write_obs(root, 5, "resolved", 5)
    write_obs(root, 6, "discarded", 29)
    reap, _ = base.scan_store(mod, root)
    assert mod.apply_reap(reap) == 2
    assert archived_ids(root) == [1, 2], archived_ids(root)
    assert active_ids(root) == [3, 4, 5, 6], active_ids(root)


def test_discarded_without_resolved_date_ages_from_created_at(mod, root):
    write_obs(root, 8, "discarded", 60, resolved=False)
    reap, _ = base.scan_store(mod, root)
    assert [o["id"] for o in reap] == [8], reap



def test_consumed_handoff_moves_and_is_rekeyed(mod, root):
    src = base.write_handoff(root, "global", "2026-01-01-a.md", "consumed 2026-01-02", 10)
    before_meta, before_body = split(src)
    res = sweep(mod, root)
    dst = archived_handoff(root, "global", "2026-01-01-a.md")
    assert not os.path.exists(src) and os.path.isfile(dst), res
    meta, body = split(dst)
    assert body == before_body
    assert 'path: "archive/handoffs/2026-01-01-a.md"' in meta, meta
    want = stable_uuid("doc", "global", "archive/handoffs/2026-01-01-a.md")
    assert f'uuid: "{want}"' in meta, meta
    
    rest = [ln for ln in before_meta if not ln.startswith(("path:", "uuid:"))]
    assert [ln for ln in meta if not ln.startswith(("path:", "uuid:"))] == rest, meta
    assert res["moved"] == ["global/handoffs/2026-01-01-a.md"], res


def test_stale_handoff_moves_and_open_or_recent_ones_stay(mod, root):
    stale = base.write_handoff(root, "global", "s.md", "stale 2026-01-02: fixed", 30)
    live = base.write_handoff(root, "global", "o.md", "open", 400)
    recent = base.write_handoff(root, "global", "r.md", "consumed", 3)
    sweep(mod, root)
    assert not os.path.exists(stale)
    assert os.path.isfile(archived_handoff(root, "global", "s.md"))
    assert os.path.isfile(live) and os.path.isfile(recent)


def test_every_scope_moves_into_its_own_archive(mod, root):
    ws = base.write_handoff(root, "ws:example-workspace", "w.md", "consumed 2026-01-02", 20)
    pj = base.write_handoff(root, "project:Foo", "p.md", "consumed 2026-01-02", 20)
    sweep(mod, root)
    assert not os.path.exists(ws) and not os.path.exists(pj)
    wmeta, _ = split(archived_handoff(root, "ws:example-workspace", "w.md"))
    pmeta, _ = split(archived_handoff(root, "project:Foo", "p.md"))
    assert f'uuid: "{stable_uuid("doc", "ws:example-workspace", "archive/handoffs/w.md")}"' in wmeta
    assert f'uuid: "{stable_uuid("doc", "project:Foo", "archive/handoffs/p.md")}"' in pmeta



def test_live_pointers_keep_a_handoff_in_place(mod, root):
    a = base.write_handoff(root, "global", "a.md", "consumed 2026-01-02", 20)
    b = base.write_handoff(root, "global", "b.md", "consumed 2026-01-02", 20)
    c = base.write_handoff(root, "global", "c.md", "consumed 2026-01-02", 20)
    d = base.write_handoff(root, "global", "d.md", "consumed 2026-01-02", 20)
    write_live_doc(root, "plan.md",
                   'Read `get_doc("handoffs/a.md")` first. See [[handoffs/b]] and '
                   "[the c handoff](handoffs/c.md). The d handoff was `handoffs/d.md`.")
    res = sweep(mod, root)
    assert os.path.isfile(a) and os.path.isfile(b) and os.path.isfile(c), res
    assert not os.path.exists(d), res
    kept = {k["ref"]: k["reason"] for k in res["kept"]}
    assert set(kept) == {"global/handoffs/a.md", "global/handoffs/b.md",
                         "global/handoffs/c.md"}, kept
    assert all("plan.md" in r for r in kept.values()), kept


def test_a_pointer_from_a_project_doc_with_a_scope_argument_keeps_it(mod, root):
    h = base.write_handoff(root, "project:Foo", "p.md", "consumed 2026-01-02", 20)
    write_live_doc(root, "notes.md", 'Then `get_doc("handoffs/p.md", "Foo")`.')
    sweep(mod, root)
    assert os.path.isfile(h)


def test_pointers_from_archives_or_other_spent_handoffs_do_not_block(mod, root):
    a = base.write_handoff(root, "global", "a.md", "consumed 2026-01-02", 20)
    b = base.write_handoff(root, "global", "b.md", "consumed 2026-01-02", 20)
    with open(a, "a", encoding="utf-8") as f:
        f.write('\nNext: `get_doc("handoffs/b.md")`.\n')
    with open(b, "a", encoding="utf-8") as f:
        f.write('\nPrevious: `get_doc("handoffs/a.md")`.\n')
    write_live_doc(root, "archive/inbox/old.md", 'See `get_doc("handoffs/a.md")`.')
    res = sweep(mod, root)
    assert not os.path.exists(a) and not os.path.exists(b), res


def test_scan_names_the_holder_of_a_kept_handoff(mod, root):
    base.write_handoff(root, "global", "a.md", "consumed 2026-01-02", 20)
    write_live_doc(root, "plan.md", 'Read `get_doc("handoffs/a.md")`.')
    _, work = base.scan_store(mod, root)
    items = base.spent(work)
    assert len(items) == 1 and "plan.md" in items[0]["detail"], items



def test_existing_destination_is_not_overwritten(mod, root):
    src = base.write_handoff(root, "global", "x.md", "consumed 2026-01-02", 20)
    dst = archived_handoff(root, "global", "x.md")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.write("older archived copy\n")
    res = sweep(mod, root)
    assert os.path.isfile(src)
    assert open(dst, encoding="utf-8").read() == "older archived copy\n"
    assert [k["ref"] for k in res["kept"]] == ["global/handoffs/x.md"], res



def test_report_mode_moves_nothing_and_names_apply(mod, root):
    src = base.write_handoff(root, "global", "a.md", "consumed 2026-01-02", 20)
    write_obs(root, 1, "resolved", 40)
    _, work = base.scan_store(mod, root)
    assert os.path.isfile(src) and active_ids(root) == [1]
    assert "store-compact.py --apply" in base.spent(work)[0]["fix"], work


def test_apply_json_reports_both_archives(mod, root):
    store = os.path.join(root, "store")
    base.write_handoff(store, "global", "a.md", "consumed 2026-01-02", 20)
    write_obs(store, 1, "resolved", 40)
    write_obs(store, 2, "discarded", 40)
    env = dict(os.environ, AGENT_CONTEXT_STORE=store)
    p = subprocess.run([sys.executable, base.TARGET, "--apply", "--json"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["archived"]["observations"] == 2, doc
    assert doc["archived"]["handoffs"] == 1, doc
    assert doc["archived"]["kept"] == [], doc
    assert archived_ids(store) == [1, 2]
    assert os.path.isfile(archived_handoff(store, "global", "a.md"))


def test_report_json_carries_no_archived_key(mod, root):
    store = os.path.join(root, "store")
    write_obs(store, 1, "resolved", 40)
    env = dict(os.environ, AGENT_CONTEXT_STORE=store)
    p = subprocess.run([sys.executable, base.TARGET, "--json"],
                       capture_output=True, text=True, env=env, timeout=60)
    doc = json.loads(p.stdout)
    assert doc["reapable"] == 1 and "archived" not in doc, doc
    assert active_ids(store) == [1]


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
