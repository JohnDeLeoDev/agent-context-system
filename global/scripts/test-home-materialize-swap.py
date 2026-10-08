#!/usr/bin/env python3
'test-home-materialize-swap.'
import importlib.util
import os
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
TMP_BASE = Path.home() / ".cache" / "tmp"


def _load_home_materialize():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("home_materialize_under_test",
                                                    SCRIPTS / "home-materialize.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["home_materialize_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _resolved_target(dst: Path) -> Path:
    "The real path dst's symlink chain resolves to, without requiring it to exist."
    target = os.readlink(dst)
    return dst.parent / target if not os.path.isabs(target) else Path(target)


def test_earlier_tag_survives_a_later_tag_in_the_same_run() -> None:
    'test earlier tag survives a later tag in the same run.'
    hm = _load_home_materialize()
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="hms-"))
    try:
        commands_dst = parent / "commands"
        skills_dst = parent / "skills"

        build1 = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="hms-build-"))
        (build1 / "one.md").write_text("first commands generation\n")
        hm.swap_projection_dir(str(commands_dst), str(build1))
        check(commands_dst.is_symlink(), "commands was not symlinked after its first swap")
        commands_gen_1 = _resolved_target(commands_dst)
        check(commands_gen_1.is_dir(), "commands' first generation did not land")

        build2 = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="hms-build-"))
        (build2 / "a.md").write_text("skills generation\n")
        hm.swap_projection_dir(str(skills_dst), str(build2))

        check(commands_dst.is_symlink(), "commands symlink itself vanished")
        live_target = _resolved_target(commands_dst)
        check(live_target.is_dir(),
              f"commands symlink is dangling after skills' swap: -> {live_target} (policy)")
        check((live_target / "one.md").is_file(), "commands' live generation lost its content")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_a_tags_second_generation_frees_its_own_first_generation_only() -> None:
    "The one-generation rollback window still works within a single tag, and still\n    does not touch a sibling tag's generation directories."
    hm = _load_home_materialize()
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="hms-"))
    try:
        commands_dst = parent / "commands"
        agents_dst = parent / "agents"

        for text in ("gen1\n", "gen2\n"):
            build = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="hms-build-"))
            (build / "c.md").write_text(text)
            hm.swap_projection_dir(str(commands_dst), str(build))

        build_agents = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="hms-build-"))
        (build_agents / "a.md").write_text("agents generation\n")
        hm.swap_projection_dir(str(agents_dst), str(build_agents))

        check(commands_dst.is_symlink(), "commands symlink vanished")
        live = _resolved_target(commands_dst)
        check(live.is_dir() and (live / "c.md").read_text() == "gen2\n",
              "commands did not resolve to its second generation")

        gens = [p for p in parent.iterdir()
                if p.name.startswith(hm.GEN_PREFIX + "commands-") and p.is_dir() and not p.is_symlink()]
        check(len(gens) <= 2, f"commands accumulated more than one rollback generation: {gens}")

        check(agents_dst.is_symlink() and _resolved_target(agents_dst).is_dir(),
              "agents' own generation was affected")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


TESTS: list[Callable[[], None]] = [
    test_earlier_tag_survives_a_later_tag_in_the_same_run,
    test_a_tags_second_generation_frees_its_own_first_generation_only,
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
