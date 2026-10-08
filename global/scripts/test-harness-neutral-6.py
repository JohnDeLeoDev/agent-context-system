#!/usr/bin/env python3
'Acceptance battery for harness-neutral cleanup, chunk 6.'
import contextlib
import importlib.util
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
ROOT = SCRIPTS.parent.parent
TMP_BASE = Path.home() / ".cache" / "tmp"
LEGACY = re.compile(r"\.claude/(hooks|scripts|docs)\b")
AGENTS = "# Agent context\n\nCritical rules.\n"
WIKIS = "\n<wikis>\n- /x/wiki/index.md\n</wikis>\n"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def fixture() -> Iterator[Path]:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(dir=TMP_BASE, prefix="n6-"))
    keys = ("HOME", "AGENT_CONTEXT_STORE")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["HOME"] = str(home)
    os.environ["AGENT_CONTEXT_STORE"] = str(home / ".agent-context")
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


def projector(home: Path) -> Callable[[], list[str]]:
    hm = load("home-materialize.py", "hm_n6")
    fn = getattr(hm, "project_claude_md", None)
    check(fn is not None, "home-materialize has no project_claude_md()")
    assert fn is not None
    return fn




def test_claude_md_is_written_from_agents_md() -> None:
    with fixture() as home:
        write(home / ".agent-context" / "AGENTS.md", AGENTS)
        projector(home)()
        target = home / ".claude" / "CLAUDE.md"
        check(target.exists(), "~/.claude/CLAUDE.md was not written")
        check(target.read_text() == AGENTS, f"content differs: {target.read_text()!r}")


def test_claude_md_replaces_a_stale_copy() -> None:
    with fixture() as home:
        write(home / ".agent-context" / "AGENTS.md", AGENTS)
        write(home / ".claude" / "CLAUDE.md", "old pre-store guardrails\n")
        projector(home)()
        check((home / ".claude" / "CLAUDE.md").read_text() == AGENTS, "stale text survived")


def test_claude_md_is_an_exact_mirror_and_drops_the_wikis_block() -> None:
    with fixture() as home:
        write(home / ".agent-context" / "AGENTS.md", AGENTS)
        write(home / ".claude" / "CLAUDE.md", "old\n" + WIKIS)
        projector(home)()
        text = (home / ".claude" / "CLAUDE.md").read_text()
        check(text == AGENTS, f"CLAUDE.md is not an exact mirror of AGENTS.md: {text!r}")
        check("<wikis>" not in text, "wikis block survived")


def test_claude_md_is_idempotent() -> None:
    with fixture() as home:
        write(home / ".agent-context" / "AGENTS.md", AGENTS)
        write(home / ".claude" / "CLAUDE.md", "old\n" + WIKIS)
        run = projector(home)
        run()
        first = (home / ".claude" / "CLAUDE.md").read_text()
        run()
        check((home / ".claude" / "CLAUDE.md").read_text() == first, "second run changed the file")


def test_claude_md_without_agents_md_writes_nothing() -> None:
    with fixture() as home:
        write(home / ".claude" / "CLAUDE.md", "keep me\n")
        projector(home)()
        check((home / ".claude" / "CLAUDE.md").read_text() == "keep me\n", "existing file was touched")


def test_main_calls_the_projection() -> None:
    src = (SCRIPTS / "home-materialize.py").read_text()
    body = src[src.index("def main("):]
    check("project_claude_md(" in body, "main() never calls project_claude_md()")




def test_invariant_scans_server_sources() -> None:
    inv = load("invariant-check.py", "inv_n6")
    sites = [str(s) for s in inv._projection_sites()]
    check(any("server/src/agent_context/tokens.py" in s for s in sites),
          "no-claude-projection-paths does not scan server/src/agent_context")


def test_server_text_names_no_retired_paths() -> None:
    hits = []
    for name in ("tokens.py", "entities.py"):
        text = (ROOT / "server" / "src" / "agent_context" / name).read_text()
        hits += [f"{name}:{n}" for n, line in enumerate(text.splitlines(), 1) if LEGACY.search(line)]
    check(not hits, f"server text still names retired paths: {hits}")




def doc(name: str) -> str:
    return (ROOT / "global" / "docs" / name).read_text()


def test_agents_layout_drops_the_scripts_hooks_projection() -> None:
    text = doc("agents-layout.md")
    check("projected back into" not in text, "agents-layout.md still says scripts and hooks are projected back")
    check(not re.search(r"scripts/ hooks/\s+copies", text), "agents-layout.md tree still shows scripts/hooks copies")


def test_docs_state_claude_md_is_generated() -> None:
    text = doc("harness-integration.md")
    says = re.search(r"CLAUDE\.md[^\n]{0,120}generated|generated[^\n]{0,120}CLAUDE\.md", text) is not None
    check("CLAUDE.md" in text and "AGENTS.md" in text and says,
          "harness-integration.md does not say ~/.claude/CLAUDE.md is generated from AGENTS.md")
    layout = doc("agents-layout.md")
    general = re.search(r"home harness directories[^\n]{0,80}generated", layout) is not None
    check(general and "not an authoring location" in layout,
          "agents-layout.md does not say the home harness directories are generated and not authored")


TESTS: list[Callable[[], None]] = [
    test_claude_md_is_written_from_agents_md,
    test_claude_md_replaces_a_stale_copy,
    test_claude_md_is_an_exact_mirror_and_drops_the_wikis_block,
    test_claude_md_is_idempotent,
    test_claude_md_without_agents_md_writes_nothing,
    test_main_calls_the_projection,
    test_invariant_scans_server_sources,
    test_server_text_names_no_retired_paths,
    test_agents_layout_drops_the_scripts_hooks_projection,
    test_docs_state_claude_md_is_generated,
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
