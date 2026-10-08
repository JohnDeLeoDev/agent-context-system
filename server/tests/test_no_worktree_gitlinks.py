'test no worktree gitlinks.'
import subprocess
from pathlib import Path

STORE = Path(__file__).resolve().parents[2]


def _ignored_patterns() -> list[str]:
    return [line.strip() for line in (STORE / ".gitignore").read_text().splitlines()]


def test_agents_worktrees_are_ignored() -> None:
    assert ".agents/worktrees/" in _ignored_patterns()


def test_claude_worktrees_are_ignored() -> None:
    assert ".claude/worktrees/" in _ignored_patterns()


def test_no_gitlink_is_tracked() -> None:
    out = subprocess.run(["git", "-C", str(STORE), "ls-files", "-s"], capture_output=True,
                         text=True, check=True).stdout
    gitlinks = [line.split("\t", 1)[1] for line in out.splitlines() if line.startswith("160000")]
    assert gitlinks == []


def test_the_scratch_directory_is_still_ignored() -> None:
    assert ".agents/tmp/" in _ignored_patterns()
