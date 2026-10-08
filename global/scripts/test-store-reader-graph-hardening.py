#!/usr/bin/env python3
'Battery for the hardening of store-reader-graph.py (items B1, B2, H1 to H10).\n\nAdditive to test-store-reader-graph.py and test-store-reader-graph-reach.py (both locked):\nthis file checks only the new behavior. Each false-removable case has a fixture where the\nwrong answer is a key marked unreachable, test-only or no-reader-found. Fixtures live under\n~/.cache/tmp and are removed at the end. stdlib only.'

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "store-reader-graph.py")
LAPTOP_ROOT = "laptop launchers and units (not inspected)"
ALL_CHECKED = ["--checked", "m4", "--checked", "rp", "--checked", "pc",
               "--checked", "mirror-a", "--checked", "mirror-b"]
BLIND_SPOTS = [
    "chezmoi source (~/.local/share/chezmoi)",
    "crontab",
    "shell rc files (.zshenv, .zshrc, .bash_profile, .profile)",
    "~/.claude/hooks, commands and scripts directories",
    "symlinked directories (not followed)",
]
POINTER = "see scan_blind_spots"

scratch_root = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
os.makedirs(scratch_root, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="store-reader-graph-hardening-test-", dir=scratch_root)

passed = 0
failures: list[str] = []
roots: dict[str, str] = {}


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def write(root: str, rel: str, content: str | bytes) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content if isinstance(content, bytes) else content.encode())
    return path


