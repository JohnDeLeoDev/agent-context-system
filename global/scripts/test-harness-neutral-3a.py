#!/usr/bin/env python3
'Acceptance battery for harness-neutral cleanup, chunk 3a.\n\nContract under test:\n  frontmatter_strip.py        strip_store_frontmatter(path), strip_frontmatter_tree(roots)\n                              (moved out of home-materialize.py, which imports them)\n  harness-materialize.py      mirror_shared_skills(report)   store global/skills -> shared-skills\n                              project_xcode_commands(notes)  store global/commands -> shared-commands,\n                                                             Xcode commands link points there\n                              project_xcode_settings(notes)  rendered from the store seed plus the\n                                                             managed set, never from ~/.claude\nEvery module is loaded under a fixture HOME, so nothing here touches the real one.'
import contextlib
import importlib.util
import json
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
TMP_BASE = Path.home() / ".cache" / "tmp"

SKILL_SRC = (
    "---\nuuid: 1234\nname: alpha\ndescription: Alpha skill\n"
    "allowed_tools: Read\n---\n\n# Alpha\nbody\n"
)
SKILL_OUT = (
    "---\nname: alpha\ndescription: Alpha skill\nallowed-tools: Read\n---\n\n# Alpha\nbody\n"
)
COMMAND_SRC = "---\nuuid: 9999\ndescription: Hand off\n---\n\n# Handoff\nbody\n"
COMMAND_OUT = "# Handoff\nbody\n"
NATIVE_SRC = "---\nname: keep\ndescription: native block\n---\n\n# Keep\n"
PLAIN_SRC = "# No frontmatter\nbody\n"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def need(module: ModuleType, attr: str):
    check(hasattr(module, attr), f"{module.__name__} has no {attr}")
    return getattr(module, attr)


@contextlib.contextmanager
def under_home(home: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in ("HOME", "AGENT_CONTEXT_STORE")}
    os.environ["HOME"] = str(home)
    os.environ.pop("AGENT_CONTEXT_STORE", None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load(filename: str, name: str) -> ModuleType:
    path = SCRIPTS / filename
    check(path.exists(), f"{filename} does not exist")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_home() -> Path:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="neutral3a-"))
    store = home / ".agent-context" / "global"
    write(store / "skills" / "alpha" / "SKILL.md", SKILL_SRC)
    write(store / "skills" / "alpha" / "SKILL.meta.toml", "meta = 1\n")
    write(store / "skills" / "beta" / "notes.md", PLAIN_SRC)
    write(store / "commands" / "handoff.md", COMMAND_SRC)
    seed = {
        "model": "seed-model",
        "marker_seed": True,
        "permissions": {"additionalDirectories": ["/somewhere"], "allow": ["Bash(ls)"]},
    }
    write(store / "settings-seed.json", json.dumps(seed))
    claude = home / ".claude"
    write(claude / "skills" / "poison" / "SKILL.md", "# poison\n")
    write(claude / "commands" / "claude-only.md", "# claude only\n")
    poison = {"POISON": True, "hooks": {"PreToolUse": [
        {"matcher": "X", "hooks": [{"type": "command", "command": "/evil.sh"}]}]}}
    write(claude / "settings.json", json.dumps(poison))
    (home / "Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig").mkdir(parents=True)
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".gemini" / "config").mkdir(parents=True)
    (home / ".copilot").mkdir(parents=True)
    return home


@contextlib.contextmanager
def fixture() -> Iterator[Path]:
    home = make_home()
    try:
        with under_home(home):
            yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def materializer() -> ModuleType:
    return load("harness-materialize.py", "harness_materialize_3a")




