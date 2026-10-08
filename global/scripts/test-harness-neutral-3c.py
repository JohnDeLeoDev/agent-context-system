#!/usr/bin/env python3
'Acceptance battery for harness-neutral cleanup, chunk 3c.\n\nContract under test:\n  harness_paths.state_dir(home)     -> <home>/.local/state/agent-context\n  state-migrate.py                  migrate(home=None) -> list[str] notes. Moves ~/.claude/state into\n                                    the new state dir. Idempotent. Where a name exists on both sides the\n                                    newer file wins and a note says so. The old dir is removed when empty.\n                                    No symlink is left behind.\n  home-materialize.py               run_state_migration() -> list[str], called from main() at session start\n  commands and docs                 name the new path, never ~/.claude/state'
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
TMP_BASE = Path.home() / ".cache" / "tmp"
OLD = "claude/state"  


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def fixture() -> Iterator[Path]:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="neutral3c-"))
    saved = {k: os.environ.get(k) for k in ("HOME", "AGENT_CONTEXT_STORE")}
    os.environ["HOME"] = str(home)
    os.environ.pop("AGENT_CONTEXT_STORE", None)
    try:
        yield home
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(home, ignore_errors=True)


def load(filename: str, name: str) -> ModuleType:
    path = SCRIPTS / filename
    check(path.exists(), f"{filename} does not exist")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, text: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def new_state(home: Path) -> Path:
    return home / ".local" / "state" / "agent-context"


def old_state(home: Path) -> Path:
    return home / ".claude" / "state"




def test_state_dir_is_the_new_location() -> None:
    hp = load("harness_paths.py", "harness_paths_3c")
    check(hp.state_dir(home="/h") == "/h/.local/state/agent-context",
          f"state_dir returned {hp.state_dir(home='/h')}")
    with fixture() as home:
        check(hp.state_dir() == str(new_state(home)), "state_dir must follow $HOME at call time")




def test_migration_moves_everything() -> None:
    with fixture() as home:
        old = old_state(home)
        write(old / "audit" / "last-x", "a")
        write(old / "health" / "mcp.json", '{"ok": true}')
        write(old / "lsp" / "cwd" / "deep" / "f", "deep")
        write(old / "top-level-file", "t")
        write(home / ".claude" / "settings.json", "{}")
        mig = load("state-migrate.py", "state_migrate_3c")
        notes = mig.migrate()
        new = new_state(home)
        check((new / "audit" / "last-x").read_text() == "a", "audit file missing")
        check((new / "health" / "mcp.json").read_text() == '{"ok": true}', "health file missing")
        check((new / "lsp" / "cwd" / "deep" / "f").read_text() == "deep", "nested file missing")
        check((new / "top-level-file").read_text() == "t", "top-level file missing")
        check(not old.exists() and not old.is_symlink(), "the old state dir must be gone, with no symlink")
        check((home / ".claude" / "settings.json").exists(), "unrelated ~/.claude files must stay")
        check(isinstance(notes, list) and len(notes) > 0, "a migration that moved things must report it")


def test_migration_is_idempotent() -> None:
    with fixture() as home:
        write(old_state(home) / "health" / "a.json", "1")
        mig = load("state-migrate.py", "state_migrate_3c_b")
        mig.migrate()
        before = (new_state(home) / "health" / "a.json").stat().st_mtime_ns
        check(mig.migrate() == [], "a second run must report nothing")
        check((new_state(home) / "health" / "a.json").stat().st_mtime_ns == before, "second run rewrote a file")


def test_migration_without_old_dir_is_a_no_op() -> None:
    with fixture() as home:
        mig = load("state-migrate.py", "state_migrate_3c_c")
        check(mig.migrate() == [], "no old state dir must be a quiet no-op")
        check(not new_state(home).exists(), "a no-op must not create the new dir")


