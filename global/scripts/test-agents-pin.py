#!/usr/bin/env python3
"Battery for agents-pin.py, the agent projector for opencode and pi.\n\nThe two harnesses keep different policies on purpose, and the fixtures pin each one:\n\n  - an alias no authenticated provider carries: opencode writes no model line, pi\n    binds the first carrier by name;\n  - an agent whose tools all drop: opencode skips it, pi copies it with tools as is;\n  - a same-named file the projector did not write: opencode leaves it, pi replaces it;\n  - MCP names: opencode keeps the server's hyphens, pi turns them into underscores;\n  - opencode renders the frontmatter anew, pi edits the model and tools lines in place.\n\nEach scenario builds a fake HOME under TMPDIR and runs the script with HOME pointed at\nit; every path the projector reads derives from HOME. A run's result is the set of\nfiles it changed or removed anywhere under the scenario root, so a stray temp file is\na failure too. Paths in output are compared with the root spelled <ROOT>. TEXTS holds\neach distinct file body once, keyed by a digest; GOLDENS refers to them by key.\n\nRuns on Python 3.8, the system Python on the Synology nodes.\n\nUsage: test-agents-pin.py"

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PIN = os.path.join(HERE, "agents-pin.py")
OLD_NAMES = ("opencode-agents-pin.py", "pi-agents-pin.py")
CALLERS = ("harness-materialize.py", "home-materialize.py", "agents-materialize.py",
           "invariant-check.py")
OPENCODE_MANIFEST = ".agent-context-agents.json"
PI_MANIFEST = ".pi-agents-pin.manifest"

EM_DASH = chr(0x2014)

AGENTS = {
    "alpha.md": (
        "---\n"
        "name: alpha\n"
        'model: "sonnet"\n'
        'tools: "Read, Grep, Glob, LS, Bash, Edit, Write, MultiEdit, NotebookEdit, Skill, '
        "WebFetch, WebSearch, ToolSearch, Task, Read, mcp__csharp-lsp__definition, "
        'mcp__agent-context__get_doc"\n'
        'description: Finds "things": fast ' + EM_DASH + " always.\n"
        "---\n"
        "\n"
        "Alpha body.\n"
    ),
    "beta.md": (
        "---\n"
        "name: beta\n"
        "description: Only tools with no equivalent.\n"
        "model: 'opus'\n"
        "tools: ToolSearch, Task\n"
        "---\n"
        "Beta body.\n"
    ),
    "gamma.md": (
        "---\n"
        "name: gamma\n"
        "description: Tier carried only by an unauthenticated provider.\n"
        "model: haiku\n"
        "tools: read, grep\n"
        "---\n"
        "Gamma body.\n"
    ),
    "delta.md": (
        "---\n"
        "name: delta\n"
        "description: Tier no catalog carries.\n"
        "model: fable\n"
        "tools: Bash\n"
        "---\n"
        "Delta body.\n"
    ),
    "epsilon.md": (
        "---\n"
        "metadata:\n"
        "  model: opus\n"
        "name: epsilon\n"
        "description: A versioned model id after a nested key.\n"
        "model: claude-sonnet-4-5\n"
        "tools: Read\n"
        "---\n"
        "Epsilon body.\n"
    ),
    "zeta.md": (
        "---\n"
        "name: zeta\n"
        "description: Uppercase alias.\n"
        "model: OPUS\n"
        "tools: Glob, LS\n"
        "---\n"
        "Zeta body.\n"
    ),
    "noname.md": (
        "---\n"
        "description: No name.\n"
        "tools: Read\n"
        "---\n"
        "Noname body.\n"
    ),
    "plain.md": "Just text, no frontmatter.\n",
    "notes.txt": "Not an agent.\n",
}

OPENCODE_AUTH = {"github-copilot": {"type": "oauth"}, "anthropic": {"type": "api"},
                 "broken": {"type": "api"}}