def test_strip_module_output() -> None:
    with fixture() as home:
        strip = load("frontmatter_strip.py", "frontmatter_strip_3a")
        tree = home / "tree"
        skill = write(tree / "alpha" / "SKILL.md", SKILL_SRC)
        command = write(tree / "cmd.md", COMMAND_SRC)
        native = write(tree / "native.md", NATIVE_SRC)
        plain = write(tree / "plain.md", PLAIN_SRC)
        text = write(tree / "data.txt", COMMAND_SRC)
        old = time.time() - 86400
        for p in (skill, command):
            os.utime(p, (old, old))
        need(strip, "strip_frontmatter_tree")([str(tree), str(home / "missing")])
        check(skill.read_text() == SKILL_OUT, f"SKILL.md wrong: {skill.read_text()!r}")
        check(command.read_text() == COMMAND_OUT, f"command wrong: {command.read_text()!r}")
        check(native.read_text() == NATIVE_SRC, "a native frontmatter block must be untouched")
        check(plain.read_text() == PLAIN_SRC, "a file with no frontmatter must be untouched")
        check(text.read_text() == COMMAND_SRC, "a non-.md file must be untouched")
        check(int(skill.stat().st_mtime) == int(old), "the source mtime must be preserved")


def test_home_materialize_uses_shared_module() -> None:
    with fixture():
        mod = load("home-materialize.py", "home_materialize_3a")
        for name in ("strip_store_frontmatter", "strip_frontmatter_tree"):
            func = need(mod, name)
            check(func.__module__ == "frontmatter_strip",
                  f"home-materialize {name} is defined in {func.__module__}, not the shared module")




def test_mirror_built_from_store_not_claude() -> None:
    with fixture() as home:
        hm = materializer()
        report: dict = {}
        check(need(hm, "mirror_shared_skills")(report) is True, "mirror reported failure")
        shared = Path(hm.SHARED_SKILLS)
        check((shared / "alpha" / "SKILL.md").exists(), "the store skill alpha was not mirrored")
        check((shared / "alpha" / "SKILL.md").read_text() == SKILL_OUT,
              "alpha/SKILL.md must be the stripped store skill")
        check((shared / "beta" / "notes.md").read_text() == PLAIN_SRC, "beta/notes.md missing")
        check(not (shared / "poison").exists(), "a Claude-only skill leaked into shared-skills")
        check(not (shared / "alpha" / "SKILL.meta.toml").exists(), "store meta file was copied")
        shutil.rmtree(home / ".claude" / "skills")
        report = {}
        check(hm.mirror_shared_skills(report) is True,
              "the mirror must not need ~/.claude/skills to exist")


def test_mirror_prunes_and_ignores_junk() -> None:
    with fixture() as home:
        hm = materializer()
        store_skills = home / ".agent-context" / "global" / "skills"
        need(hm, "mirror_shared_skills")({})
        shared = Path(hm.SHARED_SKILLS)
        check((shared / "beta").is_dir(), "precondition: beta mirrored")
        shutil.rmtree(store_skills / "beta")
        write(store_skills / ".DS_Store", "junk")
        hm.mirror_shared_skills({})
        check(not (shared / "beta").exists(), "a skill removed from the store must leave shared-skills")
        check(not (shared / ".DS_Store").exists(), ".DS_Store must not be mirrored")
        check((shared / "alpha" / "SKILL.md").exists(), "alpha must remain")


def test_empty_store_skills_is_a_finding_without_fallback() -> None:
    with fixture() as home:
        hm = materializer()
        shutil.rmtree(home / ".agent-context" / "global" / "skills")
        shared = Path(hm.SHARED_SKILLS)
        write(shared / "kept" / "SKILL.md", "# kept\n")
        before = len(hm.FINDINGS)
        report: dict = {}
        check(need(hm, "mirror_shared_skills")(report) is False, "an empty store must report failure")
        check(len(hm.FINDINGS) == before + 1, "an empty store skills dir must record one finding")
        check((shared / "kept" / "SKILL.md").exists(), "a failed mirror must touch nothing")
        check(not (shared / "poison").exists(), "must not fall back to ~/.claude/skills")




