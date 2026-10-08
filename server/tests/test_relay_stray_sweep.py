"A relay-only host's relay-owned roots hold what the bundle holds and nothing else."
from pathlib import Path

from agent_context import relay_materialize as rm


def _root(home: Path, *parts: str) -> Path:
    return home.joinpath(".agent-context", "global", *parts)


def test_a_file_no_cache_named_is_removed(tmp_path: Path) -> None:
    stale = _root(tmp_path, "commands", "audit-system.md")
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    junk = _root(tmp_path, "scripts", ".DS_Store")
    junk.parent.mkdir(parents=True)
    junk.write_text("x")
    result = rm.apply_bundle({"commands/handoff.md": "new", "scripts/a.py": "print()"}, tmp_path)
    assert not stale.exists() and not junk.exists()
    assert "commands/audit-system.md" in result.pruned
    assert _root(tmp_path, "commands", "handoff.md").read_text() == "new"


def test_an_emptied_skill_directory_goes_too(tmp_path: Path) -> None:
    old = _root(tmp_path, "skills", "gone", "SKILL.md")
    old.parent.mkdir(parents=True)
    old.write_text("x")
    rm.apply_bundle({"skills/kept/SKILL.md": "y"}, tmp_path)
    assert not old.parent.exists()
    assert _root(tmp_path, "skills", "kept", "SKILL.md").is_file()


def test_python_bytecode_and_roots_the_bundle_lacks_are_left_alone(tmp_path: Path) -> None:
    cache = _root(tmp_path, "scripts", "__pycache__", "a.cpython-314.pyc")
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"x")
    hook = _root(tmp_path, "hooks", "local.py")
    hook.parent.mkdir(parents=True)
    hook.write_text("x")
    rm.apply_bundle({"scripts/a.py": "print()"}, tmp_path)
    assert cache.is_file()
    assert hook.is_file(), "a bundle with no hooks key must not empty the hooks root"
