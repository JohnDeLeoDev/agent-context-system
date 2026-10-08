#!/usr/bin/env python3
'Acceptance battery for harness-neutral cleanup, chunks 4 and 5.'
import contextlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
TMP_BASE = Path.home() / ".cache" / "tmp"
CHEZMOI = Path.home() / ".local" / "share" / "chezmoi"
LEGACY = re.compile(r"\.claude/(hooks|scripts|docs)\b")

LEGACY_ALLOWED = {"home-materialize.py", "check-harness-paths.py", "invariant-check.py",
                  "harness_paths.py", "state-migrate.py"}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def fixture() -> Iterator[Path]:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="n45-"))
    keys = ("HOME", "AGENT_CONTEXT_STORE", "HOOK_DISPATCH_REGISTRY")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["HOME"] = str(home)
    os.environ.pop("AGENT_CONTEXT_STORE", None)
    os.environ.pop("HOOK_DISPATCH_REGISTRY", None)
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
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def legacy_hits(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        if path.name in LEGACY_ALLOWED or path.name.startswith("test-"):
            continue
        if LEGACY.search(path.read_text(errors="replace")):
            hits.append(str(path.relative_to(ROOT)))
    return hits




def test_accessors_point_into_the_store() -> None:
    hp = load("harness_paths.py", "hp_n45")
    check(hp.hooks_dir(home="/h") == "/h/.agent-context/global/hooks", f"hooks_dir: {hp.hooks_dir(home='/h')}")
    check(hp.scripts_dir(home="/h") == "/h/.agent-context/global/scripts", f"scripts_dir: {hp.scripts_dir(home='/h')}")
    check(hp.docs_dir(home="/h") == "/h/.agent-context/shared-docs", f"docs_dir: {hp.docs_dir(home='/h')}")
    registry = getattr(hp, "hook_registry_file", None)
    check(registry is not None, "harness_paths has no hook_registry_file()")
    assert registry is not None
    check(registry(home="/h") == "/h/.agent-context/hook-dispatch.json", f"registry: {registry(home='/h')}")


def test_forced_claude_files_stay_in_claude() -> None:
    hp = load("harness_paths.py", "hp_n45_b")
    check(hp.settings_file(home="/h") == "/h/.claude/settings.json", "settings.json must stay in ~/.claude")
    check(hp.skills_dir(home="/h") == "/h/.claude/skills", "skills must stay in ~/.claude")
    check(hp.commands_dir(home="/h") == "/h/.claude/commands", "commands must stay in ~/.claude")
    check(hp.projects_dir(home="/h") == "/h/.claude/projects", "Claude's transcript store must stay put")




def test_dispatcher_reads_the_store_registry() -> None:
    with fixture() as home:
        write(home / ".agent-context" / "hook-dispatch.json", json.dumps({"hooks": {"Stop": [{"hooks": []}]}}))
        disp = load("hook-dispatch.py", "hook_dispatch_n45")
        groups, problem = disp.registry_guards("Stop")
        check(problem is None and groups == [{"hooks": []}], f"registry not read from the store root: {problem}")


def test_dispatcher_ignores_the_old_registry() -> None:
    with fixture() as home:
        write(home / ".claude" / "hook-dispatch.json", json.dumps({"hooks": {"Stop": []}}))
        disp = load("hook-dispatch.py", "hook_dispatch_n45_b")
        groups, problem = disp.registry_guards("Stop")
        check(groups is None and problem is not None and "missing" in problem,
              f"an old-location registry must not be read: {groups} {problem}")


def test_registry_env_override_still_wins() -> None:
    with fixture() as home:
        other = write(home / "elsewhere.json", json.dumps({"hooks": {"Stop": [{"hooks": [1]}]}}))
        os.environ["HOOK_DISPATCH_REGISTRY"] = str(other)
        disp = load("hook-dispatch.py", "hook_dispatch_n45_c")
        groups, _ = disp.registry_guards("Stop")
        check(groups == [{"hooks": [1]}], "HOOK_DISPATCH_REGISTRY must still override")


def test_registry_path_is_the_store_file() -> None:
    with fixture() as home:
        reg = load("hook-registry.py", "hook_registry_n45")
        got = reg.registry_path(str(home / "anywhere" / "settings.json"))
        check(got == str(home / ".agent-context" / "hook-dispatch.json"), f"registry_path returned {got}")


def test_settings_sync_writes_the_registry_to_the_store_root() -> None:
    with fixture() as home:
        write(home / ".claude" / "settings.json", "{}")
        (home / ".agent-context" / "global" / "hooks").mkdir(parents=True)
        mod = load("home-settings-sync.py", "home_settings_sync_n45")
        old_argv = sys.argv
        sys.argv = ["home-settings-sync.py"]
        try:
            mod.main()
        finally:
            sys.argv = old_argv
        check((home / ".agent-context" / "hook-dispatch.json").exists(), "registry not written to the store root")
        check(not (home / ".claude" / "hook-dispatch.json").exists(), "registry written to the old location")




def test_settings_commands_use_store_paths() -> None:
    with fixture() as home:
        (home / ".agent-context" / "global" / "hooks").mkdir(parents=True)
        mod = load("home-settings-sync.py", "home_settings_sync_n45_b")
        settings = mod.sync({}, None, {})
        commands = [h.get("command", "") for groups in settings["hooks"].values()
                    for g in groups for h in g["hooks"]]
        check(len(commands) > 0, "sync produced no hook commands")
        bad = [c for c in commands if LEGACY.search(c)]
        check(not bad, "settings.json still wires ~/.claude paths: " + "; ".join(bad[:3]))
        dispatch = [c for c in commands if "hook-dispatch.py" in c]
        check(len(dispatch) > 0 and all(".agent-context/global/scripts/hook-dispatch.py" in c for c in dispatch),
              f"dispatcher commands must name the store script: {dispatch}")


def test_pi_extension_renders_store_paths() -> None:
    with fixture() as home:
        mod = load("harness-materialize.py", "harness_materialize_n45")
        manifest = json.loads((ROOT / "hooks-manifest.json").read_text())
        text = str(mod.render_pi_hooks(manifest, str(home), str(ROOT / "commands")))
        check("hook-dispatch.py" in text, "the pi extension does not name the dispatcher")
        check(not LEGACY.search(text), "the pi extension still names a ~/.claude directory")




def test_manifest_and_seed_have_no_legacy_paths() -> None:
    for name in ("hooks-manifest.json", "settings-seed.json"):
        text = (ROOT / name).read_text()
        check(not LEGACY.search(text), f"{name} still names a ~/.claude hooks, scripts or docs path")




def test_home_materialize_stops_projecting_hooks_and_scripts() -> None:
    source = (SCRIPTS / "home-materialize.py").read_text()
    check('os.path.join(CL, "hooks")' not in source and 'os.path.join(CL, "scripts")' not in source,
          "home-materialize.py still projects into ~/.claude/hooks or ~/.claude/scripts")
    check("retire_claude_projections(" in source and source.count("retire_shared_docs(") >= 2,
          "main() must call retire_shared_docs() and retire_claude_projections()")
    check("project_shared_docs" not in source,
          "docs are read over MCP: home-materialize must not project them")


def test_shared_docs_retired() -> None:
    with fixture() as home:
        root = home / ".agent-context"
        write(root / "shared-docs" / "a.md", "body-a\n")
        write(root / "shared-docs" / "p" / "c.md", "body-c\n")
        write(root / "global" / "docs" / "a.md", "---\nuuid: x\ntitle: A\n---\nbody-a\n")
        mod = load("home-materialize.py", "home_materialize_n45")
        run = getattr(mod, "retire_shared_docs", None)
        check(run is not None, "home-materialize.py has no retire_shared_docs()")
        assert run is not None
        notes = run()
        check(isinstance(notes, list) and len(notes) > 0, "a retirement that removed things must report it")
        check(not (root / "shared-docs").exists(), "the shared-docs tree must be gone")
        check((root / "global" / "docs" / "a.md").exists(), "the store's own docs must stay")
        check(run() == [], "a second run has nothing to do and says nothing")




def test_retire_removes_the_projections_and_keeps_the_rest() -> None:
    with fixture() as home:
        for sub in ("hooks", "scripts", "docs"):
            write(home / ".claude" / sub / "x.py", "x")
        write(home / ".claude" / "hook-dispatch.json", "{}")
        write(home / ".claude" / "settings.json", "{}")
        write(home / ".claude" / "skills" / "s" / "SKILL.md", "s")
        mod = load("home-materialize.py", "home_materialize_n45_b")
        retire = getattr(mod, "retire_claude_projections", None)
        check(retire is not None, "home-materialize.py has no retire_claude_projections()")
        assert retire is not None
        notes = retire()
        check(len(notes) > 0, "a retirement that removed things must report it")
        for sub in ("hooks", "scripts", "docs"):
            check(not (home / ".claude" / sub).exists(), f"~/.claude/{sub} must be gone")
        check(not (home / ".claude" / "hook-dispatch.json").exists(), "the old registry must be gone")
        check((home / ".claude" / "settings.json").exists(), "settings.json must stay")
        check((home / ".claude" / "skills" / "s" / "SKILL.md").exists(), "skills must stay")
        check(retire() == [], "a second run must report nothing")




def test_chezmoi_wrappers_use_store_paths() -> None:
    if not CHEZMOI.is_dir():
        return
    names = ("dot_local/bin/executable_token-usage-collect", "dot_local/bin/executable_agent-notify-watch",
             "dot_config/systemd/user/token-usage-collect.service")
    hits = [n for n in names if (CHEZMOI / n).exists() and LEGACY.search((CHEZMOI / n).read_text())]
    check(not hits, "chezmoi files still exec ~/.claude paths: " + ", ".join(hits))




def test_no_legacy_paths_in_scripts_and_hooks() -> None:
    files = sorted((ROOT / "scripts").glob("*.py")) + sorted((ROOT / "hooks").glob("*.py"))
    hits = legacy_hits(files)
    check(not hits, "still names a ~/.claude hooks, scripts or docs path: " + ", ".join(hits))


def test_no_legacy_paths_in_commands_skills_docs() -> None:
    files = (sorted((ROOT / "commands").glob("*.md")) + sorted((ROOT / "skills").glob("*/SKILL.md"))
             + sorted((ROOT / "docs").glob("*.md")))
    hits = legacy_hits(files)
    check(not hits, "still names a ~/.claude hooks, scripts or docs path: " + ", ".join(hits))


def test_gitignore_covers_the_new_projections() -> None:
    lines = {ln.strip() for ln in (ROOT.parent / ".gitignore").read_text().splitlines()}
    check("shared-docs/" in lines, ".gitignore lacks shared-docs/")
    check("hook-dispatch.json" in lines, ".gitignore lacks hook-dispatch.json")




def test_invariant_rule_exists() -> None:
    check("no-claude-projection-paths" in (SCRIPTS / "invariant-check.py").read_text(),
          "invariant-check.py has no no-claude-projection-paths rule")


TESTS: list[Callable[[], None]] = [
    test_accessors_point_into_the_store,
    test_forced_claude_files_stay_in_claude,
    test_dispatcher_reads_the_store_registry,
    test_dispatcher_ignores_the_old_registry,
    test_registry_env_override_still_wins,
    test_registry_path_is_the_store_file,
    test_settings_sync_writes_the_registry_to_the_store_root,
    test_settings_commands_use_store_paths,
    test_pi_extension_renders_store_paths,
    test_manifest_and_seed_have_no_legacy_paths,
    test_home_materialize_stops_projecting_hooks_and_scripts,
    test_shared_docs_retired,
    test_retire_removes_the_projections_and_keeps_the_rest,
    test_chezmoi_wrappers_use_store_paths,
    test_no_legacy_paths_in_scripts_and_hooks,
    test_no_legacy_paths_in_commands_skills_docs,
    test_gitignore_covers_the_new_projections,
    test_invariant_rule_exists,
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
