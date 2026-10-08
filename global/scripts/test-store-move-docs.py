#!/usr/bin/env python3
'Each test builds a throwaway store under TMPDIR, points the module at it, and checks\nwhat a report and an --apply run do to the files.\n\n    python3 test-store-move-docs.py'
import importlib.util
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "store-move-docs.py")
SCOPE = "project:Foo"


def load(root):
    spec = importlib.util.spec_from_file_location("store_move_docs", TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.C.STORE = root
    return mod


def docs(root):
    return os.path.join(root, "projects", "Foo", "docs")


def write_doc(root, path, body, extra=""):
    full = os.path.join(docs(root), *path.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write('---\nuuid: "old"\ntype: "doc"\n' f'path: "{path}"\n'
                'title: "T ' + path + '"\nupdated_at: "2026-01-01T00:00:00Z"\n'
                'scope: "project:Foo"\n' + extra + "---\n\n" + body)
    return full


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


HOLDER = ("See [[projects/Foo/docs/reports/a|the a report]] and "
          '`get_doc("reports/a.md", "Foo")`. Plain text reports/a.md stays.\n')
TYPED = 'depends_on: ["[[projects/Foo/docs/reports/a.md]]", "[[projects/Foo/docs/keep.md]]"]\n'


def fixture(root):
    src = write_doc(root, "reports/a.md", "report body\n")
    holder = write_doc(root, "index.md", HOLDER, extra=TYPED)
    write_doc(root, "keep.md", "kept\n")
    frozen = write_doc(root, "archive/old.md", HOLDER)
    return src, holder, frozen


def test_a_report_run_plans_the_move_and_rewrites_and_changes_nothing(mod, root):
    src, holder, _ = fixture(root)
    before = read(holder)
    res = mod.run_moves(SCOPE, [("reports/a.md", "archive/reports/a.md")], apply=False)
    assert res["moves"] == [{"from": "reports/a.md", "to": "archive/reports/a.md"}], res
    changes = [c for r in res["rewrites"] for c in r["changes"]]
    assert len(changes) == 3, changes
    assert os.path.exists(src) and read(holder) == before
    assert not os.path.exists(os.path.join(docs(root), "archive", "reports", "a.md"))


def test_apply_moves_rekeys_and_rewrites_wikilink_typed_link_and_pointer(mod, root):
    src, holder, frozen = fixture(root)
    frozen_before = read(frozen)
    res = mod.run_moves(SCOPE, [("reports/a.md", "archive/reports/a.md")], apply=True)
    assert res["refused"] == [], res
    dst = os.path.join(docs(root), "archive", "reports", "a.md")
    assert not os.path.exists(src) and os.path.isfile(dst)
    moved = read(dst)
    assert 'path: "archive/reports/a.md"' in moved and moved.endswith("report body\n")
    want = mod.C.stable_uuid("doc", SCOPE, "archive/reports/a.md")
    assert f'uuid: "{want}"' in moved, moved
    text = read(holder)
    assert "[[projects/Foo/docs/archive/reports/a|the a report]]" in text, text
    assert '`get_doc("archive/reports/a.md", "Foo")`' in text, text
    assert '"[[projects/Foo/docs/archive/reports/a.md]]", "[[projects/Foo/docs/keep.md]]"' in text
    assert "Plain text reports/a.md stays." in text
    assert read(frozen) == frozen_before       


def test_a_missing_source_or_existing_target_is_refused_and_the_rest_runs(mod, root):
    fixture(root)
    write_doc(root, "reports/b.md", "b\n")
    write_doc(root, "archive/reports/b.md", "someone else's b\n")
    rows = [("reports/gone.md", "archive/reports/gone.md"),
            ("reports/b.md", "archive/reports/b.md"),
            ("reports/a.md", "archive/reports/a.md"),
            ("reports/a.md", "../escape.md")]
    res = mod.run_moves(SCOPE, rows, apply=True)
    reasons = {r["ref"].split("/", 1)[1] + ">" + r["to"]: r["reason"] for r in res["refused"]}
    assert reasons["reports/gone.md>archive/reports/gone.md"] == "source is missing", reasons
    assert reasons["reports/b.md>archive/reports/b.md"] == "target exists", reasons
    assert "reports/a.md>../escape.md" in reasons, reasons
    assert res["moves"] == [{"from": "reports/a.md", "to": "archive/reports/a.md"}], res
    assert os.path.isfile(os.path.join(docs(root), "archive", "reports", "a.md"))
    assert read(os.path.join(docs(root), "archive", "reports", "b.md")).endswith("someone else's b\n")
    assert mod.main(["--scope", SCOPE, "--file", os.devnull]) == 0


def test_an_interrupted_move_is_finished(mod, root):
    src, _, _ = fixture(root)
    dst = os.path.join(docs(root), "archive", "reports", "a.md")
    os.makedirs(os.path.dirname(dst))
    with open(dst, "w", encoding="utf-8") as f:
        f.write(mod.C._rekey(read(src), SCOPE, "archive/reports/a.md"))
    res = mod.run_moves(SCOPE, [("reports/a.md", "archive/reports/a.md")], apply=True)
    assert res["refused"] == [] and not os.path.exists(src), res


SPLIT_BODY = ("Intro.\n\n## Live\n\nstays\n\n## Old one\n\nold text\n\n### Sub\n\nsub text\n\n"
              "```\n## Not a heading\n```\n\n## Old two\n\nmore\n\n## Tail\n\nend\n")


def test_a_split_moves_named_headings_and_leaves_one_pointer_each(mod, root):
    src = write_doc(root, "tracker/VERIFY.md", SPLIT_BODY)
    res = mod.run_split(SCOPE, "tracker/VERIFY.md", "archive/tracker/VERIFY-old.md",
                        ["Old one", "Old two", "Sub", "Nope"], None, apply=False)
    assert res["sections"] == ["Old one", "Old two"] and not res["applied"], res
    assert {r["heading"] for r in res["refused"]} == {"Sub", "Nope"}, res
    assert read(src).endswith(SPLIT_BODY)

    res = mod.run_split(SCOPE, "tracker/VERIFY.md", "archive/tracker/VERIFY-old.md",
                        ["Old one", "Old two"], None, apply=True)
    assert res["applied"] and res["refused"] == [], res
    text = read(src)
    pointer = 'Moved to `get_doc("archive/tracker/VERIFY-old.md", "Foo")`.'
    assert text.count(pointer) == 2, text
    assert "old text" not in text and "sub text" not in text and "more" not in text
    assert "## Live\n\nstays" in text and "## Tail\n\nend\n" in text and "## Old one\n" in text
    dst = read(os.path.join(docs(root), "archive", "tracker", "VERIFY-old.md"))
    assert 'path: "archive/tracker/VERIFY-old.md"' in dst and 'type: "doc"' in dst
    assert 'scope: "project:Foo"' in dst
    assert "## Old one\n\nold text\n\n### Sub\n\nsub text\n\n```\n## Not a heading\n```" in dst
    assert dst.endswith("## Old two\n\nmore\n"), dst[-80:]


def test_a_second_split_appends_to_the_existing_archive_doc(mod, root):
    write_doc(root, "tracker/VERIFY.md", SPLIT_BODY)
    to = "archive/tracker/VERIFY-old.md"
    mod.run_split(SCOPE, "tracker/VERIFY.md", to, ["Old one"], None, apply=True)
    mod.run_split(SCOPE, "tracker/VERIFY.md", to, ["Old two"], None, apply=True)
    dst = read(os.path.join(docs(root), "archive", "tracker", "VERIFY-old.md"))
    assert dst.count("## Old one") == 1 and dst.endswith("## Old two\n\nmore\n"), dst
    assert dst.index("## Old one") < dst.index("## Old two")


def test_rows_parse_tabs_and_skip_comments(mod, root):
    assert mod.read_rows("# c\n\na.md\tb.md\n") == [("a.md", "b.md")]
    try:
        mod.read_rows("a.md b.md\n")
    except SystemExit:
        return
    raise AssertionError("a row without a tab was accepted")


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as root:
            try:
                fn(load(root), root)
                print(f"ok    {name}")
            except Exception:
                failed += 1
                print(f"FAIL  {name}")
                traceback.print_exc(limit=2)
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
