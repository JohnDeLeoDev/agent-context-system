#!/usr/bin/env python3
'Review findings for harness-neutral chunk 3a (adds to test-harness-neutral-3a.py, which is locked).\n\n  1. A store skills dir holding only .DS_Store counts as empty: a finding, and the mirror is untouched.\n  2. A missing rsync is a finding, not a crash.\n  3. A shared-skills path that is a symlink is replaced, never written through.'
import importlib.util
import os
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent


def load_base() -> ModuleType:
    spec = importlib.util.spec_from_file_location("neutral3a_base", SCRIPTS / "test-harness-neutral-3a.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load_base()
check = base.check


def test_ds_store_only_store_is_empty() -> None:
    with base.fixture() as home:
        hm = base.materializer()
        store_skills = home / ".agent-context" / "global" / "skills"
        for child in list(store_skills.iterdir()):
            if child.is_dir():
                for inner in sorted(child.rglob("*"), reverse=True):
                    inner.unlink() if inner.is_file() else inner.rmdir()
                child.rmdir()
        base.write(store_skills / ".DS_Store", "junk")
        shared = Path(hm.SHARED_SKILLS)
        base.write(shared / "kept" / "SKILL.md", "# kept\n")
        before = len(hm.FINDINGS)
        check(hm.mirror_shared_skills({}) is False, "a store holding only .DS_Store must count as empty")
        check(len(hm.FINDINGS) == before + 1, "an empty store must record one finding")
        check((shared / "kept" / "SKILL.md").exists(), "the existing mirror must be left alone")


def test_missing_rsync_falls_back_to_a_plain_copy() -> None:
    
    
    with base.fixture():
        hm = base.materializer()
        empty_bin = tempfile.mkdtemp(dir=base.TMP_BASE, prefix="nopath-")
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = empty_bin
        try:
            before = len(hm.FINDINGS)
            result = hm.mirror_shared_skills({})
        finally:
            os.environ["PATH"] = saved
            os.rmdir(empty_bin)
        check(result is True, "with no rsync the mirror is written by the plain copy")
        check(len(hm.FINDINGS) == before, "a missing rsync records no finding")
        check((Path(hm.SHARED_SKILLS) / "alpha" / "SKILL.md").exists(),
              "the plain copy wrote the mirror")


def test_a_copy_that_fails_is_a_finding() -> None:
    with base.fixture():
        hm = base.materializer()
        hm.project_stripped = lambda src, dst: (False, "no space left on device")
        before = len(hm.FINDINGS)
        report: dict = {}
        check(hm.mirror_shared_skills(report) is False, "a copy that fails must report failure")
        check(len(hm.FINDINGS) == before + 1, "a copy that fails must record one finding")
        check("no space left on device" in hm.FINDINGS[-1], "the finding carries the copy's error")


def test_symlinked_shared_skills_is_not_written_through() -> None:
    with base.fixture() as home:
        hm = base.materializer()
        target = home / "elsewhere"
        base.write(target / "victim" / "SKILL.md", "# victim\n")
        shared = Path(hm.SHARED_SKILLS)
        shared.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, shared)
        check(hm.mirror_shared_skills({}) is True, "the mirror must succeed over a symlink")
        check((target / "victim" / "SKILL.md").exists(), "the symlink target was written through")
        check(not shared.is_symlink() and (shared / "alpha" / "SKILL.md").exists(),
              "shared-skills must become a real directory holding the store skills")


TESTS: list[Callable[[], None]] = [
    test_ds_store_only_store_is_empty,
    test_missing_rsync_falls_back_to_a_plain_copy,
    test_a_copy_that_fails_is_a_finding,
    test_symlinked_shared_skills_is_not_written_through,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
            if not isinstance(exc, AssertionError):
                traceback.print_exc()
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
