#!/usr/bin/env python3
"Acceptance battery for home-materialize and directories it does not own (criterion C9).\n\nContract under test:\n  home-materialize.py never deletes, changes or projects into ~/.claude/skills/synced/\n  (Claude Desktop's own bucket). The store's skills still project with store frontmatter\n  stripped, a stale skill directory the store no longer has is still removed, and a stale\n  command is still removed. A second run changes nothing.\nEach case runs the script as a subprocess against a fixture HOME under ~/.cache/tmp, with\nHOME, TMPDIR and AGENT_CONTEXT_STORE pointed at the fixture, so the real HOME is never touched."
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
TMP_BASE = Path.home() / ".cache" / "tmp"

SKILL_SRC = "---\nuuid: 1234\nname: alpha\ndescription: Alpha skill\n---\n\n# Alpha\nbody\n"
SKILL_OUT = "---\nname: alpha\ndescription: Alpha skill\n---\n\n# Alpha\nbody\n"
BUCKET_MARKER = "bucket marker\n"
BUCKET_FILE = "desktop-owned\n"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@contextlib.contextmanager
def fixture() -> Iterator[Path]:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="hmf-"))
    try:
        write(home / ".agent-context" / "global" / "skills" / "alpha" / "SKILL.md", SKILL_SRC)
        write(home / ".claude" / "skills" / "synced" / ".bucket-x", BUCKET_MARKER)
        write(home / ".claude" / "skills" / "synced" / "bucket" / "file.txt", BUCKET_FILE)
        write(home / ".claude" / "skills" / "oldskill" / "SKILL.md", "stale skill\n")
        write(home / ".claude" / "commands" / "stale.md", "stale command\n")
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def run_materialize(home: Path, script: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("HOME", "AGENT_CONTEXT_STORE", "TMPDIR")}
    env["HOME"] = str(home)
    env["AGENT_CONTEXT_STORE"] = str(home / ".agent-context")
    env["TMPDIR"] = str(home / ".cache" / "tmp")
    (home / ".cache" / "tmp").mkdir(parents=True, exist_ok=True)
    return subprocess.run([sys.executable, str(script or SCRIPTS / "home-materialize.py"), "--force"],
                          env=env, capture_output=True, text=True, timeout=120)


def snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        out[rel] = path.read_text() if path.is_file() else "<dir>"
    return out


def test_synced_bucket_survives_byte_identical() -> None:
    with fixture() as home:
        run_materialize(home)
        synced = home / ".claude" / "skills" / "synced"
        check((synced / ".bucket-x").is_file() and (synced / ".bucket-x").read_text() == BUCKET_MARKER,
              "skills/synced/.bucket-x was deleted or changed")
        check((synced / "bucket" / "file.txt").is_file()
              and (synced / "bucket" / "file.txt").read_text() == BUCKET_FILE,
              "skills/synced/bucket/file.txt was deleted or changed")
        check(sorted(p.name for p in synced.iterdir()) == [".bucket-x", "bucket"],
              "skills/synced/ gained or lost entries")


def test_store_skill_projects_with_frontmatter_stripped() -> None:
    with fixture() as home:
        run_materialize(home)
        projected = home / ".claude" / "skills" / "alpha" / "SKILL.md"
        check(projected.is_file(), "skills/alpha/SKILL.md was not projected")
        check(projected.read_text() == SKILL_OUT, f"alpha frontmatter not stripped: {projected.read_text()!r}")


def test_stale_skill_directory_is_removed() -> None:
    with fixture() as home:
        run_materialize(home)
        check(not (home / ".claude" / "skills" / "oldskill").exists(),
              "stale skills/oldskill was not removed")


def test_stale_command_is_removed() -> None:
    with fixture() as home:
        write(home / ".agent-context" / "global" / "commands" / "keep.md", "---\nuuid: 9\n---\n\n# Keep\n")
        run_materialize(home)
        check((home / ".claude" / "commands" / "keep.md").is_file(), "store command did not project")
        check(not (home / ".claude" / "commands" / "stale.md").exists(),
              "stale commands/stale.md was not removed")


def test_second_run_changes_nothing() -> None:
    with fixture() as home:
        run_materialize(home)
        first = {sub: snapshot(home / ".claude" / sub) for sub in ("skills", "commands", "agents")
                 if (home / ".claude" / sub).exists()}
        second_run = run_materialize(home)
        check(second_run.returncode == 0, f"second run failed: {second_run.stderr[-300:]}")
        second = {sub: snapshot(home / ".claude" / sub) for sub in ("skills", "commands", "agents")
                  if (home / ".claude" / sub).exists()}
        check(first == second, "a second run changed the projected tree")
        check(second.get("skills", {}).get("synced/bucket/file.txt") == BUCKET_FILE,
              "skills/synced/bucket/file.txt is missing after two runs")


TESTS: list[Callable[[], None]] = [
    test_synced_bucket_survives_byte_identical,
    test_store_skill_projects_with_frontmatter_stripped,
    test_stale_skill_directory_is_removed,
    test_stale_command_is_removed,
    test_second_run_changes_nothing,
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