def test_xcode_commands_come_from_the_store() -> None:
    with fixture() as home:
        hm = materializer()
        xcode = Path(hm.XCODE_CLAUDE)
        os.symlink(home / ".claude" / "commands", xcode / "commands")
        notes: list[str] = []
        need(hm, "project_xcode_commands")(notes)
        link = xcode / "commands"
        check(link.is_symlink(), "Xcode commands must be a link to the projection")
        real = link.resolve()
        check(not str(real).startswith(str((home / ".claude").resolve())),
              f"Xcode commands still resolve into ~/.claude: {real}")
        check(str(real).startswith(str((home / ".agent-context").resolve())),
              f"Xcode commands must resolve into the store checkout: {real}")
        check((link / "handoff.md").read_text() == COMMAND_OUT, "handoff.md must be the stripped store command")
        check(not (link / "claude-only.md").exists(), "a Claude-only command leaked into Xcode")
        (home / ".agent-context" / "global" / "commands" / "handoff.md").unlink()
        hm.project_xcode_commands([])
        check(not (link / "handoff.md").exists(), "a command removed from the store must leave Xcode")




def expected_settings() -> dict:
    sync = load("home-settings-sync.py", "home_settings_sync_3a")
    doc = sync.seed_settings()
    check(doc is not None, "fixture seed did not load")
    doc = sync.sync(doc, None, {})
    perms = dict(doc.get("permissions", {}))
    perms.pop("additionalDirectories", None)
    return dict(doc, permissions=perms)


def test_xcode_settings_ignore_claude_settings() -> None:
    with fixture():
        hm = materializer()
        notes: list[str] = []
        need(hm, "project_xcode_settings")(notes)
        out_path = Path(hm.XCODE_CLAUDE) / "settings.json"
        check(out_path.exists(), "Xcode settings.json was not written")
        out = json.loads(out_path.read_text())
        check("POISON" not in out, "a key from ~/.claude/settings.json reached Xcode")
        check("/evil.sh" not in json.dumps(out), "a hook from ~/.claude/settings.json reached Xcode")
        check(out.get("marker_seed") is True, "the store seed did not reach Xcode")
        check(out == expected_settings(), "Xcode settings differ from the store render")


def test_xcode_settings_shape_and_idempotence() -> None:
    with fixture():
        hm = materializer()
        need(hm, "project_xcode_settings")([])
        out_path = Path(hm.XCODE_CLAUDE) / "settings.json"
        out = json.loads(out_path.read_text())
        check("additionalDirectories" not in out.get("permissions", {}),
              "permissions.additionalDirectories must be dropped")
        commands = [h["command"] for g in out["hooks"]["PreToolUse"] for h in g["hooks"]]
        check(any("hook-dispatch.py" in c for c in commands), "the guard dispatcher is not wired for Xcode")
        before = out_path.stat().st_mtime_ns
        notes: list[str] = []
        hm.project_xcode_settings(notes)
        check(notes == [], f"a second run must be a no-op, notes: {notes}")
        check(out_path.stat().st_mtime_ns == before, "a second run rewrote the file")




def test_other_harness_skill_wiring_unchanged() -> None:
    with fixture() as home:
        hm = materializer()
        claude_skills = home / ".claude" / "skills"
        shutil.rmtree(claude_skills)
        shutil.copytree(home / ".agent-context" / "global" / "skills", claude_skills)
        report: dict = {}
        hm.materialize_skills(report)
        shared = hm.SHARED_SKILLS
        for link in (home / ".config" / "opencode" / "skills", home / ".gemini" / "config" / "skills"):
            check(link.is_symlink() and os.readlink(link) == shared, f"{link} must link to shared-skills")
        cfg = json.loads((home / ".copilot" / "settings.json").read_text())
        check(cfg.get("skillDirectories") == [shared, ".claude/skills"],
              f"copilot skillDirectories changed: {cfg.get('skillDirectories')}")


TESTS: list[Callable[[], None]] = [
    test_strip_module_output,
    test_home_materialize_uses_shared_module,
    test_mirror_built_from_store_not_claude,
    test_mirror_prunes_and_ignores_junk,
    test_empty_store_skills_is_a_finding_without_fallback,
    test_xcode_commands_come_from_the_store,
    test_xcode_settings_ignore_claude_settings,
    test_xcode_settings_shape_and_idempotence,
    test_other_harness_skill_wiring_unchanged,
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