OPENCODE_CATALOG = {
    "github-copilot": {"models": {"claude-sonnet-4": {}, "claude-sonnet-4.6": {},
                                  "claude-sonnet-5": {}, "claude-opus-4.1": {}}},
    "anthropic": {"models": {"claude-sonnet-5": {}, "claude-opus-5": {}}},
    "amazon-bedrock": {"models": {"claude-haiku-4-5": {}, "claude-opus-6": {}}},
    "broken": "not a provider",
}
PI_AUTH = {"github-copilot": {"type": "oauth"}, "anthropic": {"type": "api"}}
PI_SETTINGS = {"defaultProvider": "anthropic"}
PI_CATALOG = {
    "github-copilot": {"models": [{"id": "claude-sonnet-4"}, {"id": "claude-sonnet-5"},
                                  {"id": "claude-opus-4.1"}]},
    "anthropic": {"models": [{"id": "claude-sonnet-5"}, {"id": "claude-opus-5"}, "junk"]},
    "amazon-bedrock": {"models": [{"id": "claude-haiku-4-5"}, {"id": "claude-opus-6"}]},
    "empty": None,
}

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def put_json(path, data):
    put(path, json.dumps(data))


def make_src(root, agents=None):
    src = os.path.join(root, "store-agents")
    os.makedirs(src)
    for name, text in (AGENTS if agents is None else agents).items():
        put(os.path.join(src, name), text)
    return src


def bare_home(root):
    home = os.path.join(root, "home")
    os.makedirs(home, exist_ok=True)
    return home


def opencode_home(root, catalog=True):
    home = bare_home(root)
    os.makedirs(os.path.join(home, ".config", "opencode"))
    if catalog:
        put_json(os.path.join(home, ".local", "share", "opencode", "auth.json"), OPENCODE_AUTH)
        put_json(os.path.join(home, ".cache", "opencode", "models.json"), OPENCODE_CATALOG)
    return home


def pi_home(root, catalog=True):
    home = bare_home(root)
    agent = os.path.join(home, ".pi", "agent")
    os.makedirs(agent)
    if catalog:
        put_json(os.path.join(agent, "auth.json"), PI_AUTH)
        put_json(os.path.join(agent, "settings.json"), PI_SETTINGS)
        put_json(os.path.join(agent, "models-store.json"), PI_CATALOG)
    return home


def tree(root):
    '{path relative to root: text} for every file under root.'
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = fh.read().decode("utf-8", "replace")
    return out


def run(argv, home):
    return subprocess.run(argv, env=dict(os.environ, HOME=home), capture_output=True,
                          text=True, timeout=60)


def execute(root, home, argv):
    'One projector run: exit code, output, and what it changed under root.'
    before = tree(root)
    r = run(argv, home)
    after = tree(root)
    return {
        "rc": r.returncode,
        "stdout": r.stdout.replace(root, "<ROOT>"),
        "stderr": r.stderr.replace(root, "<ROOT>"),
        "changed": {p: t for p, t in sorted(after.items()) if before.get(p) != t},
        "removed": sorted(set(before) - set(after)),
    }




def opencode_default(root, cmd):
    home = opencode_home(root)
    src = make_src(root)
    argv = cmd("opencode") + [src]
    return [execute(root, home, argv), execute(root, home, argv)]


def opencode_explicit_dst(root, cmd):
    home = opencode_home(root)
    src = make_src(root)
    dst = os.path.join(root, "custom-agent")
    put(os.path.join(dst, "alpha.md"), "hand-written\n")
    put(os.path.join(dst, "gamma.md"), "old projection\n")
    put(os.path.join(dst, "stale.md"), "retired\n")
    put_json(os.path.join(dst, OPENCODE_MANIFEST), ["gamma.md", "stale.md", "gone.md"])
    return [execute(root, home, cmd("opencode") + [src, dst])]


def opencode_bad_manifest(root, cmd):
    home = opencode_home(root)
    src = make_src(root)
    dst = os.path.join(home, ".config", "opencode", "agent")
    put(os.path.join(dst, "gamma.md"), "old projection\n")
    put_json(os.path.join(dst, OPENCODE_MANIFEST), {"gamma.md": True})
    return [execute(root, home, cmd("opencode") + [src])]


def opencode_no_catalog(root, cmd):
    home = opencode_home(root, catalog=False)
    src = make_src(root)
    return [execute(root, home, cmd("opencode") + [src])]


def opencode_empty_src(root, cmd):
    home = opencode_home(root)
    src = make_src(root, {"notes.txt": "Not an agent.\n"})
    dst = os.path.join(home, ".config", "opencode", "agent")
    put(os.path.join(dst, "stale.md"), "retired\n")
    put_json(os.path.join(dst, OPENCODE_MANIFEST), ["stale.md"])
    return [execute(root, home, cmd("opencode") + [src])]