def build(name: str, files: dict[str, str | bytes]) -> str:
    root = os.path.join(tmp, name)
    for rel, content in files.items():
        write(root, rel, content)
    for sub in ("store", "home", "outdir"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    roots[name] = root
    return root


def snapshot(*dirs: str) -> dict[str, str]:
    state: dict[str, str] = {}
    for top in dirs:
        for base, _dirs, names in os.walk(top):
            for name in names:
                path = os.path.join(base, name)
                with open(path, "rb") as handle:
                    state[path] = hashlib.sha256(handle.read()).hexdigest()
    return state


def run_tool(args: list[str], env_overrides: dict[str, str] | None = None) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.pop("AGENT_CONTEXT_STORE", None)
    env.update(env_overrides or {})
    proc = subprocess.run([sys.executable, TOOL] + args, capture_output=True,
                          text=True, env=env, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def run_json(root: str, extra: list[str] | None = None) -> tuple[int, Any, str]:
    args = ["--store", os.path.join(root, "store"), "--home", os.path.join(root, "home"),
            "--json"] + (extra or [])
    rc, out, err = run_tool(args, {"HOME": root})
    try:
        return rc, json.loads(out), err
    except ValueError:
        return rc, None, err + out[:300]


def case(name: str, files: dict[str, str | bytes], extra: list[str] | None = None) -> tuple[str, Any]:
    root = build(name, files)
    rc, report, err = run_json(root, extra)
    check("%s: --json run exits 0 and prints a JSON object" % name,
          rc == 0 and isinstance(report, dict), "rc=%s %s" % (rc, err[:200]))
    return root, report


def entry(report: Any, cls: str, key: str) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    section = (report.get("classes") or {}).get(cls)
    keys = section.get("keys") if isinstance(section, dict) else None
    found = keys.get(key) if isinstance(keys, dict) else None
    return found if isinstance(found, dict) else None


def reach_is(report: Any, cls: str, key: str, expected: str) -> tuple[bool, str]:
    info = entry(report, cls, key)
    got = info.get("reach") if info else None
    return got == expected, "reach %r, want %r" % (got, expected)


def verdict_is(report: Any, cls: str, key: str, expected: str) -> tuple[bool, str]:
    info = entry(report, cls, key)
    got = info.get("verdict") if info else None
    return got == expected, "verdict %r, want %r" % (got, expected)


def exec_readers(report: Any, cls: str, key: str, suffix: str = "") -> list[dict[str, Any]]:
    info = entry(report, cls, key)
    readers = info.get("readers") if info else None
    if not isinstance(readers, list):
        return []
    return [r for r in readers if isinstance(r, dict) and r.get("kind") == "exec"
            and str(r.get("file", "")).endswith(suffix)]


def exec_from(report: Any, cls: str, key: str, suffix: str, via: str,
              line: int | None = None) -> tuple[bool, str]:
    found = exec_readers(report, cls, key, suffix)
    ok = any(r.get("via") == via and (line is None or r.get("line") == line) for r in found)
    return ok, "exec readers from %s: %s" % (suffix, [(r.get("via"), r.get("line")) for r in found])


def loader_lines(report: Any) -> list[dict[str, Any]]:
    found = report.get("loader_lines") if isinstance(report, dict) else None
    return [ln for ln in found if isinstance(ln, dict)] if isinstance(found, list) else []


def loaders_in(report: Any, suffix: str, line: int | None = None) -> list[dict[str, Any]]:
    return [ln for ln in loader_lines(report) if str(ln.get("file", "")).endswith(suffix)
            and (line is None or ln.get("line") == line)]


def unreadable_files(report: Any) -> list[dict[str, Any]]:
    found = report.get("unreadable") if isinstance(report, dict) else None
    return [u for u in found if isinstance(u, dict)] if isinstance(found, list) else []


def is_utf8(blob: bytes) -> bool:
    try:
        blob.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def line_of(content: str, needle: str) -> int:
    return next(i for i, ln in enumerate(content.splitlines(), 1) if needle in ln)


def runs(*names: str) -> str:
    return "import subprocess\n" + "".join(
        'subprocess.run(["python3", "%s"])\n' % name for name in names)


def settings_json(hook_names: tuple[str, ...] = (), script_names: tuple[str, ...] = ()) -> str:
    commands = ["python3 $HOME/.agent-context/global/hooks/" + n for n in hook_names] \
        + ["python3 $HOME/.agent-context/global/scripts/" + n for n in script_names]
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": c} for c in commands]}]}}, indent=2) + "\n"


def script_path(name: str) -> str:
    return "$HOME/.agent-context/global/scripts/" + name


G = "store/global/"


LISTING_LOADER = (
    "import os\n"
    "STORE = os.path.expanduser(\"~/.agent-context\")\n"
    "for n in os.listdir(os.path.join(STORE, \"global\", \"hooks\")):\n"
    "    pass\n")


def glob_loader(directory: str, extension: str) -> str:
    return ("import glob, os\n"
            "ROOT = os.path.expanduser(\"~/.agent-context/global\")\n"
            "for path in glob.glob(os.path.join(ROOT, \"%s/*.%s\")):\n"
            "    pass\n" % (directory, extension))


def fstring_loader(extension: str) -> str:
    return ("import subprocess\n"
            "SCRIPTS = \"/x\"\n"
            "for name in (\"a\",):\n"
            "    subprocess.run([\"bash\", f\"{SCRIPTS}/{name}.%s\"])\n" % extension)



TEST_LOADERS = {
    "test-hook-list.py": (LISTING_LOADER, "os.listdir"),
    "test_glob_hooks.py": (glob_loader("hooks", "py"), "glob.glob"),
    "test_docs_glob.py": (glob_loader("docs", "md"), "glob.glob"),
    "hook-test-cases.py": (glob_loader("commands", "md"), "glob.glob"),
    "hook-test-run.py": (fstring_loader("sh"), "{name}.sh"),
}
NON_TEST_LOADERS = {
    "loader-hooks-sh.py": (glob_loader("hooks", "sh"), "glob.glob"),
    "loader-real-ts.py": (fstring_loader("ts"), "{name}.ts"),
    "latest-loader.py": (glob_loader("agents", "md"), "glob.glob"),
}
B1_FILES: dict[str, str | bytes] = {
    G + "hooks/hook-rooted.py": "print('rooted')\n",
    G + "hooks/hook-glob-only.py": "print('glob only')\n",
    G + "hooks/hook-test-read.py": "print('read by a test')\n",
    G + "hooks/hook-mixed.sh": "echo mixed\n",
    G + "scripts/test-runs-hook.py": runs("hook-test-read.py"),
    G + "scripts/dyn-by-hook-test.sh": "echo target\n",
    G + "scripts/dyn-control.ts": "// control\n",
    G + "commands/cmd-only-test-loaded.md": "# Cmd\n",
    G + "agents/agent-latest.md": "# Agent\n",
    "store/shared-docs/doc-only-test-loaded.md": "# Doc\n",
    "home/.claude/settings.json": settings_json(("hook-rooted.py",)),
}
for _name, (_content, _needle) in {**TEST_LOADERS, **NON_TEST_LOADERS}.items():
    B1_FILES[G + "scripts/" + _name] = _content


CASE_SH = (
    "#!/bin/sh\n"
    "case \"$1\" in\n"
    "  one) echo one ;;\n"
    "*) python3 \"" + script_path("x-case-target.py") + "\" ;;\n"
    "esac\n"
    "# python3 \"" + script_path("x-hash-comment.py") + "\"\n")
STAR_PY = (
    "import subprocess\n"
    "subprocess.run([\"python3\", \"-u\",\n"
    "    *[\"x-star-target.py\"]])\n")
DOCBLOCK_JS = "/**\n * runs x-doc-target.py for reports\n */\n"


HOOK_LATIN = (b"# caf\xe9 latin-1 comment\nimport subprocess\n"
              b"subprocess.run([\"python3\", \"x-hook-latin-target.py\"])\n")
TOOL_LATIN = (b"#!/bin/sh\n# \xff\xfe bytes\npython3 \"" + script_path("x-latin-target.py").encode()
              + b"\"\n")
TOOL_BIG = ("#!/bin/sh\n" + ("# " + "p" * 78 + "\n") * 32000
            + "python3 \"" + script_path("x-big-target.py") + "\"\n").encode()


SKILL_MD = (
    "# skill-runner\n\n"
    "Prose mention: the script x-skill-prose.py exists.\n\n"
    "```bash\n"
    "python3 \"" + script_path("x-skill-fence.py") + "\"\n"
    "```\n\n"
    "```\n"
    "python3 \"" + script_path("x-skill-bare.py") + "\"\n"
    "```\n\n"
    "!python3 ~/.agent-context/global/scripts/x-skill-bang.py\n")
AGENT_MD = (
    "Prose mention: x-agent-prose.py is described here.\n\n"
    "```sh\n"
    "python3 \"" + script_path("x-agent-fence.py") + "\"\n"
    "```\n\n"
    "!python3 ~/.agent-context/global/scripts/x-agent-bang.py\n")
COMMAND_MD = (
    "Intro.\n\n"
    "!python3 ~/.agent-context/global/scripts/x-cmd-bang.py\n\n"
    "```\n"
    "python3 ~/.agent-context/global/scripts/x-cmd-bare.py\n"
    "```\n\n"
    "```bash\n"
    "python3 ~/.agent-context/global/scripts/x-cmd-bash.py\n"
    "```\n\n"
    "```json\n"
    "{\"run\": \"x-cmd-json-mention.py\"}\n"
    "```\n")
H4_TARGETS = ("x-skill-fence.py", "x-skill-bare.py", "x-skill-bang.py", "x-skill-prose.py",
              "x-agent-fence.py", "x-agent-bang.py", "x-agent-prose.py", "x-cmd-bang.py",
              "x-cmd-bare.py", "x-cmd-bash.py", "x-cmd-json-mention.py")


CALL = "python3 ~/.agent-context/global/scripts/"
H5_CODE = {
    "store/projects/demo/run.sh": "#!/bin/sh\npython3 \"" + script_path("x-h5-sh.py") + "\"\n"
                                  "python3 \"" + script_path("x-h5-chain-a.py") + "\"\n",
    "store/templates/tpl/gen.py": runs("x-h5-py.py"),
    "store/workspaces/ws/task.ts": "const cmd = \"" + CALL + "x-h5-ts.py\";\n",
    "store/machines/m1/hook.js": "const cmd = \"" + CALL + "x-h5-js.py\";\n",
    "store/projects/demo/lib.mjs": "export const cmd = \"" + CALL + "x-h5-mjs.py\";\n",
}
H5_DATA = {
    "store/projects/demo/notes.md": "Run x-h5-data-md.py by hand.\n",
    "store/projects/demo/config.json": json.dumps({"run": CALL + "x-h5-data-json.py"}) + "\n",
    "store/machines/m1/settings.toml": "run = \"" + CALL + "x-h5-data-toml.py\"\n",
}


IMPORT_DRIVER = (
    "import os, x_comma\n"
    "import sys, json, x_third\n"
    "from x_from import helper\n"
    "import importlib\n"
    "importlib.import_module(\"x_lit_dq\")\n"
    "importlib.import_module('x_lit_sq')\n"
    "# import x_commented\n")


H7_LINES = {
    "concat": "path = d + \"/\" + name + \".py\"\n",
    "join-concat": "path = os.path.join(base, name + \".py\")\n",
    "join-fstring": "path = os.path.join(base, f\"{name}.py\")\n",
    "path-divide": "path = Path(base) / (name + \".py\")\n",
}


try:
    print("fixture sanity")
    check("tool exists: global/scripts/store-reader-graph.py", os.path.isfile(TOOL), TOOL)
    check("fixture: 2.5 MB script is over the 2 MB cap", len(TOOL_BIG) > 2_500_000)
    check("fixture: latin-1 and 0xff scripts are not valid UTF-8 and hold no NUL",
          all(b"\0" not in blob and not is_utf8(blob) for blob in (HOOK_LATIN, TOOL_LATIN)))

    print("B1: loaders in test files")
    b1_root, b1 = case("b1", B1_FILES)
    for name, (content, needle) in {**TEST_LOADERS, **NON_TEST_LOADERS}.items():
        expect_test = name in TEST_LOADERS
        line = line_of(content, needle)
        found = loaders_in(b1, name, line)
        check("loader_lines still lists %s:%d" % (name, line), len(found) == 1,
              str([ln.get("file") for ln in loader_lines(b1)])[:200])
        check("%s loader has in_test_file %s" % (name, expect_test),
              bool(found) and found[0].get("in_test_file") is expect_test,
              str(found)[:200])
    for cls, key, reach, verdict in (
            ("hooks", "global/hooks/hook-glob-only.py", "unreachable", "no-reader-found"),
            ("scripts", "global/scripts/dyn-by-hook-test.sh", "unreachable", "no-reader-found"),
            ("docs", "shared-docs/doc-only-test-loaded.md", "unreachable", "no-reader-found"),
            ("commands", "global/commands/cmd-only-test-loaded.md", "unreachable",
             "no-reader-found")):
        ok, detail = reach_is(b1, cls, key, reach)
        check("%s matched only by a test-file loader is %s" % (os.path.basename(key), reach),
              ok, detail)
        ok, detail = verdict_is(b1, cls, key, verdict)
        check("%s matched only by a test-file loader has verdict %s"
              % (os.path.basename(key), verdict), ok, detail)
    ok, detail = reach_is(b1, "hooks", "global/hooks/hook-test-read.py", "test-only")
    check("hook-test-read.py (only test readers, glob in a test file) stays test-only",
          ok, detail)
    ok, detail = reach_is(b1, "hooks", "global/hooks/hook-rooted.py", "root-reachable")
    check("hook-rooted.py (root-reachable, matched by a test loader) stays root-reachable",
          ok, detail)
    for cls, key, label in (
            ("hooks", "global/hooks/hook-mixed.sh", "matched by a test and a non-test loader"),
            ("scripts", "global/scripts/dyn-control.ts", "matched by a non-test f-string loader"),
            ("agents", "global/agents/agent-latest.md",
             "matched by latest-loader.py (name only contains test)")):
        ok, detail = reach_is(b1, cls, key, "uncertain")
        check("%s %s: reach uncertain" % (os.path.basename(key), label), ok, detail)
        ok, detail = verdict_is(b1, cls, key, "uncertain")
        check("%s %s: verdict uncertain" % (os.path.basename(key), label), ok, detail)

    print("B2: test_loader_effect")
    effect_raw: Any = b1.get("test_loader_effect") if isinstance(b1, dict) else None
    check("test_loader_effect is an object with keys_changed and keys",
          isinstance(effect_raw, dict) and isinstance(effect_raw.get("keys_changed"), int)
          and isinstance(effect_raw.get("keys"), dict), str(effect_raw)[:200])
    effect: dict[str, Any] = effect_raw if isinstance(effect_raw, dict) else {}
    keys_raw: Any = effect.get("keys")
    effect_keys: dict[str, Any] = keys_raw if isinstance(keys_raw, dict) else {}
    hook_loaders = [("test-hook-list.py", line_of(LISTING_LOADER, "os.listdir"), "os.listdir"),
                    ("test_glob_hooks.py", line_of(TEST_LOADERS["test_glob_hooks.py"][0],
                                                   "glob.glob"), "glob.glob")]
    expected_effect = {
        "global/hooks/hook-glob-only.py": hook_loaders,
        "global/hooks/hook-test-read.py": hook_loaders,
        "global/scripts/dyn-by-hook-test.sh": [
            ("hook-test-run.py", line_of(TEST_LOADERS["hook-test-run.py"][0], "{name}.sh"),
             "{name}.sh")],
        "shared-docs/doc-only-test-loaded.md": [
            ("test_docs_glob.py", line_of(TEST_LOADERS["test_docs_glob.py"][0], "glob.glob"),
             "glob.glob")],
        "global/commands/cmd-only-test-loaded.md": [
            ("hook-test-cases.py", line_of(TEST_LOADERS["hook-test-cases.py"][0], "glob.glob"),
             "glob.glob")],
    }
    check("keys_changed counts the five keys that stop being uncertain",
          effect.get("keys_changed") == 5, "keys_changed %r" % effect.get("keys_changed"))
    check("test_loader_effect.keys holds exactly those five keys",
          set(effect_keys) == set(expected_effect), str(sorted(effect_keys))[:300])
    for key, wanted in expected_effect.items():
        got = effect_keys.get(key)
        got = got if isinstance(got, list) else []
        got_pairs = sorted((os.path.basename(str(g.get("file"))), g.get("line")) for g in got
                           if isinstance(g, dict))
        want_pairs = sorted((f, ln) for f, ln, _n in wanted)
        check("effect for %s lists its test-file loader lines" % os.path.basename(key),
              got_pairs == want_pairs, "%s, want %s" % (got_pairs, want_pairs))
        check("effect for %s has file, line and snippet on every entry"
              % os.path.basename(key),
              bool(got) and all(isinstance(g.get("file"), str) and isinstance(g.get("line"), int)
                                and isinstance(g.get("snippet"), str) and g["snippet"]
                                for g in got if isinstance(g, dict))
              and all(any(n in str(g.get("snippet")) for n in {n for _f, _l, n in wanted})
                      for g in got if isinstance(g, dict)), str(got)[:200])
    for key in ("global/hooks/hook-mixed.sh", "global/scripts/dyn-control.ts",
                "global/agents/agent-latest.md", "global/hooks/hook-rooted.py"):
        check("%s never appears in test_loader_effect (its reach did not change)"
              % os.path.basename(key), bool(effect) and key not in effect_keys,
              "effect keys %s" % sorted(effect_keys))
    rc_text, text, _err = run_tool(["--store", os.path.join(b1_root, "store"),
                                    "--home", os.path.join(b1_root, "home")], {"HOME": b1_root})
    summary = [ln for ln in text.splitlines() if ln.startswith("test-file loaders:")]
    check("text mode prints one 'test-file loaders: 5 key(s) no longer uncertain' line",
          rc_text == 0 and summary == ["test-file loaders: 5 key(s) no longer uncertain"],
          str(summary))

    print("H9: scan_blind_spots and reach_caveat pointer")
    spots = b1.get("scan_blind_spots") if isinstance(b1, dict) else None
    check("scan_blind_spots is a list of strings",
          isinstance(spots, list) and all(isinstance(s, str) for s in spots), str(spots)[:200])
    for spot in BLIND_SPOTS:
        check("scan_blind_spots contains %r" % spot,
              isinstance(spots, list) and spot in spots, str(spots)[:200])
    rc, hooks_only, _err = run_json(b1_root, ["--class", "hooks"])
    check("scan_blind_spots is present with --class hooks",
          isinstance(hooks_only, dict) and isinstance(hooks_only.get("scan_blind_spots"), list))
    removable = [(cls, key, info) for cls, section in (b1 or {}).get("classes", {}).items()
                 for key, info in section.get("keys", {}).items()
                 if info.get("reach") in ("test-only", "unreachable")]
    check("fixture has unreachable and test-only keys to check", len(removable) >= 4,
          str(len(removable)))
    check("every unreachable/test-only key's reach_caveat ends with %r" % POINTER,
          bool(removable) and all(str(i.get("reach_caveat", "")).endswith(POINTER)
                                  for _c, _k, i in removable),
          str([k for _c, k, i in removable if not str(i.get("reach_caveat", "")).endswith(POINTER)][:3]))
    check("every unreachable/test-only key's reach_caveat still names every unchecked root",
          bool(removable) and all(
              all(r in str(i.get("reach_caveat", "")) for r in
                  ("m4", "rp", "pc", "mirror-a", "mirror-b", LAPTOP_ROOT)) for _c, _k, i in removable))
    check("every unreachable/test-only key keeps a non-empty caveat naming the roots",
          bool(removable) and all(
              all(r in str(i.get("caveat", "")) for r in ("m4", "mirror-b", LAPTOP_ROOT))
              for _c, _k, i in removable))
    rc, done, _err = run_json(b1_root, ALL_CHECKED)
    done_removable = [i for section in (done or {}).get("classes", {}).values()
                      for i in section.get("keys", {}).values()
                      if i.get("reach") in ("test-only", "unreachable")]
    check("with all machines checked reach_caveat still ends with the pointer",
          bool(done_removable) and all(str(i.get("reach_caveat", "")).endswith(POINTER)
                                       and LAPTOP_ROOT in str(i.get("reach_caveat", ""))
                                       for i in done_removable))

    print("H1: --out safety")
    h1_files: dict[str, str | bytes] = {
        G + "hooks/hook-h1.py": "print('h1')\n",
        "store/shared-docs/existing-target.md": "# Precious\nbytes stay\n",
    }
    h1 = build("h1", h1_files)
    h1_store = os.path.join(h1, "store")
    h1_home = os.path.join(h1, "home")
    os.symlink(os.path.join(h1_store, "global"), os.path.join(h1, "outdir", "linkdir"))
    os.symlink(os.path.join(h1_store, "shared-docs", "existing-target.md"),
               os.path.join(h1, "outdir", "sym.json"))
    inside_cases = (
        ("new file in the store", os.path.join(h1_store, "global", "new-report.json")),
        ("existing store file", os.path.join(h1_store, "shared-docs", "existing-target.md")),
        ("'..' path into the store",
         os.path.join(h1, "outdir", "..", "store", "global", "dotdot-report.json")),
        ("symlinked directory into the store",
         os.path.join(h1, "outdir", "linkdir", "link-report.json")),
        ("symlink to a store file", os.path.join(h1, "outdir", "sym.json")),
    )
    for label, out_path in inside_cases:
        before = snapshot(h1_store, h1_home)
        rc, _out, err = run_tool(["--store", h1_store, "--home", h1_home, "--json",
                                  "--out", out_path], {"HOME": h1})
        after = snapshot(h1_store, h1_home)
        check("--out %s: refused with nonzero exit" % label, rc != 0, "rc=%s" % rc)
        check("--out %s: message on stderr, no traceback" % label,
              bool(err.strip()) and "Traceback" not in err, err[:200])
        check("--out %s: store and home byte-identical, no file created" % label,
              before == after,
              "changed: %s" % sorted(k for k in set(before) | set(after)
                                     if before.get(k) != after.get(k))[:3])
    missing = os.path.join(h1, "outdir", "no-such-dir", "report.json")
    rc, _out, err = run_tool(["--store", h1_store, "--home", h1_home, "--json",
                              "--out", missing], {"HOME": h1})
    check("--out with a missing parent directory exits 1", rc == 1, "rc=%s" % rc)
    check("--out with a missing parent directory prints no traceback",
          "Traceback" not in err, err[-200:])
    check("--out with a missing parent directory prints a one-line error on stderr",
          len(err.strip().splitlines()) == 1, "stderr lines: %d" % len(err.strip().splitlines()))
    check("--out with a missing parent directory creates nothing",
          not os.path.exists(os.path.dirname(missing)))

    print("H2: '*)' and '*' continuation lines are code")
    h2_files: dict[str, str | bytes] = {
        G + "tools/case-dispatch.sh": CASE_SH,
        G + "tools/py-star.py": STAR_PY,
        G + "tools/docblock.js": DOCBLOCK_JS,
        G + "scripts/x-case-target.py": "print('case')\n",
        G + "scripts/x-star-target.py": "print('star')\n",
        G + "scripts/x-hash-comment.py": "print('hash')\n",
        G + "scripts/x-doc-target.py": "print('doc')\n",
        "home/.claude/settings.json": "{}\n",
    }
    _h2_root, h2 = case("h2", h2_files)
    case_line = line_of(CASE_SH, "*) python3")
    ok, detail = exec_from(h2, "scripts", "global/scripts/x-case-target.py",
                           "case-dispatch.sh", "script", case_line)
    check("shell case branch '*) python3 ...' gives x-case-target.py an exec reader", ok, detail)
    ok, detail = reach_is(h2, "scripts", "global/scripts/x-case-target.py", "root-reachable")
    check("x-case-target.py (named on a '*)' line of unkeyed code) is root-reachable", ok, detail)
    star_line = line_of(STAR_PY, "*[")
    ok, detail = exec_from(h2, "scripts", "global/scripts/x-star-target.py",
                           "py-star.py", "script", star_line)
    check("Python continuation line starting with '*' gives x-star-target.py an exec reader",
          ok, detail)
    ok, detail = reach_is(h2, "scripts", "global/scripts/x-star-target.py", "root-reachable")
    check("x-star-target.py is root-reachable", ok, detail)
    check("control: a '#' comment line in shell gives no exec reader",
          not exec_readers(h2, "scripts", "global/scripts/x-hash-comment.py"),
          str(exec_readers(h2, "scripts", "global/scripts/x-hash-comment.py")))
    check("control: a ' * ' doc-block comment line gives no exec reader",
          not exec_readers(h2, "scripts", "global/scripts/x-doc-target.py"),
          str(exec_readers(h2, "scripts", "global/scripts/x-doc-target.py")))
    ok, detail = reach_is(h2, "scripts", "global/scripts/x-doc-target.py", "unreachable")
    check("control: x-doc-target.py named only in a doc-block comment is unreachable",
          ok, detail)

    print("H3: non-UTF-8 and large files are scanned")
    h3_files: dict[str, str | bytes] = {
        G + "hooks/hook-latin.py": HOOK_LATIN,
        G + "tools/latin.sh": TOOL_LATIN,
        G + "tools/big.sh": TOOL_BIG,
        G + "scripts/x-hook-latin-target.py": "print('a')\n",
        G + "scripts/x-latin-target.py": "print('b')\n",
        G + "scripts/x-big-target.py": "print('c')\n",
        G + "scripts/binary.py": b"\x00\xff\xfe\x80" * 64,
        "home/.claude/settings.json": settings_json(("hook-latin.py",)),
    }
    _h3_root, h3 = case("h3", h3_files)
    for key, reader, label in (
            ("x-hook-latin-target.py", "hook-latin.py", "a wired hook with a latin-1 byte"),
            ("x-latin-target.py", "latin.sh", "an unkeyed script with 0xff bytes"),
            ("x-big-target.py", "big.sh", "a 2.5 MB script, callee on its last line")):
        ok, detail = exec_from(h3, "scripts", "global/scripts/" + key, reader, "script")
        check("%s has an exec reader in %s (%s)" % (key, reader, label), ok, detail)
        ok, detail = reach_is(h3, "scripts", "global/scripts/" + key, "root-reachable")
        check("%s is root-reachable, not unreachable" % key, ok, detail)
    listed = [u for u in unreadable_files(h3)
              if any(str(u.get("file", "")).endswith(n)
                     for n in ("hook-latin.py", "latin.sh", "big.sh"))]
    check("no non-UTF-8 or large file is listed in unreadable", not listed, str(listed))
    check("no unreadable entry has a non-utf8 or too large reason",
          not any("utf" in str(u.get("reason", "")).lower()
                  or "too large" in str(u.get("reason", "")).lower()
                  for u in unreadable_files(h3)), str(unreadable_files(h3))[:200])
    check("control: a file with NUL bytes is still listed as binary",
          any(str(u.get("file", "")).endswith("binary.py") and "binary" in str(u.get("reason"))
              for u in unreadable_files(h3)), str(unreadable_files(h3))[:200])

    print("H4: shell blocks in skills, agents and commands")
    h4_files: dict[str, str | bytes] = {
        G + "skills/skill-runner/SKILL.md": SKILL_MD,
        G + "agents/agent-runner.md": AGENT_MD,
        G + "commands/cmd-runner.md": COMMAND_MD,
        "home/.claude/settings.json": "{}\n",
    }
    for target in H4_TARGETS:
        h4_files[G + "scripts/" + target] = "print('t')\n"
    _h4_root, h4 = case("h4", h4_files)
    for target, reader, content, needle, via in (
            ("x-skill-fence.py", "SKILL.md", SKILL_MD, "x-skill-fence.py", "root:skill-shell"),
            ("x-skill-bare.py", "SKILL.md", SKILL_MD, "x-skill-bare.py", "root:skill-shell"),
            ("x-skill-bang.py", "SKILL.md", SKILL_MD, "x-skill-bang.py", "root:skill-shell"),
            ("x-agent-fence.py", "agent-runner.md", AGENT_MD, "x-agent-fence.py",
             "root:agent-shell"),
            ("x-agent-bang.py", "agent-runner.md", AGENT_MD, "x-agent-bang.py",
             "root:agent-shell"),
            ("x-cmd-bang.py", "cmd-runner.md", COMMAND_MD, "x-cmd-bang.py",
             "root:command-shell"),
            ("x-cmd-bare.py", "cmd-runner.md", COMMAND_MD, "x-cmd-bare.py",
             "root:command-shell"),
            ("x-cmd-bash.py", "cmd-runner.md", COMMAND_MD, "x-cmd-bash.py",
             "root:command-shell")):
        ok, detail = exec_from(h4, "scripts", "global/scripts/" + target, reader, via,
                               line_of(content, needle))
        check("%s: exec reader in %s with via %s" % (target, reader, via), ok, detail)
        ok, detail = reach_is(h4, "scripts", "global/scripts/" + target, "root-reachable")
        check("%s is root-reachable" % target, ok, detail)
    for target in ("x-skill-prose.py", "x-agent-prose.py", "x-cmd-json-mention.py"):
        info = entry(h4, "scripts", "global/scripts/" + target)
        check("%s stays a mention (no exec reader, verdict mention-only)" % target,
              bool(info) and info.get("exec_count") == 0
              and info.get("verdict") == "mention-only", str(info)[:200])
        ok, detail = reach_is(h4, "scripts", "global/scripts/" + target, "unreachable")
        check("%s (mention only) is unreachable" % target, ok, detail)

    print("H5: code under projects, templates, workspaces, machines")
    h5_files: dict[str, str | bytes] = {**H5_CODE, **H5_DATA,
                                        "home/.claude/settings.json": "{}\n"}
    for target in ("x-h5-sh", "x-h5-py", "x-h5-ts", "x-h5-js", "x-h5-mjs", "x-h5-data-md",
                   "x-h5-data-json", "x-h5-data-toml", "x-h5-chain-b"):
        h5_files[G + "scripts/%s.py" % target] = "print('t')\n"
    h5_files[G + "scripts/x-h5-chain-a.py"] = runs("x-h5-chain-b.py")
    _h5_root, h5 = case("h5", h5_files)
    for target, reader in (("x-h5-sh", "run.sh"), ("x-h5-py", "gen.py"), ("x-h5-ts", "task.ts"),
                           ("x-h5-js", "hook.js"), ("x-h5-mjs", "lib.mjs")):
        key = "global/scripts/%s.py" % target
        ok, detail = exec_from(h5, "scripts", key, reader, "script")
        check("%s.py has an exec reader in %s with via script" % (target, reader), ok, detail)
        ok, detail = reach_is(h5, "scripts", key, "root-reachable")
        check("%s.py is root-reachable (its reader is a root)" % target, ok, detail)
    ok, detail = reach_is(h5, "scripts", "global/scripts/x-h5-chain-b.py", "root-reachable")
    check("x-h5-chain-b.py behind a script run from projects/ is root-reachable", ok, detail)
    for target, reader in (("x-h5-data-md", "notes.md"), ("x-h5-data-json", "config.json"),
                           ("x-h5-data-toml", "settings.toml")):
        key = "global/scripts/%s.py" % target
        info = entry(h5, "scripts", key)
        check("%s.py named only in data file %s has no exec reader" % (target, reader),
              bool(info) and info.get("exec_count") == 0, str(info)[:200])
        ok, detail = reach_is(h5, "scripts", key, "unreachable")
        check("%s.py named only in data file %s is unreachable" % (target, reader), ok, detail)

    print("H6: Python imports")
    h6_files: dict[str, str | bytes] = {
        G + "scripts/import-driver.py": IMPORT_DRIVER,
        "home/.claude/settings.json": settings_json(script_names=("import-driver.py",)),
    }
    for module in ("x_comma", "x_third", "x_from", "x_lit_dq", "x_lit_sq", "x_commented"):
        h6_files[G + "scripts/%s.py" % module] = "print('m')\n"
    _h6_root, h6 = case("h6", h6_files)
    for module, needle in (("x_comma", "import os, x_comma"), ("x_third", "x_third"),
                           ("x_from", "from x_from"), ("x_lit_dq", "\"x_lit_dq\""),
                           ("x_lit_sq", "'x_lit_sq'")):
        key = "global/scripts/%s.py" % module
        ok, detail = exec_from(h6, "scripts", key, "import-driver.py", "script",
                               line_of(IMPORT_DRIVER, needle))
        check("%s.py has an exec reader in import-driver.py at its import line" % module,
              ok, detail)
        ok, detail = reach_is(h6, "scripts", key, "root-reachable")
        check("%s.py is root-reachable through the wired importer" % module, ok, detail)
    check("control: 'import x_commented' in a comment gives no exec reader",
          not exec_readers(h6, "scripts", "global/scripts/x_commented.py"),
          str(exec_readers(h6, "scripts", "global/scripts/x_commented.py")))
    check("literal import_module(\"x\") is not a loader line",
          not loaders_in(h6, "import-driver.py"), str(loaders_in(h6, "import-driver.py")))
    dyn_import = "import importlib\nname = \"n\"\nmod = importlib.import_module(name)\n"
    h6d_files: dict[str, str | bytes] = {
        G + "scripts/import-dynamic.py": dyn_import,
        G + "scripts/x-dyn-victim.py": "print('victim')\n",
        "home/.claude/settings.json": settings_json(script_names=("import-dynamic.py",)),
    }
    _h6d_root, h6d = case("h6-dynamic", h6d_files)
    dyn_line = line_of(dyn_import, "import_module(name)")
    found = loaders_in(h6d, "import-dynamic.py", dyn_line)
    check("dynamic import_module(name) is a loader_lines entry", len(found) == 1
          and "import_module(name)" in str(found[0].get("snippet")), str(loader_lines(h6d))[:300])
    ok, detail = reach_is(h6d, "scripts", "global/scripts/x-dyn-victim.py", "uncertain")
    check("x-dyn-victim.py (same directory, no other reader) is uncertain", ok, detail)
    ok, detail = verdict_is(h6d, "scripts", "global/scripts/x-dyn-victim.py", "uncertain")
    check("x-dyn-victim.py verdict is uncertain, not no-reader-found", ok, detail)

    print("H7: dynamic names without a directory name on the line")
    for label, line_text in H7_LINES.items():
        content = "import os\n" + line_text
        h7_files: dict[str, str | bytes] = {
            G + "scripts/h7-loader.py": content,
            G + "scripts/x-h7-victim.py": "print('victim')\n",
            "home/.claude/settings.json": settings_json(script_names=("h7-loader.py",)),
        }
        _h7_root, h7 = case("h7-" + label, h7_files)
        line = line_of(content, "name")
        found = loaders_in(h7, "h7-loader.py", line)
        check("h7 %s: loader_lines entry for %r" % (label, line_text.strip()),
              len(found) == 1 and isinstance(found[0].get("matched_keys"), int),
              str(loader_lines(h7))[:300])
        ok, detail = reach_is(h7, "scripts", "global/scripts/x-h7-victim.py", "uncertain")
        check("h7 %s: x-h7-victim.py is uncertain" % label, ok, detail)
        ok, detail = verdict_is(h7, "scripts", "global/scripts/x-h7-victim.py", "uncertain")
        check("h7 %s: x-h7-victim.py verdict is uncertain" % label, ok, detail)
    static = ("import os\nfrom pathlib import Path\n"
              "path = os.path.join(base, \"fixed.py\")\n"
              "other = Path(base) / \"fixed.py\"\n")
    h7s_files: dict[str, str | bytes] = {
        G + "scripts/h7-static.py": static,
        G + "scripts/x-h7-victim.py": "print('victim')\n",
        "home/.claude/settings.json": settings_json(script_names=("h7-static.py",)),
    }
    _h7s_root, h7s = case("h7-static", h7s_files)
    check("control: static joins with a literal file name are not loader lines",
          not loaders_in(h7s, "h7-static.py"), str(loader_lines(h7s))[:300])
    ok, detail = reach_is(h7s, "scripts", "global/scripts/x-h7-victim.py", "unreachable")
    check("control: x-h7-victim.py next to static joins stays unreachable", ok, detail)

    print("H8: only hooks-manifest.json and mcp-servers.json are manifest roots")
    h8_files: dict[str, str | bytes] = {
        G + "hooks/hook-in-manifest.py": "print('m')\n",
        G + "hooks/hook-fp-only.py": "print('fp')\n",
        G + "hooks/hook-other-json.py": "print('o')\n",
        G + "scripts/script-mcp.py": "print('mcp')\n",
        G + "hooks-manifest.json": json.dumps({"hooks": {"hook-in-manifest.py": {"sha256": "a"}}},
                                              indent=2) + "\n",
        G + "hook-fingerprints.json": json.dumps({"hooks": {"hook-fp-only.py": {"sha256": "a"}}},
                                                 indent=2) + "\n",
        G + "other-data.json": json.dumps({"hooks": {"hook-other-json.py": {"sha256": "a"}}},
                                          indent=2) + "\n",
        G + "mcp-servers.json": json.dumps({"servers": {"demo": {
            "args": ["/home/x/.agent-context/global/scripts/script-mcp.py"]}}}, indent=2) + "\n",
        "home/.claude/settings.json": "{}\n",
    }
    _h8_root, h8 = case("h8", h8_files)
    ok, detail = exec_from(h8, "hooks", "global/hooks/hook-in-manifest.py",
                           "hooks-manifest.json", "root:manifest")
    check("control: hooks-manifest.json is root:manifest", ok, detail)
    ok, detail = exec_from(h8, "scripts", "global/scripts/script-mcp.py",
                           "mcp-servers.json", "root:manifest")
    check("control: mcp-servers.json is root:manifest", ok, detail)
    for key, reader in (("global/hooks/hook-fp-only.py", "hook-fingerprints.json"),
                        ("global/hooks/hook-other-json.py", "other-data.json")):
        ok, detail = reach_is(h8, "hooks", key, "unreachable")
        check("%s named only in %s is unreachable" % (os.path.basename(key), reader),
              ok, detail)
        check("%s: %s is not a root:manifest reader" % (os.path.basename(key), reader),
              not any(r.get("via") == "root:manifest"
                      for r in exec_readers(h8, "hooks", key, reader)),
              str(exec_readers(h8, "hooks", key, reader)))
    h8_effect: Any = h8.get("test_loader_effect") if isinstance(h8, dict) else None
    check("test_loader_effect is {keys_changed: 0, keys: {}} when no test loader exists",
          h8_effect == {"keys_changed": 0, "keys": {}}, str(h8_effect)[:200])

    print("H10: report-only")
    written_path = os.path.join(b1_root, "outdir", "report.json")
    rc_out, _stdout, _err = run_tool(
        ["--store", os.path.join(b1_root, "store"), "--home", os.path.join(b1_root, "home"),
         "--json", "--out", written_path], {"HOME": b1_root})
    written = None
    if os.path.isfile(written_path):
        with open(written_path, "r", encoding="utf-8") as handle:
            try:
                written = json.load(handle)
            except ValueError:
                written = None
    check("valid --out exits 0 and writes a report with every new field",
          rc_out == 0 and isinstance(written, dict)
          and all(k in written for k in ("test_loader_effect", "scan_blind_spots"))
          and all("in_test_file" in ln for ln in loader_lines(written)) and bool(loader_lines(written)),
          "rc=%s" % rc_out)
    for name in ("b1", "h2", "h3", "h4", "h5", "h6", "h8"):
        root = roots[name]
        base_snapshot = snapshot(os.path.join(root, "store"), os.path.join(root, "home"))
        run_tool(["--store", os.path.join(root, "store"), "--home", os.path.join(root, "home"),
                  "--json", "--out", os.path.join(root, "outdir", "report.json")], {"HOME": root})
        run_tool(["--store", os.path.join(root, "store"), "--home", os.path.join(root, "home")],
                 {"HOME": root})
        after_snapshot = snapshot(os.path.join(root, "store"), os.path.join(root, "home"))
        check("%s: store and home byte-identical after runs incl. --out" % name,
              base_snapshot == after_snapshot,
              "changed: %s" % sorted(k for k in set(base_snapshot) | set(after_snapshot)
                                     if base_snapshot.get(k) != after_snapshot.get(k))[:3])
    write_pattern = re.compile(
        r"os\.remove|unlink|rmtree|shutil\.(move|copy\w*)|os\.rename|os\.replace|write_text|"
        r"write_bytes|os\.(makedirs|mkdir|chmod|chown|symlink|link|truncate|utime)|\.touch\(|"
        r"O_WRONLY|O_RDWR|O_CREAT|O_APPEND|open\([^)]*[\"'][^\"']*[wax+][^\"']*[\"']|"
        r"subprocess|os\.system|os\.popen")
    with open(TOOL, "r", encoding="utf-8") as handle:
        source_lines = handle.read().splitlines()
    offenders = [(i, ln.strip()) for i, ln in enumerate(source_lines, 1)
                 if write_pattern.search(ln) and not ln.lstrip().startswith("#")
                 and not re.search(r"\bout", ln, re.I)]
    check("source writes, renames, deletes or spawns nothing outside the --out writer",
          not offenders, str(offenders[:5]))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

total = passed + len(failures)
print("test-store-reader-graph-hardening: %d/%d passed" % (passed, total))
sys.exit(1 if failures else 0)