def test_migration_merges_and_newer_file_wins() -> None:
    with fixture() as home:
        now = time.time()
        old, new = old_state(home), new_state(home)
        write(old / "health" / "newer-old.json", "old-wins", now)
        write(new / "health" / "newer-old.json", "new-loses", now - 1000)
        write(old / "health" / "older-old.json", "old-loses", now - 1000)
        write(new / "health" / "older-old.json", "new-wins", now)
        write(old / "health" / "only-old.json", "moved")
        write(new / "health" / "only-new.json", "kept")
        mig = load("state-migrate.py", "state_migrate_3c_d")
        notes = mig.migrate()
        check((new / "health" / "newer-old.json").read_text() == "old-wins", "a newer old file must win")
        check((new / "health" / "older-old.json").read_text() == "new-wins", "a newer new file must win")
        check((new / "health" / "only-old.json").read_text() == "moved", "an old-only file must move")
        check((new / "health" / "only-new.json").read_text() == "kept", "a new-only file must stay")
        check(any("conflict" in n for n in notes), f"conflicts must be logged: {notes}")
        check(not old.exists(), "the old dir must be removed once everything is merged")


def test_migration_type_clash_keeps_both_and_reports() -> None:
    with fixture() as home:
        old, new = old_state(home), new_state(home)
        write(old / "clash" / "inner", "dir-side")
        write(new / "clash", "file-side")
        mig = load("state-migrate.py", "state_migrate_3c_e")
        notes = mig.migrate()
        check((new / "clash").read_text() == "file-side", "the existing destination must not be replaced")
        check((old / "clash" / "inner").read_text() == "dir-side", "an unmovable entry must stay in the old dir")
        check(any("clash" in n for n in notes), f"the clash must be reported: {notes}")




def test_home_materialize_runs_the_migration() -> None:
    with fixture() as home:
        write(old_state(home) / "health" / "x.json", "x")
        mod = load("home-materialize.py", "home_materialize_3c")
        run = getattr(mod, "run_state_migration", None)
        check(run is not None, "home-materialize.py has no run_state_migration()")
        assert run is not None
        notes = run()
        check(isinstance(notes, list), "run_state_migration must return a list")
        check((new_state(home) / "health" / "x.json").exists(), "the migration did not run")
        source = (SCRIPTS / "home-materialize.py").read_text()
        check(source.count("run_state_migration(") >= 2, "main() must call run_state_migration()")




LEGACY_ALLOWED = {"state-migrate.py", "check-harness-paths.py", "hook-test-cases.py", "hook-parity-run.py"}


def test_no_stale_path_in_hooks_scripts_commands_docs() -> None:
    hits: list[str] = []
    for folder in ("hooks", "scripts"):
        for path in sorted((ROOT / folder).glob("*.py")):
            if path.name.startswith("test-") or path.name in LEGACY_ALLOWED:
                continue
            if OLD in path.read_text(errors="replace"):
                hits.append(f"{folder}/{path.name}")
    for path in sorted((ROOT / "commands").glob("*.md")):
        if OLD in path.read_text(errors="replace"):
            hits.append(f"commands/{path.name}")
    for path in sorted((ROOT / "docs").glob("*.md")):
        if OLD in path.read_text(errors="replace"):
            hits.append(f"docs/{path.name}")
    check(not hits, "still names the legacy state path: " + ", ".join(hits))


def test_commands_use_the_new_path() -> None:
    for name in ("context-audit", "verify-project-memory"):
        text = (ROOT / "commands" / f"{name}.md").read_text()
        check(".local/state/agent-context" in text, f"{name}.md does not use the new state path")


TESTS: list[Callable[[], None]] = [
    test_state_dir_is_the_new_location,
    test_migration_moves_everything,
    test_migration_is_idempotent,
    test_migration_without_old_dir_is_a_no_op,
    test_migration_merges_and_newer_file_wins,
    test_migration_type_clash_keeps_both_and_reports,
    test_home_materialize_runs_the_migration,
    test_no_stale_path_in_hooks_scripts_commands_docs,
    test_commands_use_the_new_path,
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