def opencode_not_installed(root, cmd):
    home = bare_home(root)
    src = make_src(root)
    return [execute(root, home, cmd("opencode") + [src])]


def opencode_missing_src(root, cmd):
    home = opencode_home(root)
    return [execute(root, home, cmd("opencode") + [os.path.join(root, "nowhere")])]




def pi_home_level(root, cmd):
    home = pi_home(root)
    src = make_src(root)
    argv = cmd("pi") + [src, os.path.join(home, ".pi", "agent", "agents")]
    return [execute(root, home, argv), execute(root, home, argv)]


def pi_project_level(root, cmd):
    home = pi_home(root)
    src = make_src(root)
    return [execute(root, home, cmd("pi") + [src, os.path.join(root, "repo", ".pi", "agents")])]


def pi_owned_and_foreign(root, cmd):
    home = pi_home(root)
    src = make_src(root)
    dst = os.path.join(home, ".pi", "agent", "agents")
    put(os.path.join(dst, "alpha.md"), "hand-written\n")
    put(os.path.join(dst, "hand.md"), "kept\n")
    put(os.path.join(dst, "stale.md"), "retired\n")
    put(os.path.join(dst, PI_MANIFEST), "stale.md\ngone.md\n\n")
    return [execute(root, home, cmd("pi") + [src, dst])]


def pi_no_catalog(root, cmd):
    home = pi_home(root, catalog=False)
    src = make_src(root)
    return [execute(root, home, cmd("pi") + [src, os.path.join(home, ".pi", "agent", "agents")])]


def pi_empty_src(root, cmd):
    home = pi_home(root)
    src = make_src(root, {"notes.txt": "Not an agent.\n"})
    dst = os.path.join(home, ".pi", "agent", "agents")
    put(os.path.join(dst, "stale.md"), "retired\n")
    put(os.path.join(dst, PI_MANIFEST), "stale.md\n")
    return [execute(root, home, cmd("pi") + [src, dst])]


def pi_not_installed(root, cmd):
    home = bare_home(root)
    src = make_src(root)
    return [execute(root, home, cmd("pi") + [src, os.path.join(home, ".pi", "agent", "agents")])]


def pi_missing_src(root, cmd):
    home = pi_home(root)
    dst = os.path.join(home, ".pi", "agent", "agents")
    return [execute(root, home, cmd("pi") + [os.path.join(root, "nowhere"), dst])]


SCENARIOS = (
    ("opencode-default", "opencode, default destination, then a rerun that changes nothing",
     opencode_default),
    ("opencode-explicit-dst", "opencode, explicit destination with a foreign file, an owned "
     "file, a stale entry and a missing one", opencode_explicit_dst),
    ("opencode-bad-manifest", "opencode, a manifest that is not a list owns nothing",
     opencode_bad_manifest),
    ("opencode-no-catalog", "opencode, no auth or catalog writes no model lines",
     opencode_no_catalog),
    ("opencode-empty-src", "opencode, a source with no agents retires every owned file",
     opencode_empty_src),
    ("opencode-not-installed", "opencode not installed leaves no trace", opencode_not_installed),
    ("opencode-missing-src", "opencode, a missing source leaves no trace", opencode_missing_src),
    ("pi-home", "pi, home level, then a rerun that changes nothing", pi_home_level),
    ("pi-project", "pi, project level outside HOME", pi_project_level),
    ("pi-owned-and-foreign", "pi, replaces a same-named foreign file, prunes a stale owned "
     "one, keeps a hand-written one", pi_owned_and_foreign),
    ("pi-no-catalog", "pi, no catalog keeps aliases and still translates tools", pi_no_catalog),
    ("pi-empty-src", "pi, a source with no agents prunes owned files and leaves the manifest",
     pi_empty_src),
    ("pi-not-installed", "pi not installed leaves no trace", pi_not_installed),
    ("pi-missing-src", "pi, a missing source leaves no trace", pi_missing_src),
)


def new_command(harness):
    return [sys.executable, PIN, harness]


def clip(value):
    text = repr(value)
    return text if len(text) <= 160 else text[:160] + "..."


