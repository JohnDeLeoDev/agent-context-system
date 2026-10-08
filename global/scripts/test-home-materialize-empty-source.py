#!/usr/bin/env python3
'test-home-materialize-empty-source.'
import importlib.util
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def _load_swap_battery():
    spec = importlib.util.spec_from_file_location("home_materialize_swap_battery",
                                                  SCRIPTS / "test-home-materialize-swap.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


swap = _load_swap_battery()
check = swap.check


def test_empty_build_keeps_the_live_generation() -> None:
    hm = swap._load_home_materialize()
    swap.TMP_BASE.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-"))
    try:
        agents_dst = parent / "agents"
        build1 = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-build-"))
        (build1 / "worker.md").write_text("live roster\n")
        hm.swap_projection_dir(str(agents_dst), str(build1))
        live_before = swap._resolved_target(agents_dst)

        empty = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-build-"))
        hm.swap_projection_dir(str(agents_dst), str(empty))
        shutil.rmtree(empty, ignore_errors=True)

        live_after = swap._resolved_target(agents_dst)
        check(live_after == live_before,
              f"an empty build replaced the live generation: {live_before} -> {live_after} (policy)")
        check((live_after / "worker.md").is_file(), "the live generation lost its content")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_empty_build_promotes_on_first_run() -> None:
    hm = swap._load_home_materialize()
    swap.TMP_BASE.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-"))
    try:
        agents_dst = parent / "agents"
        empty = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-build-"))
        hm.swap_projection_dir(str(agents_dst), str(empty))
        check(agents_dst.is_symlink(), "a first-run empty build was not promoted")
        check(swap._resolved_target(agents_dst).is_dir(), "the first-run generation did not land")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_short_build_is_refused_even_over_an_emptied_live_generation() -> None:
    'test short build is refused even over an emptied live generation.'
    hm = swap._load_home_materialize()
    swap.TMP_BASE.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-"))
    try:
        agents_dst = parent / "agents"
        build1 = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-build-"))
        for name in ("a.md", "b.md"):
            (build1 / name).write_text("roster\n")
        check(hm.swap_projection_dir(str(agents_dst), str(build1), expected=2) is True,
              "a full build was not promoted")
        live = swap._resolved_target(agents_dst)
        for f in live.iterdir():
            f.unlink()

        short = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-build-"))
        (short / "a.md").write_text("roster\n")
        check(hm.swap_projection_dir(str(agents_dst), str(short), expected=2) is False,
              "a build with 1 of 2 store entries was promoted")
        shutil.rmtree(short, ignore_errors=True)
        check(swap._resolved_target(agents_dst) == live, "a refused build moved the symlink")

        full = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-build-"))
        for name in ("a.md", "b.md"):
            (full / name).write_text("roster\n")
        check(hm.swap_projection_dir(str(agents_dst), str(full), expected=2) is True,
              "a full build did not heal an emptied live generation")
        check(sorted(p.name for p in agents_dst.iterdir()) == ["a.md", "b.md"],
              "the healed generation does not hold the store's entries")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_source_entry_count_skips_what_the_projection_drops() -> None:
    hm = swap._load_home_materialize()
    swap.TMP_BASE.mkdir(parents=True, exist_ok=True)
    src = Path(tempfile.mkdtemp(dir=swap.TMP_BASE, prefix="hme-src-"))
    try:
        for name in ("a.md", "a.md.meta.toml", "b.md.meta.json", ".DS_Store"):
            (src / name).write_text("x\n")
        (src / "__pycache__").mkdir()
        (src / "skill-dir").mkdir()
        got = hm.source_entry_count(str(src))
        check(got == 2, f"source_entry_count counted {got}, expected 2 (a.md, skill-dir)")
        check(hm.source_entry_count(str(src / "missing")) == 0, "a missing source is not 0")
    finally:
        shutil.rmtree(src, ignore_errors=True)


TESTS: list[Callable[[], None]] = [
    test_empty_build_keeps_the_live_generation,
    test_empty_build_promotes_on_first_run,
    test_short_build_is_refused_even_over_an_emptied_live_generation,
    test_source_entry_count_skips_what_the_projection_drops,
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
