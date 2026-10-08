#!/usr/bin/env python3
'The spent-status match must end at a word boundary: `consumedByAgent` or `stale_copy`\nis not a consumed or stale handoff. Reuses the helpers in test-store-compact.py.\n\n    python3 test-store-compact-edges.py'
import importlib.util
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "test_store_compact", os.path.join(HERE, "test-store-compact.py"))
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load spec for test_store_compact")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)


def test_status_word_with_uppercase_suffix_is_not_spent(mod, root):
    base.write_handoff(root, "global", "a.md", "consumedByAgent 2026-08-01", 30)
    _, work = base.scan_store(mod, root)
    assert base.spent(work) == [], work


def test_status_word_with_digit_or_underscore_suffix_is_not_spent(mod, root):
    base.write_handoff(root, "global", "b.md", "stale_copy 2026-08-01", 30)
    base.write_handoff(root, "global", "c.md", "consumed2 2026-08-01", 30)
    _, work = base.scan_store(mod, root)
    assert base.spent(work) == [], work


def test_status_word_followed_by_punctuation_is_spent(mod, root):
    base.write_handoff(root, "global", "d.md", "stale: already fixed", 30)
    base.write_handoff(root, "global", "e.md", "consumed", 30)
    _, work = base.scan_store(mod, root)
    assert sorted(w["ref"] for w in base.spent(work)) == \
        ["global/handoffs/d.md", "global/handoffs/e.md"], work


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