def describe(expected, got):
    'The first difference between two runs, or "" when they match.'
    for key in ("rc", "stdout", "stderr", "removed"):
        if expected[key] != got[key]:
            return "%s: expected %s, got %s" % (key, clip(expected[key]), clip(got[key]))
    exp, act = expected["changed"], got["changed"]
    missing, extra = sorted(set(exp) - set(act)), sorted(set(act) - set(exp))
    if missing or extra:
        return "files: missing %s, unexpected %s" % (missing, extra)
    for path in sorted(exp):
        if exp[path] != act[path]:
            want, have = exp[path].split("\n"), act[path].split("\n")
            for n, (a, b) in enumerate(zip(want, have), 1):
                if a != b:
                    return "%s line %d: expected %r, got %r" % (path, n, clip(a), clip(b))
            return "%s: expected %d lines, got %d" % (path, len(want), len(have))
    return ""


def resolve_golden(run_golden):
    return dict(run_golden, changed={p: TEXTS[k] for p, k in run_golden["changed"].items()})


def check_goldens():
    print("[1] output matches the old projectors byte for byte")
    check("goldens are captured", bool(GOLDENS) and bool(TEXTS))
    for key, label, build in SCENARIOS:
        expected = GOLDENS.get(key)
        if expected is None:
            check(label, False, "no golden for " + key)
            continue
        root = tempfile.mkdtemp(prefix="agents-pin-test-")
        try:
            got = build(root, new_command)
        except Exception as exc:
            check(label, False, "raised %s: %s" % (type(exc).__name__, exc))
            continue
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if len(got) != len(expected):
            check(label, False, "expected %d runs, got %d" % (len(expected), len(got)))
            continue
        for n, (want, have) in enumerate(zip(expected, got), 1):
            detail = describe(resolve_golden(want), have)
            check("%s (run %d)" % (label, n), not detail, detail)


