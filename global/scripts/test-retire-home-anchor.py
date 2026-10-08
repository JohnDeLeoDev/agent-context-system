#!/usr/bin/env python3
'test-retire-home-anchor.'
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent
TMP_BASE = Path.home() / ".cache" / "tmp"
OLD_DIRS = ("hooks", "scripts", "docs")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def fixture() -> Iterator[Path]:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="ra-"))
    saved = {k: os.environ.get(k) for k in ("HOME", "AGENT_CONTEXT_STORE")}
    os.environ["HOME"] = str(home)
    os.environ["AGENT_CONTEXT_STORE"] = str(home / ".agent-context")
    try:
        for name in OLD_DIRS:
            (home / ".claude" / name).mkdir(parents=True)
            (home / ".claude" / name / "x").write_text("old")
        yield home
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(home, ignore_errors=True)


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hm_ra", SCRIPTS / "home-materialize.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hm_ra"] = module
    spec.loader.exec_module(module)
    return module


def settings(home: Path, text: str) -> None:
    (home / ".claude" / "settings.json").write_text(text)


def survivors(home: Path) -> list[str]:
    return [n for n in OLD_DIRS if (home / ".claude" / n).exists()]


def test_project_docs_permission_does_not_block_retire() -> None:
    with fixture() as home:
        settings(home, '{"permissions": {"allow": ["Edit(/Users/x/Dev/Proj/.claude/docs/api/**)"]}}')
        load().retire_claude_projections()
        check(survivors(home) == [], f"retire was blocked by a project path: {survivors(home)}")


def test_home_anchored_forms_still_block_retire() -> None:
    forms = ["~/.claude/hooks/a.py", "$HOME/.claude/scripts/a.py", "${HOME}/.claude/docs/a.md",
             "%h/.claude/scripts/a.py", "{home}/.claude/hooks/a.py"]
    for form in forms:
        with fixture() as home:
            settings(home, '{"command": "%s"}' % form.replace("{home}", str(home)))
            notes = load().retire_claude_projections()
            check(len(survivors(home)) == 3, f"{form} did not keep the copies: {survivors(home)}")
            check(any("kept" in n for n in notes), f"{form}: no kept report: {notes}")


def test_another_users_absolute_home_does_not_block() -> None:
    with fixture() as home:
        settings(home, '{"command": "/Users/someone-else/Dev/P/.claude/scripts/a.py"}')
        load().retire_claude_projections()
        check(survivors(home) == [], f"a foreign absolute path blocked retire: {survivors(home)}")


TESTS: list[Callable[[], None]] = [
    test_project_docs_permission_does_not_block_retire,
    test_home_anchored_forms_still_block_retire,
    test_another_users_absolute_home_does_not_block,
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