def check_usage():
    print("[2] a malformed command line exits 2")
    root = tempfile.mkdtemp(prefix="agents-pin-test-")
    try:
        home = opencode_home(root)
        pi_home(root)
        src = make_src(root)
        dst = os.path.join(root, "dst")
        for label, tail in (("no arguments", []),
                            ("pi without a destination", ["pi", src]),
                            ("an unknown harness", ["copilot", src, dst]),
                            ("opencode with an extra argument", ["opencode", src, dst, "extra"])):
            r = run([sys.executable, PIN] + tail, home)
            
            check(label + ": exit 2, usage on stderr, nothing written",
                  r.returncode == 2 and "usage" in r.stderr.lower() and not os.path.exists(dst),
                  "rc %d, stderr %s" % (r.returncode, clip(r.stderr)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_atomic():
    print("[3] opencode replaces a changed file by rename")
    root = tempfile.mkdtemp(prefix="agents-pin-test-")
    try:
        home = opencode_home(root)
        src = make_src(root)
        dst = os.path.join(home, ".config", "opencode", "agent")
        target = os.path.join(dst, "gamma.md")
        put(target, "old projection\n")
        put_json(os.path.join(dst, OPENCODE_MANIFEST), ["gamma.md"])
        before = os.stat(target).st_ino
        r = run(new_command("opencode") + [src], home)
        after = os.stat(target).st_ino if os.path.exists(target) else before
        check("gamma.md is a new inode after the run", r.returncode == 0 and before != after,
              "rc %d, inode %s -> %s" % (r.returncode, before, after))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_callers():
    print("[4] every caller runs agents-pin.py and the old scripts are gone")
    new_name = re.compile(r"(?<![\w-])agents-pin\.py")
    old_name = re.compile(r"(?:opencode|pi)-agents-pin\.py")
    texts = {}
    for name in CALLERS:
        try:
            with open(os.path.join(HERE, name), encoding="utf-8") as fh:
                texts[name] = fh.read()
        except OSError:
            texts[name] = ""
        hit = old_name.search(texts[name])
        check("%s names agents-pin.py and neither old script" % name,
              bool(new_name.search(texts[name])) and not hit,
              "still names " + hit.group(0) if hit else "agents-pin.py not found")
    projection = re.search(r"_PROJECTION_SCRIPTS = \(([^)]*)\)", texts["invariant-check.py"])
    check("invariant-check.py lists agents-pin.py as a projection script",
          bool(projection) and '"agents-pin.py"' in projection.group(1))
    for old in OLD_NAMES:
        check(old + " is deleted", not os.path.exists(os.path.join(HERE, old)))






TEXTS = {
    
    "04dd22ee92e9": (
        "---\n"
        'description: "Finds \\"things\\": fast ' + EM_DASH + ' always."\n'
        "mode: subagent\n"
        "model: github-copilot/claude-sonnet-5\n"
        "tools:\n"
        '  "*": false\n'
        "  read: true\n"
        "  grep: true\n"
        "  glob: true\n"
        "  list: true\n"
        "  bash: true\n"
        "  edit: true\n"
        "  write: true\n"
        "  patch: true\n"
        "  skill: true\n"
        "  webfetch: true\n"
        "  websearch: true\n"
        "  csharp-lsp_definition: true\n"
        "  agent-context_get_doc: true\n"
        "---\n"
        "Alpha body.\n"
    ),
    "7150520f9cde": (
        "---\n"
        'description: "Finds \\"things\\": fast ' + EM_DASH + ' always."\n'
        "mode: subagent\n"
        "tools:\n"
        '  "*": false\n'
        "  read: true\n"
        "  grep: true\n"
        "  glob: true\n"
        "  list: true\n"
        "  bash: true\n"
        "  edit: true\n"
        "  write: true\n"
        "  patch: true\n"
        "  skill: true\n"
        "  webfetch: true\n"
        "  websearch: true\n"
        "  csharp-lsp_definition: true\n"
        "  agent-context_get_doc: true\n"
        "---\n"
        "Alpha body.\n"
    ),
    "65efd640c2e9": (
        "---\n"
        'description: "Tier carried only by an unauthenticated provider."\n'
        "mode: subagent\n"
        "tools:\n"
        '  "*": false\n'
        "  read: true\n"
        "  grep: true\n"
        "---\n"
        "Gamma body.\n"
    ),
    "9458c9c4b789": (
        "---\n"
        'description: "Tier no catalog carries."\n'
        "mode: subagent\n"
        "tools:\n"
        '  "*": false\n'
        "  bash: true\n"
        "---\n"
        "Delta body.\n"
    ),
    "01d1fb67b2a1": (
        "---\n"
        'description: "A versioned model id after a nested key."\n'
        "mode: subagent\n"
        "tools:\n"
        '  "*": false\n'
        "  read: true\n"
        "---\n"
        "Epsilon body.\n"
    ),
    "c6ae37361f88": (
        "---\n"
        'description: "Uppercase alias."\n'
        "mode: subagent\n"
        "model: anthropic/claude-opus-5\n"
        "tools:\n"
        '  "*": false\n'
        "  glob: true\n"
        "  list: true\n"
        "---\n"
        "Zeta body.\n"
    ),
    "07d74a860155": (
        "---\n"
        'description: "Uppercase alias."\n'
        "mode: subagent\n"
        "tools:\n"
        '  "*": false\n'
        "  glob: true\n"
        "  list: true\n"
        "---\n"
        "Zeta body.\n"
    ),
    "d33b29229d39": '[\n  "alpha.md",\n  "delta.md",\n  "epsilon.md",\n  "gamma.md",\n  "zeta.md"\n]',
    "4e363be6f208": '[\n  "alpha.md",\n  "delta.md",\n  "epsilon.md",\n  "zeta.md"\n]',
    "e1f53786b594": '[\n  "delta.md",\n  "epsilon.md",\n  "gamma.md",\n  "zeta.md"\n]',
    "4f53cda18c2b": "[]",
    
    "7b339833968f": (
        "---\n"
        "name: alpha\n"
        'model: "anthropic/claude-sonnet-5"\n'
        'tools: "read, grep, find, ls, bash, edit, write, csharp_lsp_definition, '
        'agent_context_get_doc"\n'
        'description: Finds "things": fast ' + EM_DASH + " always.\n"
        "---\n"
        "\n"
        "Alpha body.\n"
    ),
    "dd8349fa81df": (
        "---\n"
        "name: alpha\n"
        'model: "sonnet"\n'
        'tools: "read, grep, find, ls, bash, edit, write, csharp_lsp_definition, '
        'agent_context_get_doc"\n'
        'description: Finds "things": fast ' + EM_DASH + " always.\n"
        "---\n"
        "\n"
        "Alpha body.\n"
    ),
    "9bc333cfffd4": (
        "---\n"
        "name: beta\n"
        "description: Only tools with no equivalent.\n"
        "model: 'amazon-bedrock/claude-opus-6'\n"
        "tools: ToolSearch, Task\n"
        "---\n"
        "Beta body.\n"
    ),
    "450898096525": (
        "---\n"
        "name: beta\n"
        "description: Only tools with no equivalent.\n"
        "model: 'opus'\n"
        "tools: ToolSearch, Task\n"
        "---\n"
        "Beta body.\n"
    ),
    "3eec3d3dee67": (
        "---\n"
        "name: gamma\n"
        "description: Tier carried only by an unauthenticated provider.\n"
        "model: amazon-bedrock/claude-haiku-4-5\n"
        "tools: read, grep\n"
        "---\n"
        "Gamma body.\n"
    ),
    "40354038a2cd": (
        "---\n"
        "name: gamma\n"
        "description: Tier carried only by an unauthenticated provider.\n"
        "model: haiku\n"
        "tools: read, grep\n"
        "---\n"
        "Gamma body.\n"
    ),
    "b2a66fbe5a54": (
        "---\n"
        "name: delta\n"
        "description: Tier no catalog carries.\n"
        "model: fable\n"
        "tools: bash\n"
        "---\n"
        "Delta body.\n"
    ),
    "549a3bff7a94": (
        "---\n"
        "metadata:\n"
        "  model: amazon-bedrock/claude-opus-6\n"
        "name: epsilon\n"
        "description: A versioned model id after a nested key.\n"
        "model: claude-sonnet-4-5\n"
        "tools: read\n"
        "---\n"
        "Epsilon body.\n"
    ),
    "2ff469cac949": (
        "---\n"
        "metadata:\n"
        "  model: opus\n"
        "name: epsilon\n"
        "description: A versioned model id after a nested key.\n"
        "model: claude-sonnet-4-5\n"
        "tools: read\n"
        "---\n"
        "Epsilon body.\n"
    ),
    "bf0ab4ecca39": (
        "---\n"
        "name: zeta\n"
        "description: Uppercase alias.\n"
        "model: amazon-bedrock/claude-opus-6\n"
        "tools: find, ls\n"
        "---\n"
        "Zeta body.\n"
    ),
    "0be08996cc9c": (
        "---\n"
        "name: zeta\n"
        "description: Uppercase alias.\n"
        "model: OPUS\n"
        "tools: find, ls\n"
        "---\n"
        "Zeta body.\n"
    ),
    "9eb6447e6101": "---\ndescription: No name.\ntools: read\n---\nNoname body.\n",
    "26f6dc3fd277": "Just text, no frontmatter.\n",
    "d63a1420b1d7": ("alpha.md\nbeta.md\ndelta.md\nepsilon.md\ngamma.md\nnoname.md\n"
                     "plain.md\nzeta.md\n"),
}


def golden(stdout, changed=None, removed=(), rc=0, stderr=""):
    return {"rc": rc, "stdout": stdout, "stderr": stderr, "changed": dict(changed or {}),
            "removed": sorted(removed)}


OC = "home/.config/opencode/agent/"
OC_SKIPPED = ("SKIPPED beta.md (no tool name translated); noname.md (no name/description); "
              "plain.md (no name/description); dropped unmappable tools Task ToolSearch\n")
OC_STDOUT = "opencode-agents-pin: 5 agent(s) -> <ROOT>/home/.config/opencode/agent; " + OC_SKIPPED

PI = "home/.pi/agent/agents/"
PI_TOOLS = "tools translated alpha(9) delta(1) epsilon(1) noname(1) zeta(2); "
PI_DROPPED = "DROPPED unmappable tools MultiEdit NotebookEdit Skill Task ToolSearch WebFetch WebSearch\n"
PI_NOTES = ("pinned alpha=anthropic/claude-sonnet-5 beta=amazon-bedrock/claude-opus-6 "
            "epsilon=amazon-bedrock/claude-opus-6 gamma=amazon-bedrock/claude-haiku-4-5 "
            "zeta=amazon-bedrock/claude-opus-6; " + PI_TOOLS
            + "UNRESOLVED alias kept in delta.md; " + PI_DROPPED)
PI_STDOUT = "pi-agents-pin: ~/.pi/agent/agents <- 8 definition(s); " + PI_NOTES


def pi_full(prefix):
    return {prefix + PI_MANIFEST: "d63a1420b1d7", prefix + "alpha.md": "7b339833968f",
            prefix + "beta.md": "9bc333cfffd4", prefix + "delta.md": "b2a66fbe5a54",
            prefix + "epsilon.md": "549a3bff7a94", prefix + "gamma.md": "3eec3d3dee67",
            prefix + "noname.md": "9eb6447e6101", prefix + "plain.md": "26f6dc3fd277",
            prefix + "zeta.md": "bf0ab4ecca39"}


OC_FULL = {OC + OPENCODE_MANIFEST: "d33b29229d39", OC + "alpha.md": "04dd22ee92e9",
           OC + "delta.md": "9458c9c4b789", OC + "epsilon.md": "01d1fb67b2a1",
           OC + "gamma.md": "65efd640c2e9", OC + "zeta.md": "c6ae37361f88"}

GOLDENS = {
    "opencode-default": [golden(OC_STDOUT, OC_FULL), golden(OC_STDOUT)],
    "opencode-explicit-dst": [golden(
        "opencode-agents-pin: 4 agent(s) -> <ROOT>/custom-agent; pruned stale.md; "
        "LEFT ALONE (not ours): alpha.md; " + OC_SKIPPED,
        {"custom-agent/" + OPENCODE_MANIFEST: "e1f53786b594",
         "custom-agent/delta.md": "9458c9c4b789", "custom-agent/epsilon.md": "01d1fb67b2a1",
         "custom-agent/gamma.md": "65efd640c2e9", "custom-agent/zeta.md": "c6ae37361f88"},
        ["custom-agent/stale.md"])],
    "opencode-bad-manifest": [golden(
        "opencode-agents-pin: 4 agent(s) -> <ROOT>/home/.config/opencode/agent; "
        "LEFT ALONE (not ours): gamma.md; " + OC_SKIPPED,
        {OC + OPENCODE_MANIFEST: "4e363be6f208", OC + "alpha.md": "04dd22ee92e9",
         OC + "delta.md": "9458c9c4b789", OC + "epsilon.md": "01d1fb67b2a1",
         OC + "zeta.md": "c6ae37361f88"})],
    "opencode-no-catalog": [golden(OC_STDOUT, dict(
        OC_FULL, **{OC + "alpha.md": "7150520f9cde", OC + "zeta.md": "07d74a860155"}))],
    "opencode-empty-src": [golden("opencode-agents-pin: pruned stale.md\n",
                                  {OC + OPENCODE_MANIFEST: "4f53cda18c2b"}, [OC + "stale.md"])],
    "opencode-not-installed": [golden("")],
    "opencode-missing-src": [golden("")],
    "pi-home": [golden(PI_STDOUT, pi_full(PI)), golden(PI_STDOUT)],
    "pi-project": [golden("pi-agents-pin: <ROOT>/repo/.pi/agents <- 8 definition(s); " + PI_NOTES,
                          pi_full("repo/.pi/agents/"))],
    "pi-owned-and-foreign": [golden(PI_STDOUT, pi_full(PI), [PI + "stale.md"])],
    "pi-no-catalog": [golden(
        "pi-agents-pin: ~/.pi/agent/agents <- 8 definition(s); " + PI_TOOLS
        + "UNRESOLVED alias kept in alpha.md beta.md delta.md epsilon.md gamma.md zeta.md; "
        + PI_DROPPED,
        dict(pi_full(PI), **{PI + "alpha.md": "dd8349fa81df", PI + "beta.md": "450898096525",
                             PI + "epsilon.md": "2ff469cac949", PI + "gamma.md": "40354038a2cd",
                             PI + "zeta.md": "0be08996cc9c"}))],
    "pi-empty-src": [golden("", removed=[PI + "stale.md"])],
    "pi-not-installed": [golden("")],
    "pi-missing-src": [golden("")],
}


def main():
    check_goldens()
    check_usage()
    check_atomic()
    check_callers()
    print("\n%d passed, %d failed" % (passed, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
