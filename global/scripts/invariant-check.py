#!/usr/bin/env python3
"invariant-check — assert every lesson this store has paid for, at every site.\n\nWhy this exists. The recurring defect in this system is the half-applied\ninvariant: a lesson is learned at one site, written into that site's\ncomments, and never applied to the other sites in its class. The lesson looks\nhandled -- there is an audit observation, there is a resolution note, there is a\nparagraph of commentary in the file -- while the same hole stays open two files\nover. Examples:\n\nProse cannot fix this and neither can diligence. A lesson is not learned until it is\nan executable check that enumerates its own sites, and a fix is not done until that\ncheck passes at all of them.\n\nThe false-positive rule, which matters more than any individual check. A naive regex\nsweep flags inspectors, delegators and tests along with the sites that violate, and a\ncheck that cries wolf is a check people learn to skim. So:\n\n  * every exception is an explicit `allow` entry with a reason, never a loosened\n    pattern;\n  * a check that cannot be made precise is deleted, not tolerated;\n  * `--verify` re-runs every allow entry and fails if one no longer matches, so the\n    allowlist cannot rot into a silence.\n\nUSAGE\n  invariant-check.py                 human report, exit 1 if violations\n  invariant-check.py --json          machine-readable\n  invariant-check.py --health        write a health verdict; preflight surfaces it\n  invariant-check.py --paths a b     only sites among these paths (pre-commit gate)\n  invariant-check.py --list          the registry, with the incident behind each\n  invariant-check.py --verify        check the allowlists themselves for rot\n\nObservations guarded, beyond those cited beside a rule: #241, #275, #290, #347, #396."
import ast
import collections
import contextlib
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tokenize

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task

WORKTREE_GLOB = "*/%s/worktrees/*|*/%s/worktrees/*" % (hp.AGENTS_DIRNAME, hp.CLAUDE_DIRNAME)

STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context"))
G = os.path.join(STORE, "global")
HEALTH_RECORD = os.path.join(hp.scripts_dir(), "health-record.py")


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _hooks(*exts):
    out = []
    for e in exts:
        out += glob.glob(os.path.join(G, "hooks", "*." + e))
    return sorted(out)


def _scripts(*exts):
    out = []
    for e in exts:
        out += glob.glob(os.path.join(G, "scripts", "*." + e))
    return sorted(out)


def _project_scripts(*exts):
    out = []
    for e in exts:
        out += glob.glob(os.path.join(STORE, "projects", "*", "scripts", "*." + e))
    return sorted(out)


def _project_hooks(*exts):
    out = []
    for e in exts:
        out += glob.glob(os.path.join(STORE, "projects", "*", "hooks", "*." + e))
    return sorted(out)


def _project_of(path):
    'Project name for a path under STORE/projects/<name>/..., else None.'
    root = os.path.realpath(os.path.join(STORE, "projects")) + os.sep
    real = os.path.realpath(path)
    return real[len(root):].split(os.sep, 1)[0] if real.startswith(root) else None


def _allow_key(p):
    'Allow-list lookup key: basename, except under projects/, where the project\n    name is prefixed -- project scripts share stems (wt-finish x5, wt-sweep x3) and\n    basename alone would collide across them.'
    project = _project_of(p)
    return project + "/" + os.path.basename(p) if project else os.path.basename(p)


def _docs():
    'Every doc, at any depth — docs/ nests, unlike the flat entity buckets.'
    return sorted(glob.glob(os.path.join(G, "docs", "**", "*.md"), recursive=True))


def _memories():
    return sorted(glob.glob(os.path.join(G, "memory", "**", "*.md"), recursive=True))


def _skills():
    'Skill bodies. A skill is a procedure followed verbatim, so a rule stated in one\n    section and omitted from another is a rule that gets followed half the time.'
    return sorted(glob.glob(os.path.join(G, "skills", "**", "*.md"), recursive=True))


def _instructions():
    return sorted(glob.glob(os.path.join(G, "instructions", "*.md")))


def _agents():
    'Worker definitions. A mechanism can be prose, and for a dispatch-time constraint\n    it has to be -- there is no hook that can enforce what a brief asks of a worker.'
    return sorted(glob.glob(os.path.join(G, "agents", "*.md")))


def _server_py():
    "The store server's own modules. Not an entity kind, so no other lister\n    reaches them — which is precisely why the atomic-write defect below could live\n    in the code that persists every entity while every checker looked elsewhere."
    d = os.path.join(STORE, "server", "src", "agent_context")
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py"))




_TRUNCATING_OPEN = re.compile(r'(?<!os\.fd)\bopen\(\s*([^,()]+(?:\([^()]*\))?[^,()]*?)\s*,'
                              r'\s*["\']w["\']')


def _store_write_not_atomic(path, src):
    'A store write must not truncate the file before it has the new bytes.\n\n    audit.py, fleet.py, usage.py and daemon.py\n    write through tmp + os.replace. The entity writers — store.py,\n    session.py, projects.py, i.e. every doc, memory, instruction, skill, command,\n    hook and script — did not. The whole knowledge base was guarded less carefully\n    than the usage counters. Textbook half-applied invariant, which is why it is a\n    check and not a comment.'
    code = _uncommented(src)
    hits = []
    for m in _TRUNCATING_OPEN.finditer(code):
        target = m.group(1).strip()
        
        
        
        
        if re.search(r'\btmp\b|lock', target, re.I) or target.isdigit():
            continue
        hits.append("open(%s, \"w\") at line %d"
                    % (target[:38], code.count("\n", 0, m.start()) + 1))
    if not hits:
        return None
    return "%s — write through store._write_atomic() instead" % "; ".join(hits[:3])


def _fsync_not_behind_the_switch(path, src):
    'Every flush in the server goes through the one durability switch.\n\n    fsync is not free, and its price is set by the slowest volume in the fleet\n    (registry entry fsync-stays-behind-one-switch states the cost).\n\n    So the flush stays switchable and there stays exactly one switch. A second call\n    site added later without the guard reintroduces the stall on whichever node has\n    the slowest disk, which is never the one running this check.\n\n    The guard must be visible from the call, so only the few lines above it are\n    consulted: a reader looking at an fsync has to be able to see what makes it\n    conditional without tracing the enclosing function.'
    code = _uncommented(src)
    hits = []
    for m in re.finditer(r"\bos\.fsync\(", code):
        
        
        before = code[:m.start()].split("\n")
        preceding = before[-1:] + [line for line in before[:-1] if line.strip()][-2:]
        if any("_fsync_enabled()" in line for line in preceding):
            continue
        hits.append("os.fsync at line %d" % (code.count("\n", 0, m.start()) + 1))
    if not hits:
        return None
    return "%s -- guard it with `if _fsync_enabled():`" % "; ".join(hits[:3])


def _hand_rolled_atomic_write(path, src):
    'Every atomic write in the server goes through paths.write_atomic.'
    if os.path.basename(path) == "paths.py":
        return None
    code = _uncommented(src)
    hits = ["os.replace at line %d" % (code.count("\n", 0, m.start()) + 1)
            for m in re.finditer(r"\bos\.replace\(", code)]
    if not hits:
        return None
    return "%s -- write through paths.write_atomic(path, text) instead" % "; ".join(hits[:3])


_PROJECTION_SCRIPTS = ("harness-materialize.py", "agents-pin.py", "home-settings-sync.py",
                       "lspd.py", "health-record.py")


def _projection_py():
    "The scripts that write files ANOTHER PROCESS executes or loads: pi extensions,\n    the opencode guard plugin, agent definitions, settings.json, the LSP daemon's own\n    state. A half-written one of these is loaded by the next session to start."
    return [p for p in _scripts("py") if os.path.basename(p) in _PROJECTION_SCRIPTS]





_BARE_PYTHON_COMMAND = re.compile(
    r"""\[\s*["']python(?:\d+(?:\.\d+)?)?["']|f?["']python(?:\d+(?:\.\d+)?)? """)


def _bare_python_command(path, src):
    "A command names this host's interpreter, never a bare python3 or python."
    code = _uncommented(src)
    hits = ["line %d" % (code.count("\n", 0, m.start()) + 1)
            for m in _BARE_PYTHON_COMMAND.finditer(code)]
    if not hits:
        return None
    return ("starts a command with a bare python3/python at %s -- use sys.executable for a "
            "subprocess, agent-python.py interpreter() for a rendered command"
            % ", ".join(hits[:3]))


def _own_decision_regex(path, src):
    'A hook must not carry its own decision-phrase list; decision-phrases.py is it.'
    if os.path.basename(path) == "decision-phrases.py":
        return None
    if re.search(r"^\s*DECISION\s*=\s*re\.compile", _uncommented(src), re.M):
        return "defines its own DECISION regex -- load ~/.agent-context/global/scripts/decision-phrases.py"
    return None


_TRIPLE_OPEN = re.compile(r"^[rRbBuUfF]*('''|\"\"\")")


def _blank_triple_strings(src):
    '`src` with every triple-quoted string blanked, newlines kept.'
    def blank(m):
        return "\n" * m.group(0).count("\n")

    def regex(text):
        text = re.sub(r'"""(?:.|\n)*?"""', blank, text)
        return re.sub(r"'''(?:.|\n)*?'''", blank, text)

    try:
        ast.parse(src)
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (SyntaxError, ValueError, tokenize.TokenError):
        return regex(src)
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    spans, depth, opened = [], 0, None
    for tok in toks:
        if fstring_start is not None and tok.type == fstring_start:
            if depth == 0:
                opened = tok.start if _TRIPLE_OPEN.match(tok.string) else None
            depth += 1
        elif fstring_end is not None and tok.type == fstring_end:
            depth -= 1
            if depth == 0 and opened is not None:
                spans.append((opened, tok.end))
                opened = None
        elif depth == 0 and tok.type == tokenize.STRING and _TRIPLE_OPEN.match(tok.string):
            spans.append((tok.start, tok.end))
    lines = src.split("\n")
    for (sr, sc), (er, ec) in reversed(spans):
        first, last = lines[sr - 1], lines[er - 1]
        if sr == er:
            lines[sr - 1] = first[:sc] + first[ec:]
            continue
        lines[sr - 1] = first[:sc]
        for i in range(sr, er - 1):
            lines[i] = ""
        lines[er - 1] = last[ec:]
    return "\n".join(lines)


def _uncommented(src):
    'Source with comments AND python docstrings dropped, so prose never trips a check.\n\n    Docstring stripping is not fussiness. The first version of this only dropped `#`\n    lines, and `store-git-write-merge-guard` promptly reported lspd.py -- whose only\n    `git add` is the phrase "the index mtime moves on `git add`" inside a docstring\n    explaining why it watches the index. That is the exact false positive this whole\n    file exists to avoid, produced by this file, on its first run. A checker earns its\n    allowlist only after it stops flagging prose.\n\n    Dropped lines are BLANKED, not removed, so a check that counts newlines in the result\n    cites the line a human opens. Removing them reported every line after a docstring\n    short by the docstring\'s length (test-invariant-line-numbers.py).'
    src = _blank_triple_strings(src)
    keep = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#") or s.startswith("*") or s.startswith("//"):
            keep.append("")
            continue
        keep.append(line)
    return "\n".join(keep)


def _code_lines(src):
    '[(lineno, code)] with comment and docstring lines BLANKED, not removed.\n\n    Blanked rather than dropped so a finding can still cite the line a human would\n    open. Every check that reports line numbers must run through this: two of the\n    six checks in the first draft reported prose as code -- `no-hardcoded-home`\n    flagged token-usage-collect.py for the sentence "the laptop\'s /Users/user/... "\n    inside the docstring that EXPLAINS why paths are normalized, and flagged this\n    file for its own search pattern.'
    lines = src.splitlines()
    out, in_doc, delim = [], False, ""
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if in_doc:
            out.append((i, ""))
            if delim in s:
                in_doc = False
            continue
        m = re.match(r'^[rbfu]*("""|\'\'\')', s)
        if m:
            delim = m.group(1)
            
            if s.count(delim) < 2:
                in_doc = True
            out.append((i, ""))
            continue
        if s.startswith("#") or s.startswith("*") or s.startswith("//"):
            out.append((i, ""))
            continue
        out.append((i, line))
    return out


def _statement_at(code, idx, span=15):
    "The whole logical statement beginning at code[idx], as one string.\n\n    Shell guards do not always sit on the line that needs guarding. A pipeline feeding\n    a `while ... done` carries its `|| true` on the `done`, and a `\\`-continued command\n    carries it on the last physical line. Judging the first line alone reported\n    the shell project-materialize:551 and agents-materialize:271 as unguarded when both are\n    guarded three lines down -- and agents-materialize's guard has a ten-line comment\n    above it explaining the false DEGRADED banner its absence once caused. A checker\n    that flags the documented fix is worse than no checker.\n\n    Depth-counted rather than keyword-anchored on the FIRST line, which was the second\n    bug in this helper: `ls ... | while read; do` opens a block while starting with\n    `ls`, so anchoring on the opening keyword stopped at line one and still missed the\n    `done || true` two lines below."
    parts, depth = [], 0
    for j in range(idx, min(idx + span, len(code))):
        text = code[j][1]
        s = text.strip()
        parts.append(text)
        depth += len(re.findall(r"(?:^|;|\s)(?:do|then)\b", s))
        depth += len(re.findall(r"(?:^|;|\s)case\b", s))
        depth -= len(re.findall(r"^\s*(?:done|fi|esac)\b", s))
        depth -= len(re.findall(r";;\s*esac\b", s))
        if s.endswith("\\"):
            continue
        if depth <= 0:
            break
    return " ".join(p.rstrip("\\") for p in parts)


class Invariant:
    'One lesson, its incident, its site set, and its verified exceptions.'

    def __init__(self, ident, rule, incident, sites, violated, allow=None, fix=None):
        self.id = ident
        self.rule = rule            
        self.incident = incident    
        self.sites = sites          
        self.violated = violated    
        self.allow = allow or {}    
        self.fix = fix or ""        

    def run(self, limit=None):
        found, checked = [], 0
        for p in self.sites():
            base = _allow_key(p)
            if limit is not None and os.path.realpath(p) not in limit:
                continue
            if base in self.allow:
                continue
            checked += 1
            why = self.violated(p, _read(p))
            if why:
                found.append({"site": base, "path": p,
                              "detail": why if isinstance(why, str) else self.rule})
        return found, checked

    def verify_allow(self):
        'An allow entry that would no longer fire is stale -- report it.\n\n        An allowlist is the part of a checker that rots silently: the site gets\n        fixed, or deleted, and the exemption stays behind quietly weakening the\n        check for a file that no longer needs it.'
        stale = []
        present = {_allow_key(p): p for p in self.sites()}
        for base, reason in self.allow.items():
            if base not in present:
                stale.append((base, "site no longer exists"))
                continue
            if not self.violated(present[base], _read(present[base])):
                stale.append((base, "no longer violates; exemption is dead weight"))
        return stale












_SIDE_EFFECTING = (
    "home-materialize.py",         
    "machine-bootstrap.py",        
    "self-heal.py",                
    "refresh-intellij-server.py",  
    
    
    
    "lsp-canary.py",
)









_SIDE_EFFECTING_NAME = re.compile(
    r"^(verify-.*|.*-run|.*-canary|.*-materialize|.*-commit|.*-sync|release-.*|.*-bootstrap"
    r"|refresh-.*|self-heal|.*-sweep|.*-prune|.*-install|.*-deploy.*|.*-reset|.*-finish.*"
    r"|.*-seed|.*-apply)\.py$")


def _side_effecting_scripts():
    out = []
    for p in _scripts("py") + _project_scripts("py") + _project_hooks("py"):
        base = os.path.basename(p)
        if base in _SIDE_EFFECTING:
            out.append(p)
        elif (_SIDE_EFFECTING_NAME.match(base) and not base.startswith("test-")
              and "port-cases" not in base):
            out.append(p)
    return out


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _names_wt_finish_core(node):
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "wt_finish_core.py" in n.value for n in ast.walk(node))


def _delegates_to_wt_finish_core(src):
    'A project wt-finish-core.py that loads the shared core and hands it the run. The\n    core parses the flags, judges gate relevance and builds the worktree paths.\n\n    Structural: the source must make a real call to run_project, and pass a path naming\n    wt_finish_core.py to a loader call (spec_from_file_location, or a path join that\n    builds it). A file that only mentions both in a string does not count.'
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    runs = loads = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "run_project":
            runs = True
        elif name in ("spec_from_file_location", "join") and any(
                _names_wt_finish_core(a) for a in list(node.args) + [k.value for k in node.keywords]):
            loads = True
    return runs and loads


def _script_does_not_parse_flags(path, src):
    if os.path.basename(path) == "wt-finish-core.py" and _delegates_to_wt_finish_core(src):
        return None
    code = _uncommented(src)
    handles_help = ('"--help"' in code or "'--help'" in code
                    or "ArgumentParser" in code or "add_argument" in code)
    
    
    refuses = ("ArgumentParser" in code
               or re.search(r"unknown flag", src, re.I) is not None)
    if not handles_help and not refuses:
        return "no flag parsing at all: an unknown flag, --help included, runs the live path"
    if not handles_help:
        return "refuses unknown flags but does not answer --help"
    if not refuses:
        return "answers --help but lets an unrecognized flag through to the live path"
    return None








def _wt_finish_scripts():
    "Every project's landing logic. Not a global entity kind, so nothing else\n    lists them -- which is how three of five grew their own copy of one judgment\n    while two had none, and nobody could see the spread. The logic lives in\n    wt-finish-core.py; wt-finish.sh is a one-line launcher that execs it. A ported\n    project file delegates to the shared global/scripts/wt_finish_core.py, which is a\n    site too."
    return sorted(glob.glob(os.path.join(STORE, "projects", "*", "scripts",
                                         "wt-finish-core.py"))
                  + glob.glob(os.path.join(G, "scripts", "wt_finish_core.py")))


def _landing_gate_judges_relevance(path, src):
    'A landing script must ask whether the gate can affect this change at all.\n\n    The check is that the judgment is DELEGATED, not merely present. Three of the\n    five scripts had each grown their own inline copy with a different safe list\n    and a different message, and the observation stayed open for the two that had\n    none -- the half-applied shape, inside the fix for it. Per-repo exclusions are\n    still per-repo and are passed as arguments; only the judgment is shared.'
    body = _uncommented(src)
    if "GATE_RELEVANCE" in body:
        return None
    if os.path.basename(path) == "wt-finish-core.py" and _delegates_to_wt_finish_core(src):
        return None
    if re.search(r"gate_relevant|build_relevant", body):
        return ("judges gate relevance with its own inline copy -- call "
                "gate-relevance.py and pass this repo's exclusions instead")
    return "never asks whether the landing gate can affect the change"


def _deployed_matchers(name):
    'Every matcher ~/.claude/settings.json actually wires this hook under.\n\n    A hook can legitimately appear in more than one group, so this returns the\n    UNION. Empty means settings.json does not wire it at all, which is not the\n    same as an empty matcher and must not be read as one.'
    path = hp.settings_file()
    if not os.path.exists(path):
        return None
    
    
    
    
    
    
    
    settings = json.loads(_read(path))
    found = []
    for groups in _effective_hooks(path, settings).values():
        for g in groups:
            for h in g.get("hooks") or []:
                cmd = h.get("command", "") or ""
                if re.search(r"\b%s\.(?:sh|py)\b" % re.escape(name), cmd):
                    found.append(g.get("matcher", "") or "")
    if not found:
        return None
    if any(m == "*" for m in found):
        return "*"
    return "|".join(sorted({p for m in found for p in m.split("|") if p}))


def _hook_matcher(path):
    "The matcher a hook is actually WIRED with — from settings.json first.\n\n    Read from the DEPLOYED wiring, falling back to the .meta.toml sidecar only\n    when settings.json does not mention the hook at all (another harness's, or a\n    machine that has not materialized yet)."
    deployed = _deployed_matchers(os.path.basename(path).rsplit(".", 1)[0])
    if deployed is not None:
        return deployed
    meta = _read(path + ".meta.toml")
    m = re.search(r'^matcher\s*=\s*"([^"]*)"', meta, re.M)
    return m.group(1) if m else ""


_PROSE_TOOLS_CACHE = {}


def _prose_tools():
    '{tool: [text fields]} from store-prose-tools, the derived registry.\n\n    Empty on any failure, which makes the invariant below report zero sites\n    rather than a false clean run -- `checked` drops to 0 and that is visible in\n    the summary line. An exit 2 from the registry (a server field nobody has\n    classified) is itself the finding, and it is loud where it happens.'
    if _PROSE_TOOLS_CACHE:
        return _PROSE_TOOLS_CACHE
    script = os.path.join(G, "scripts", "store-prose-tools.py")
    if not os.path.exists(script):
        return {}
    try:
        out = subprocess.run([sys.executable, script, "--json"],
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return {}
        _PROSE_TOOLS_CACHE.update(json.loads(out.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return _PROSE_TOOLS_CACHE









_GENERAL_GUARD_SHARE = 2.0 / 3.0


def _prose_guard_is_short(path, src):
    "A guard over store writes that misses a tool, or matches one it cannot read.\n\n    The second half is subtler and is what makes a widened matcher a lie: the\n    matcher fires on the new tool, the extractor never learned its field name, so\n    the guard receives the call, reads an empty body and allows it. A hook that\n    matches a tool must reference at least one of that tool's text fields; asking\n    for EVERY field would need per-hook exemptions (the language check has no\n    business scanning upsert_memory's `metadata`, which is JSON), and an\n    exemption list is what rots."
    registry = _prose_tools()
    if not registry:
        return None
    matcher = _hook_matcher(path)
    if "mcp__agent-context__" not in matcher:
        return None
    named = {t for t in registry if ("mcp__agent-context__" + t) in matcher}
    
    
    
    
    
    
    
    
    
    
    
    declared = _read(path + ".meta.toml")
    m = re.search(r'^matcher\s*=\s*"([^"]*)"', declared, re.M)
    declared_names = {t for t in registry
                      if ("mcp__agent-context__" + t) in (m.group(1) if m else "")}
    general = max(len(named), len(declared_names))
    if general < len(registry) * _GENERAL_GUARD_SHARE:
        return None
    problems = []
    missing = sorted(set(registry) - named)
    if missing:
        problems.append("matcher misses %s" % ", ".join(missing))
    unreadable = []
    for tool in sorted(named):
        fields = registry[tool]
        
        probes = [f.split("[")[0] for f in fields]
        if tool == "bulk_edit":
            probes = ["replacements"]
        if not any(re.search(r"\b%s\b" % re.escape(p), src) for p in probes):
            unreadable.append(tool)
    if unreadable:
        problems.append("matches but cannot read %s" % ", ".join(unreadable))
    return "; ".join(problems) if problems else None


def _effective_hooks(path, settings):
    "settings.json's hooks with dispatched events expanded through hook-registry.py.\n\n    Consolidation Phase 3a moves an event's guards into hook-dispatch.json and leaves\n    settings.json naming one hook-dispatch.py command. Read raw, every dispatched guard\n    would look undeployed to the two checks that read the wiring."
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hook_registry", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-registry.py"))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load hook-registry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.effective_hooks(path, settings)


_HOOKS_KEY = re.compile(r"""(?:\.get\(\s*|\[\s*)["']hooks["']""")


def _hook_reader_skips_registry(path, src):
    'A script that reads the hooks out of settings.json without hook-registry.py.'
    if "settings.json" not in src or not _HOOKS_KEY.search(src):
        return None
    if "hook-registry" in src:
        return None
    return ("reads the hooks in settings.json without hook-registry.py, so a dispatched "
            "event's guards look unregistered to it")


def _codex_hook_parity_not_dynamic(path, src):
    "Codex must derive from Claude's generated graph, never a sibling registry."
    required = {
        "harness-materialize.py": (
            'settings.get("hooks"', "render_codex_hooks(source",
            "persist_codex_hook_trust", '".codex", "hooks.json"',
        ),
        "codex-hook-adapter.py": (
            '"PostToolUseFailure"', '"Notification"', '"exec_command": "Bash"',
        ),
    }
    missing = [needle for needle in required.get(os.path.basename(path), ()) if needle not in src]
    return "missing " + ", ".join(missing) if missing else None


def _codex_test_unlock_not_guarded(path, src):
    "Codex's approval handler must be wired and agent invocation must be denied."
    sync = _read(os.path.join(G, "scripts", "home-settings-sync.py"))
    guard = _read(os.path.join(G, "hooks", "block-consent-self-grant.py"))
    missing = []
    if '"codex-test-unlock.py"' not in sync:
        missing.append("UserPromptSubmit wiring")
    if '"codex-test-unlock": LOCK_DENY' not in guard:
        missing.append("self-grant guard")
    if "approval.validate(question)" not in src or "approval.grant(spec" not in src:
        missing.append("validated grant path")
    return "missing " + ", ".join(missing) if missing else None


def _codex_content_parity(path, src):
    'Every Claude content path must have a Codex projection call site.'
    required = {
        "harness-materialize.py": ("materialize_codex_content(HOME, STORE, None",
                                    "materialize_codex_mcp(HOME, manifest",
                                    '"--codex-project"'),
        "agents-materialize.py": ('"--codex-project", root',),
    }
    missing = [item for item in required.get(os.path.basename(path), ()) if item not in src]
    if missing:
        return "missing " + ", ".join(missing)
    if os.path.basename(path) == "mcp-servers.json":
        if not src.strip():
            return None  
        try:
            servers = json.loads(src)["servers"]
        except (ValueError, KeyError):
            return "mcp-servers.json is not valid JSON with a servers map"
        missing = [name for name, spec in servers.items()
                   if "claude" in spec.get("harnesses", []) and name != "xcode"
                   and "codex" not in spec.get("harnesses", [])]
        if missing:
            return "Codex MCP missing: " + ", ".join(missing)
        if "codex" in servers["xcode"].get("harnesses", []):
            return "exclusive xcode bridge must stay Claude-only"
    return None


def _unwired_on_purpose():
    "Hook files hook-registration-probe's EXEMPT map lists, each with its reason there:\n    run by something other than a settings.json event, so absence is not a fault."
    probe = _read(os.path.join(G, "scripts", "hook-registration-probe.py"))
    block = re.search(r"^EXEMPT = \{\n(.*?)^\}", probe, re.M | re.S)
    return set(re.findall(r'^    "([\w.-]+)":', block.group(1), re.M)) if block else set()


def _deployed_events(name):
    'Every event ~/.claude/settings.json wires this hook under. None = absent.'
    path = hp.settings_file()
    if not os.path.exists(path):
        return None
    settings = json.loads(_read(path))
    found = set()
    for event, groups in _effective_hooks(path, settings).items():
        for g in groups:
            for h in g.get("hooks") or []:
                if re.search(r"\b%s\.(?:sh|py)\b" % re.escape(name), h.get("command", "") or ""):
                    found.add(event)
    return found or None


def _hook_deploy_diverges(path, src):
    'A hook whose DEPLOYED wiring does not carry what its store row declares.\n\n    The declaration and the deployment are different files maintained by different\n    hands: the `.meta.toml` sidecar states the intent, and a table inside\n    home-settings-sync.py is what actually reaches settings.json. Nothing compared\n    them until this check existed, and the gap is silent in the worst direction --\n    the sidecar reads correct, so every audit that consults it reports clean while\n    the guard runs narrow or does not run at all.\n\n    Both failure modes are measured, not hypothetical:\n\n    The second case is why absence is a violation and not merely a warning: a\n    guardrail nobody deployed is indistinguishable, from every source anyone reads,\n    from one that works.'
    name = os.path.basename(path).rsplit(".", 1)[0]
    meta = _read(path + ".meta.toml")
    if not meta:
        return None
    m = re.search(r'^matcher\s*=\s*"([^"]*)"', meta, re.M)
    e = re.search(r'^event_type\s*=\s*"([^"]*)"', meta, re.M)
    declared_matcher = {p for p in (m.group(1) if m else "").split("|") if p}
    declared_event = e.group(1) if e else ""

    events = _deployed_events(name)
    if events is None and os.path.basename(path) in _unwired_on_purpose():
        return None
    if events is None:
        return ("declared %s but is not wired in ~/.claude/settings.json at all -- "
                "it has never fired" % (declared_event or "a PreToolUse hook"))
    if declared_event and declared_event not in events:
        return "declares event %s; settings.json wires it only under %s" % (
            declared_event, ", ".join(sorted(events)))

    deployed = _deployed_matchers(name)
    if deployed == "*" or not declared_matcher:
        return None
    have = {p for p in (deployed or "").split("|") if p}
    missing = sorted(declared_matcher - have)
    if missing:
        return "settings.json matcher is missing %s" % ", ".join(missing)
    return None


def _py_hook_no_crash_handler(path, src):
    if re.search(r"except\s+(Exception|BaseException)\b", src):
        return None
    return "entry point not wrapped in `except Exception`"


_HEREDOC = re.compile(r"<<-?\s*'?\"?(\w+)'?\"?\n.*?^\1\b", re.S | re.M)


def _unquoted(text):
    '`text` with heredoc bodies and quoted spans removed.\n\n    Deliberately crude: it does not model shell quoting exactly. It only has to move the\n    boundary from "the word appears" to "the word appears where a command can run", and a\n    real invocation is unquoted at command position by construction.'
    
    
    
    
    
    if "import " in text or text.lstrip().startswith(("#!/usr/bin/env python", "\"\"\"")):
        try:
            import ast
            tree = ast.parse(text)
            lines = text.splitlines(keepends=True)
            offs, n = [0], 0
            for ln in lines:
                n += len(ln)
                offs.append(n)

            def pos(lineno, col):
                return offs[lineno - 1] + col

            spans = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.end_lineno is not None:
                        spans.append((pos(node.lineno, node.col_offset),
                                      pos(node.end_lineno, node.end_col_offset)))
            out = list(text)
            for a, b in spans:
                for k in range(a, min(b, len(out))):
                    if out[k] != "\n":
                        out[k] = " "
            return "".join(out)
        except SyntaxError:
            pass  

    
    t = _HEREDOC.sub(" ", text)
    t = re.sub(r"'[^'\n]*'", " ", t)
    t = re.sub(r'"[^"\n]*"', " ", t)
    return t


def _store_write_no_merge_guard(path, src):
    
    
    
    
    body = _uncommented(_unquoted(src))
    if not re.search(r"git\s+(-C\s+\S+\s+)?add\b", body):
        return None
    if "MERGE_HEAD" in body or "--unmerged" in body:
        return None
    return "`git add` with no MERGE_HEAD / --unmerged check"


def _health_writer_no_fail_path(path, src):
    body = _uncommented(src)
    if "health-record" not in body:
        return None
    if "--fail" in body:
        return None
    return "calls health-record.py but never with --fail"


def _daemon_cannot_be_retired(path, src):
    'A long-lived daemon with no request-driven exit, or no way to see its build.\n\n    Deliberately narrow. The site set is "a store script that spawns itself as a\n    persistent background process", which is spelled `--daemon` here; a script that\n    merely mentions daemons in prose is stripped out by _uncommented first. Two\n    properties are asserted because either one alone leaves the operator stuck: a\n    daemon you cannot ask to exit has to be pkill\'d, and a daemon whose build you\n    cannot read gives no reason to.'
    body = _uncommented(src)
    if not re.search(r"[\"']--daemon[\"']", body):
        return None
    missing = []
    
    
    
    if not (re.search(r"shutdown|SIGTERM|SIGHUP|terminate_request", body)
            and re.search(r"signal\.signal|/shutdown|\"shutdown\"|'shutdown'", body)):
        missing.append("no request-driven exit (a running instance can only be killed)")
    if "sha256" not in body and "code_fingerprint" not in body:
        missing.append("does not record which build it is running")
    return "; ".join(missing) if missing else None


def _set_e_pipeline(path, src):
    'A pipeline under `set -e` whose guard is missing.\n\n    JOINS LINE CONTINUATIONS FIRST, and that is not a detail. The first version\n    checked line by line and reported the shell agents-materialize:271 -- a pipeline whose\n    `|| true` sits on line 272 because the statement wraps, and whose ten-line comment\n    block directly above it explains that the guard is load-bearing and describes the\n    exact false DEGRADED banner its absence once caused. So the checker flagged, as a\n    violation, the very site that had already been fixed and documented. Second false\n    positive out of this file (see _uncommented); both were found by running it against\n    a store whose real state was known, which is the only way a checker earns trust.'
    if not re.search(r"set -[a-z]*e", src):
        return None
    code = _code_lines(src)
    bad = []
    for idx, (lineno, text) in enumerate(code):
        s = text.strip()
        
        
        
        
        if not re.match(r"^(ls|find|grep)\b[^|]*\|(?!\|)", s):
            continue
        if "|| true" in _statement_at(code, idx):
            continue
        bad.append(str(lineno))
    if not bad:
        return None
    return "unguarded pipeline at line(s) " + ", ".join(bad)


def _speaks_to_the_model(path):
    'Does this hook produce a verdict or a message the agent will act on?'
    if re.match(r"^(block|require|guard)-", os.path.basename(path)):
        return True
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return False
    return any(tok in src for tok in
               ("permissionDecision", "systemMessage", "additionalContext"))


def _hook_test_case_stems():
    'Hook stems with a case battery in hook-test-cases.py\'s CASES dict.\n\n    Consolidation Phase 3b-3h moved most hooks\' tests off a per-hook test-<name>.sh\n    wrapper and onto a CASES entry run by the shared hook-test-run.py --hook <name>\n    runner, so "has a test" can no longer be answered by finding a test-<name> FILE\n    alone -- the file for most hooks no longer exists on purpose. Read as text, not\n    imported: this module has no need to execute hook-test-cases.py\'s payloads to\n    learn what keys it defines.'
    src = _read(os.path.join(G, "scripts", "hook-test-cases.py"))
    return {os.path.splitext(k)[0] for k in re.findall(r'^    "([\w.-]+)":\s*\[', src, re.M)}


def _enforcer_without_test(path, src):
    if not _speaks_to_the_model(path):
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem in _hook_test_case_stems():
        return None
    tests = {os.path.basename(p) for p in _scripts("sh", "py")}
    if any(t.startswith("test-" + stem) for t in tests):
        return None
    return "no test-%s and no hook-test-cases.py CASES entry" % stem


def _embedded_python_in_single_quotes(path, src):
    "A MULTI-LINE `python3 -c '...'` program inside a shell script.\n\n    Any apostrophe in the embedded program -- in a comment, in prose, in an\n    English possessive -- closes the shell string early and the script dies with\n    `unexpected EOF while looking for matching )`. The error names a paren and a\n    line number far from the real cause, so it reads as a shell bug rather than\n    a typo in a comment.\n\n    Deliberately narrow: a genuine ONE-LINER (`python3 -c 'print(1)'`) is common,\n    low-risk and not flagged. The risk scales with the program, because a long\n    embedded block grows explanatory comments, and comments are where\n    apostrophes live. The threshold is 3 lines."
    findings = []
    lines = src.split("\n")
    for idx, line in enumerate(lines):
        
        
        
        
        
        
        
        if line.lstrip().startswith("#"):
            continue
        m = re.search(r"python3?\s+-c\s+'", line)
        if not m:
            continue
        
        
        depth_start = idx
        rest = line[m.end():]
        end = None
        if "'" in rest:
            end = idx
        else:
            for j in range(idx + 1, len(lines)):
                if "'" in lines[j]:
                    end = j
                    break
        if end is None:
            end = len(lines) - 1
        span = end - depth_start + 1
        if span >= 3:
            findings.append(
                "%d: a %d-line python program embedded in `python3 -c '...'` -- "
                "one apostrophe anywhere inside ends the shell string"
                % (depth_start + 1, span))
    return findings


def _stat_not_portable(path, src):
    '`stat -f` without a `stat -c` fallback, or an mtime that defaults to a number.\n\n    Judged on the whole logical statement, because the fallback is routinely written on\n    a continuation line -- reading one physical line at a time would report all four of\n    the sites that DO carry it.'
    findings = []
    lines = _code_lines(src)
    for idx, (lineno, code) in enumerate(lines):
        if "stat -f" not in code:
            continue
        
        
        
        
        
        lo = max(0, idx - 6)
        window = _statement_at(lines, idx) + "\n" + "\n".join(c for _, c in lines[lo:idx + 8])
        
        
        
        if not re.search(r"stat\s+-c|-c\s+['\"]?%Y", window):
            findings.append("%s: `stat -f` with no `stat -c` fallback (GNU hosts)" % lineno)
            continue
        
        
        
        
        
        
        
        
        
        
        
        
        if re.search(r"stat\s+-f[^|\n]*\|\|\s*stat\s+-c", window):
            findings.append("%s: `stat -f … || stat -c …` — the fallback is unreachable "
                            "on GNU, where `stat -f` exits 0 with filesystem text"
                            % lineno)
        elif re.search(r"if\s+stat\s+-f", window):
            findings.append("%s: dispatch probes with `stat -f`, which succeeds on GNU "
                            "too, so the BSD branch is taken on every Linux host"
                            % lineno)
    return "; ".join(findings) if findings else None


def _context_budget_site():
    return [os.path.join(G, "scripts", "context_budget.py")]


def _context_over_budget(path, src):
    'Run `context_budget.py --check`; its non-zero exit names each overrun.'
    try:
        res = subprocess.run([sys.executable, path, "--check"], capture_output=True,
                             text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return "could not run context_budget.py --check: %s" % exc
    if res.returncode == 0:
        return None
    return "; ".join(l.strip() for l in res.stdout.splitlines() if l.strip()) or (
        "context_budget.py --check exited %d: %s" % (res.returncode, res.stderr.strip()))


def _check_mode_cannot_fail(path, src):
    'A `--check` that has no way to say no is a permanently-green check.\n\n    The general shape, and the reason this is a registry entry rather than a one-file\n    fix: a validation mode that cannot exit non-zero is indistinguishable from a\n    validation mode that always passes. This store has the same rule for eval cases\n    (`eval-case-grounded`) and for health writers (`health-writer-fail-path`); a check\n    mode is the third site of one idea.'
    if "/scripts/" not in path.replace(os.sep, "/"):
        return None
    lines = _code_lines(src)
    code = "\n".join(c for _, c in lines)
    
    
    
    accepts = next((m for m in re.finditer(r'["\']--(?:check|verify)["\']\s*(?:in|==|,|\))', code)
                    if not re.search(r'rev-parse["\']\s*,\s*$', code[max(0, m.start() - 40):m.start()])),
                   None) or re.search(r'--(?:check|verify)\s*\)', code)          
    if not accepts:
        return None
    fails = re.search(r'sys\.exit\(\s*[1-9]|return\s+[1-9]|\bexit\s+[1-9]', code)
    if fails:
        return None
    return ("accepts --check/--verify but has no non-zero exit anywhere, so the mode "
            "cannot report a failure")


def _server_source(name):
    "One file of the store's own server, the one tree the site helpers skip."
    return [os.path.join(STORE, "server", "src", "agent_context", name)]


def _chezmoi_sources():
    "The dotfiles source's provisioning scripts and shipped executables, when the\n    machine running this check has a chezmoi source at all."
    root = _chezmoi_root()
    if not os.path.isdir(root):
        return []
    out = glob.glob(os.path.join(root, "run_*")) + glob.glob(os.path.join(root, "dot_local", "bin", "*"))
    return sorted(p for p in out if os.path.isfile(p))


def _chezmoi_root():
    'The chezmoi source dir. AGENT_CONTEXT_CHEZMOI_SOURCE points a test at a fixture;\n    unset, it is the default path chezmoi itself uses.'
    return os.environ.get("AGENT_CONTEXT_CHEZMOI_SOURCE") or os.path.expanduser("~/.local/share/chezmoi")


def _chezmoi_bin():
    'The executables chezmoi installs into ~/.local/bin; run_* provisioning scripts excluded.'
    return [p for p in _chezmoi_sources() if os.path.basename(os.path.dirname(p)) == "bin"]


def _in_chezmoi(path):
    return os.path.realpath(path).startswith(os.path.realpath(_chezmoi_root()) + os.sep)


def _chezmoi_name(path):
    'The name chezmoi installs a source file under: attribute prefixes and .tmpl dropped.'
    base = os.path.basename(path)
    if base.endswith(".tmpl"):
        base = base[: -len(".tmpl")]
    
    prefixes = ("executable_", "private_", "readonly_", "empty_", "encrypted_")
    stripped = True
    while stripped:
        stripped = False
        for prefix in prefixes:
            if base.startswith(prefix):
                base = base[len(prefix):]
                stripped = True
    return base


def _shebang(src):
    first = src.split("\n", 1)[0]
    return first if first.startswith("#!") else ""







_LAUNCHERS = (
    "agent-notify-watch", "token-usage-collect", "derived-data-prune",
    "intellij-server-refresh", "git-ssh-op", "sign-with-op-personal",
    "sign-with-op-work", "csharp-ls", "pi", "agent-context-refresh",
)
_LAUNCHER_MAX_CODE_LINES = 25
_SHELL_SHEBANG = re.compile(r"^#!.*\b(?:ba)?sh\b")





_PYTHON_SHEBANG_INCLUDE = '{{ template "python-shebang" . }}'
_LITERAL_PYTHON_SHEBANG = re.compile(r"^#!.*\bpython")


def _python_bin_shebang_not_rendered(path, src):
    first = src.split("\n", 1)[0].strip()
    if first == _PYTHON_SHEBANG_INCLUDE:
        if path.endswith(".tmpl"):
            return None
        return "the shebang include renders only in a .tmpl source: add the .tmpl suffix"
    if _LITERAL_PYTHON_SHEBANG.match(first):
        return "literal python shebang %r: make line 1 %s" % (first, _PYTHON_SHEBANG_INCLUDE)
    return None


def _tagged(reason, names):
    return {name: reason for name in names.split()}





_SHELL_ALLOW = {}






def _shell_sites():
    return (_hooks("sh") + _scripts("sh") + _project_hooks("sh") + _project_scripts("sh")
            + [p for p in _chezmoi_bin() if _SHELL_SHEBANG.match(_shebang(_read(p)))])


_PROJECT_LAUNCHER_MAX_CODE_LINES = 3
_PROJECT_LAUNCHER_CONTROL_FLOW = re.compile(
    r"\b(?:if|then|elif|else|fi|case|esac|for|while|until|do|done)\b"
    r"|&&|\|\||\$\(|`|\)\s*\{")



_PROJECT_LAUNCHER_PRELUDE = re.compile(
    r"(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|[^\s\"';&|<>]*)"
    r"|export(?:\s+[A-Za-z_][A-Za-z0-9_]*)+")


def _project_launcher_violation(src):
    ' project launcher violation.'
    if _shebang(src) != "#!/bin/sh":
        return "launcher shebang must be exactly #!/bin/sh"
    code = [line.strip() for line in src.splitlines()
            if line.strip() and not line.strip().startswith("#")]
    if len(code) > _PROJECT_LAUNCHER_MAX_CODE_LINES:
        return ("launcher has %d code lines, over %d: it holds logic"
                % (len(code), _PROJECT_LAUNCHER_MAX_CODE_LINES))
    if any(_PROJECT_LAUNCHER_CONTROL_FLOW.search(line) for line in code):
        return "launcher has control flow or a command substitution: it holds logic"
    if not code or not code[-1].startswith("exec "):
        return "launcher's last code line does not start with exec"
    if any(not _PROJECT_LAUNCHER_PRELUDE.fullmatch(line) for line in code[:-1]):
        return "launcher runs a command before exec: only assignments and export may precede it"
    return None


def _shell_is_not_a_launcher(path, src):
    if _project_of(path):
        return _project_launcher_violation(src)
    if not _in_chezmoi(path) or _chezmoi_name(path) not in _LAUNCHERS:
        return "shell file holds logic: port it to Python"
    if re.search(r"\bbash\b", _shebang(src)):
        return "launcher runs under bash: launchers are POSIX sh"
    n = sum(1 for line in src.splitlines() if line.strip() and not line.strip().startswith("#"))
    if n > _LAUNCHER_MAX_CODE_LINES:
        return "launcher has %d code lines, over %d: it holds logic" % (n, _LAUNCHER_MAX_CODE_LINES)
    return None




_RETIRED_NO_PORT = ("file-stat", "global-settings-json")
_SH_NAME = re.compile(r"(?<![\w.-])([a-z0-9][a-z0-9-]*)\.sh\b")
_RETIRED_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", hp.CLAUDE_DIRNAME, hp.AGENTS_DIRNAME,
                      "audit-observations", "audit-observations-archive", "docs", "handoffs"}


def _retired_in_scope(stem, dirs, no_port_names):
    'None if stem.sh is live in these dirs; a string if retired here; False if this\n    scope has no file under this stem at all, so a wider scope may still know it.'
    if any(os.path.exists(os.path.join(d, stem + ".sh")) for d in dirs):
        return None
    for d in dirs:
        if os.path.exists(os.path.join(d, stem + ".py")):
            return "%s/%s.py" % (os.path.basename(d), stem)
    return "no port" if stem in no_port_names else False


def _retired_script(stem, project=None):
    "Where a retired script went ('scripts/x.py', 'no port'), or None if live.\n\n    project scopes the search to that project's own scripts/hooks first -- stems\n    repeat across projects (wt-finish x5, wt-sweep x3) and each retires on its own\n    schedule, so a project's own live file must never be read against an unrelated\n    same-stem retirement elsewhere (the real wt-sweep shell shims are live markers,\n    not retired, even though global/scripts/wt-sweep.py exists under the same stem).\n    Only a name the project has NO file for at all falls through to the global scope,\n    which is how a project file naming a genuinely global retired helper (file-stat,\n    retired without a port) still gets caught."
    if project:
        local = _retired_in_scope(
            stem, (os.path.join(STORE, "projects", project, "scripts"),
                   os.path.join(STORE, "projects", project, "hooks")), ())
        if local is not False:
            return local
    global_dirs = (os.path.join(G, "scripts"), os.path.join(G, "hooks"))
    result = _retired_in_scope(stem, global_dirs, _RETIRED_NO_PORT)
    return None if result is False else result


def _retired_ref_sites():
    store = os.path.dirname(G)
    out = []
    for root in (os.path.join(G, "hooks"), os.path.join(G, "scripts"),
                 os.path.join(store, "server", "src"), os.path.join(store, "server", "tests"),
                 os.path.join(store, "templates")):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _RETIRED_SKIP_DIRS]
            out += [os.path.join(dirpath, f) for f in filenames
                    if not f.endswith((".md", ".pyc"))]
    out += glob.glob(os.path.join(store, "projects", "*", "scripts", "*"))
    out += glob.glob(os.path.join(store, "projects", "*", "hooks", "*"))
    out += glob.glob(os.path.join(G, "*.json"))
    return sorted(p for p in out if os.path.isfile(p)) + _chezmoi_bin()


def _names_retired_script(path, src):
    project = _project_of(path)
    hits = []
    for stem in sorted(set(_SH_NAME.findall(src))):
        where = _retired_script(stem, project)
        if where:
            hits.append("%s.sh (%s)" % (stem, "retired with no port" if where == "no port"
                                        else "now " + where))
    return ("names retired " + ", ".join(hits)) if hits else None


_NPX = re.compile(r"(?<![\w-])npx(?![\w-])")
_NPM_GLOBAL = re.compile(r"\bnpm\s+(?:install|i|add)\b[^\n;|&]*\s(?:-g|--global)\b"
                         r"|\bnpm\s+(?:-g|--global)\s+(?:install|i|add)\b")




_NODE_SELF = ("invariant-check.py", "test-invariant-consolidation.py",
              "test-invariant-node-tools.py", "test-node-tools-sync.py",
              
              "test-invariant-node-tools-edges.py",
              
              
              "test-user-pnpm.py")
_NODE_ALLOW = {}




_PNPM_BOOTSTRAP_FILE = "run_onchange_after_install-packages"
_PNPM_BOOTSTRAP = re.compile(
    r"^[ \t]*(?:sudo[ \t]+(?:-\S+[ \t]+)*)?(?:env[ \t]+(?:\S+=\S*[ \t]+)*)?(?:\S*/)?npm[ \t]+"
    r"(?:install|i|add)[ \t]+(?:-g|--global)[ \t]+pnpm(?:@latest)?(?=[ \t]*(?:$|\|\||&&|;|#))",
    re.M)


def _node_sites():
    
    
    out = [p for p in _scripts("sh", "py", "ts") + _hooks("sh", "py")
           + _project_scripts("sh", "py", "ts") + _project_hooks("sh", "py")
           if os.path.basename(p) not in _NODE_SELF]
    for extra in (os.path.join(G, "mcp-servers.json"), os.path.join(G, "node-tools", "package.json"),
                  os.path.join(G, "node-tools", "pnpm-workspace.yaml")):
        if os.path.isfile(extra):
            out.append(extra)
    
    
    return out + _chezmoi_sources()


def _floating_tools(src):
    ' floating tools.'
    try:
        data = json.loads(src)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    bad = []
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        section = data.get(field)
        if not isinstance(section, dict):
            continue
        for name, spec in sorted(section.items()):
            if spec != "latest":
                bad.append("%s %s is %r, not latest" % (field, name, spec))
    
    
    pnpm_section = data.get("pnpm")
    for field, section in (("overrides", data.get("overrides")), ("resolutions", data.get("resolutions")),
                           ("pnpm.overrides", pnpm_section.get("overrides") if isinstance(pnpm_section, dict) else None)):
        if section:
            bad.append("%s is set, which pins what the tools resolve to" % field)
    return "; ".join(bad) or None


_WORKSPACE_PIN = re.compile(r"^(overrides|catalogs?)[ \t]*:", re.M)


def _pinning_workspace(src):
    'pnpm-workspace.yaml: `overrides` and catalogs pin versions, so the tools manifest uses neither.'
    found = sorted(set(_WORKSPACE_PIN.findall(src)))
    return ("sets %s, which pins what the tools resolve to" % ", ".join(found)) if found else None


def _runs_unpinned_node(path, src):
    if os.path.basename(path) == "package.json":
        return _floating_tools(src)
    if os.path.basename(path) == "pnpm-workspace.yaml":
        return _pinning_workspace(src)
    if path.endswith(".json"):
        
        
        try:
            data = json.loads(src)
        except ValueError:
            return None
        servers = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            return None
        bad = []
        for name, spec in sorted(servers.items()):
            if not isinstance(spec, dict):
                continue
            args = spec.get("args")
            args = args if isinstance(args, list) else []
            if os.path.basename(str(spec.get("command", ""))) == "npx":
                bad.append("%s runs through npx" % name)
            elif any(str(a).endswith("@latest") for a in args):
                bad.append("%s fetches @latest" % name)
        return "; ".join(bad) or None
    
    code = re.sub(r"\\\n\s*", " ", _uncommented(src))
    if os.path.basename(path).startswith(_PNPM_BOOTSTRAP_FILE):
        code = _PNPM_BOOTSTRAP.sub("", code)
    if _NPX.search(code):
        return "runs npx"
    if _NPM_GLOBAL.search(code):
        return "installs a global npm package"
    return None














_FLEET_ROW_FIELDS = {
    "machine_id": "static", "machine_uuid": "static", "hostname": "static",
    "build": "static", "code_version": "static", "server_commit": "static",
    "code_current": "state", "verdict": "state",
    "code_stale_since": "unhealthy", "code_defer_reason": "unhealthy",
    "sync_reason": "unhealthy",
    "adoption": "state",           
    "deps": "state",               
    "updated_at": "bucketed",
}
_FLEET_ADOPTION_FIELDS = {
    "projected_at": "bucketed", "projected_commit": "masked", "stale_daemons": "state",
}


def _dict_literal_after(src, anchor):
    'The body of the first `{ ... }` dict literal after `anchor`, closed at the\n    4-space-indented `}` that ends a function-level statement. None if absent.'
    at = src.find(anchor)
    if at < 0:
        return None
    start = src.find("{", at)
    end = src.find("\n    }", start)
    return src[start + 1:end] if start >= 0 and end >= 0 else None


def _deps_manifest_sites():
    path = os.path.join(G, "deps", "manifest.toml")
    return [path] if os.path.isfile(path) else []


def _deps_manifest_invalid(path, src):
    "Problems in the fleet dependency manifest, using deps-check.py's own validator."
    import importlib.util
    checker = os.path.join(G, "scripts", "deps-check.py")
    if not os.path.isfile(checker):
        return None
    spec = importlib.util.spec_from_file_location("deps_check_for_invariant", checker)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if mod.tomllib is None:
        return None
    try:
        errors = mod.validate(mod.tomllib.loads(src))
    except mod.tomllib.TOMLDecodeError as ex:
        return "not valid TOML (%s)" % ex
    return "; ".join(errors) or None


def _chezmoi_config_template():
    return os.path.join(_chezmoi_root(), ".chezmoi.toml.tmpl")


def _chezmoi_package_data():
    ' chezmoi package data.'
    return os.path.join(_chezmoi_root(), ".chezmoidata", "packages.toml")


def _package_data_names(tomllib):
    'Every string in every list under [packages] of .chezmoidata/packages.toml, at any\n    depth (universal, per-OS and per-machine lists alike); empty when the file is absent.'
    try:
        with open(_chezmoi_package_data(), "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    names, stack = set(), [data.get("packages")]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            names.update(v for v in value if isinstance(v, str))
    return names


def _deps_parity_sites():
    manifest = os.path.join(G, "deps", "manifest.toml")
    if os.path.isfile(manifest) and (os.path.isfile(_chezmoi_config_template())
                                     or os.path.isfile(_chezmoi_package_data())):
        return [manifest]
    return []


_WANT_LSP = re.compile(r'^[ \t]*_want_lsp[ \t]+("[^"]+"|\S+)', re.M)
_COMMAND_V = re.compile(r'^[ \t]*(?:if|elif)\b[^\n#]*?command -v[ \t]+"?([A-Za-z0-9][\w.-]*)', re.M)
_PACKAGE_LIST = re.compile(r"(?:universal|os_specific)_packages[ \t]*=[ \t]*\[(.*?)\]", re.S)
_TEMPLATE_ACTION = re.compile(r"\{\{.*?\}\}", re.S)


def _template_text(text):
    '`text` without template actions and `#` comments, so a name that a template expression or a\n    comment mentions is not read as a list entry.'
    text = _TEMPLATE_ACTION.sub("", text)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _deps_chezmoi_parity(path, src):
    "Where the dependency manifest and chezmoi's install side disagree, one string, or None.\n\n    The manifest names what a machine needs (global/deps/manifest.toml); chezmoi's package lists,\n    the language servers its scripts verify and global/node-tools/package.json name what gets\n    installed. Every install-side name is a manifest tool (its `package` or `node_package`) or an\n    [unmanaged] entry with a reason, and every tool that names a package finds it there. Nothing in\n    chezmoi is rendered or changed by this check. An invalid manifest is left to deps-manifest-valid."
    checker = os.path.join(G, "scripts", "deps-check.py")
    if not os.path.isfile(checker):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("deps_check_for_parity", checker)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if mod.tomllib is None:
        return None
    try:
        manifest = mod.tomllib.loads(src)
    except mod.tomllib.TOMLDecodeError:
        return None
    if mod.validate(manifest):
        return None
    tools = manifest["tool"]
    unmanaged = manifest.get("unmanaged", {})
    packages = {t["package"]: n for n, t in tools.items() if "package" in t}
    node_names = {t["node_package"]: n for n, t in tools.items() if "node_package" in t}

    root = _chezmoi_root()
    listed = set()
    for block in _PACKAGE_LIST.findall(_template_text(_read(_chezmoi_config_template()) or "")):
        listed.update(re.findall(r"""["']([^"']+)["']""", block))
    listed |= _package_data_names(mod.tomllib)
    lsp = set()
    problems = []
    for name in ("run_onchange_after_install-packages.sh.tmpl", "run_onchange_after_install-lsp-toolchain.sh.tmpl"):
        script = os.path.join(root, name)
        if not os.path.isfile(script):
            problems.append("chezmoi script %s is missing, so its language server checks are not compared" % name)
            continue
        text = _read(script) or ""
        lsp.update(os.path.basename(m.strip('"')) for m in _WANT_LSP.findall(text))
        if name.endswith("lsp-toolchain.sh.tmpl"):
            lsp.update(_COMMAND_V.findall(text))
    try:
        with open(os.path.join(G, "node-tools", "package.json"), encoding="utf-8") as fh:
            node_deps = set((json.load(fh).get("dependencies") or {}))
    except (OSError, ValueError):
        node_deps = set()

    for name in sorted(listed):
        if name not in packages and name not in unmanaged:
            problems.append("chezmoi package %s is neither a manifest tool's `package` nor [unmanaged]" % name)
    for name, tool in sorted(packages.items()):
        if name not in listed:
            problems.append("manifest tool %s names package %s that no chezmoi list carries" % (tool, name))
    for name in sorted(lsp):
        if name not in tools and name not in unmanaged:
            problems.append("language server %s (verified by chezmoi) is neither a manifest tool nor [unmanaged]" % name)
    for name in sorted(node_deps):
        if name not in node_names and name not in unmanaged:
            problems.append("node-tools dependency %s is neither a manifest tool's `node_package` nor [unmanaged]" % name)
    for name, tool in sorted(node_names.items()):
        if name not in node_deps:
            problems.append("manifest tool %s names node_package %s that global/node-tools does not carry" % (tool, name))
    declared = listed | lsp | node_deps
    for name in sorted(unmanaged):
        if name not in declared:
            problems.append("[unmanaged] %s names nothing chezmoi or node-tools declares" % name)
        elif name in packages or name in node_names:
            problems.append("[unmanaged] %s is also a manifest tool's package" % name)
    return "; ".join(problems) or None


def _uvicorn_config_without_log_config(path, src):
    'A uvicorn.Config(...) call that does not pass log_config=.'
    findings = []
    for m in re.finditer(r"uvicorn\.Config\(", src):
        depth, i = 1, m.end()
        while i < len(src) and depth:
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        if "log_config=" not in src[m.end():i]:
            lineno = src.count("\n", 0, m.start()) + 1
            findings.append("line %d builds uvicorn.Config without log_config=" % lineno)
    return "; ".join(findings) if findings else None


def _fleet_row_fields_unaccounted(path, src):
    'A key in the published fleet row that this file has not classified.'
    findings = []
    vol_at = src.find("_VOLATILE_FIELDS = (")
    volatile = src[vol_at:src.find("\n\n", vol_at)] if vol_at >= 0 else ""
    checks = (
        ("publish payload", _dict_literal_after(src, "\n    payload = {"),
         _FLEET_ROW_FIELDS),
        ("adoption payload",
         _dict_literal_after(src[src.find("def _adoption_payload("):], "return {"),
         _FLEET_ADOPTION_FIELDS),
    )
    for label, body, registry in checks:
        if body is None:
            findings.append("could not find the %s literal in fleet.py; the check is "
                            "blind and must be re-anchored" % label)
            continue
        keys = re.findall(r'^\s{8}"(\w+)":', body, re.M)
        for k in keys:
            tag = registry.get(k)
            if tag is None:
                findings.append("%s key `%s` has no churn class in invariant-check's "
                                "registry" % (label, k))
                continue
            
            m = re.search(r'^\s{8}"%s":(.*?)(?=^\s{8}"\w+":|\Z)' % k, body, re.M | re.S)
            text = m.group(1) if m else ""
            if tag == "bucketed" and "_BUCKET_SECS" not in text:
                findings.append("%s key `%s` is classed bucketed but its value does not "
                                "use _BUCKET_SECS" % (label, k))
            if tag == "masked" and ('"%s"' % k) not in volatile:
                findings.append("%s key `%s` is classed masked but fleet._VOLATILE_FIELDS "
                                "does not name it" % (label, k))
        for k in registry:
            if k not in keys:
                findings.append("registry names %s key `%s` that fleet.py no longer "
                                "publishes; drop it from the registry" % (label, k))
    return "; ".join(findings) if findings else None


CONFLICT = re.compile(r"^(<{7}|={7}|>{7})(\s|$)", re.M)







_CARVE_NEGATED = re.compile(
    r"(no|not|without|never)\s+(an?\s+)?(exception|carve-?out|caveat)s?", re.I)
_CARVE = re.compile(
    r"(^|[^a-z])(the )?(one |single |only )?(exception|carve-?out|caveat)([^a-z]|$)"
    r"|except that|does not apply (to|when)")


_DESC_DECLARES = re.compile(r"exempt|exception|carve-?out|caveat|\bexcept\b|\bunless\b",
                            re.I)

_MD_TABLE = re.compile(r"^\|.*\|[ \t]*$\n^\|[\s:|-]+\|[ \t]*$", re.M)


def _memory_description_hides_a_carve_out(path, src):
    if not src.startswith("---\n") or "\n---\n" not in src:
        return None
    fm, body = src[4:].split("\n---\n", 1)
    meta = dict(re.findall(r'^(\w+): "?(.*?)"?$', fm, re.M))
    
    
    if meta.get("load_behavior") != "always":
        return None
    desc = meta.get("description", "")
    
    
    
    
    
    if not desc or "→ body" in desc or _DESC_DECLARES.search(desc):
        return None
    hit = _CARVE.search(_CARVE_NEGATED.sub(" ", body.lower()))
    what = "a carve-out (%r)" % hit.group(0).strip()[:40] if hit else None
    
    
    
    
    if what is None and _MD_TABLE.search(body):
        what = "a table, which a 140-char description cannot hold"
    if what is None:
        return None
    return ("the body carries %s and the description does not say so; the description "
            "is all that loads into a session" % what)


def _conflict_markers_committed(path, src):
    'Git conflict markers sitting INSIDE a store entity.\n\n    That residual matters more than it sounds. The entity keeps loading; it is the\n    instruction text itself that is corrupt, so every session on every machine reads a\n    file with three conflicting versions spliced together and nothing announces it. The\n    one place this was seen, three deletions had also been silently reverted.\n\n    Checked across every entity bucket, not just instructions: a doc or memory carrying\n    markers is the same failure with a quieter blast radius.'
    if not re.search(r"/(hooks|scripts|agents|commands|skills|docs|memory|instructions)/",
                     path.replace(os.sep, "/")):
        return None
    hits = [str(i + 1) for i, line in enumerate(src.splitlines())
            if CONFLICT.match(line)]
    if not hits:
        return None
    return ("git conflict markers at line(s) %s — this entity is corrupt and still "
            "loading" % ", ".join(hits[:6]))


def _worker_missing_uninheritable_rule(path, src):
    'Rules a worker CANNOT inherit must be restated in its definition.\n\n    A subagent gets none of the Global Agent Instructions. Two rules there are both\n    load-bearing and unenforceable by any hook a worker runs under, so each definition\n    has to carry them itself:'
    
    
    
    p = path.replace(os.sep, "/")
    if p.endswith("/docs/worker-shared-rules.md"):
        gaps = []
        if not re.search(r"stash|reset --hard|checkout --|destructive", src, re.I):
            gaps.append("the destructive-git prohibition")
        if "allow rule" not in src:
            gaps.append("the Grep/Glob gate (policy)")
        if not re.search(r"self-timeout|\breap(s|ed|ing)?\b", src, re.I):
            gaps.append("background-process reaping (policy)")
        if "policy" not in src or "still running" not in src:
            gaps.append("reporting only after owned background work ends (policy)")
        if "SendMessage" not in src:
            gaps.append("what to do with a distrusted mid-task message (policy)")
        for hook in ("block-shell-file-read", "block-redundant-read"):
            if hook not in src:
                gaps.append("the %s rule" % hook)
        if not gaps:
            return None
        return "does not state %s, and a subagent inherits no global instructions" % (
            " or ".join(gaps))
    if "/agents/" not in p:
        return None
    if 'get_doc("worker-shared-rules.md")' in src:
        return None
    return ('does not open with the first-call pointer to get_doc("worker-shared-rules.md"), '
            "and a subagent inherits no global instructions")


_BACKTICK_PATH = re.compile(r"`([^`\s]*[/~$][^`\s]*)`")


def _worker_guardrails_drift(path, src):
    "A worker's Filesystem safety paragraph names only paths AGENTS.md names."
    if "/agents/" not in path.replace(os.sep, "/"):
        return None
    para = re.search(r"\*\*Filesystem safety\.\*\*(.*?)(?:\n\s*\n|\Z)", src, re.S)
    if not para:
        return None
    try:
        with open(os.path.join(STORE, "AGENTS.md"), encoding="utf-8") as fh:
            agents = fh.read()
    except OSError:
        return None
    section = re.search(r"^### Filesystem safety\n(.*?)(?=^### |\Z)", agents, re.S | re.M)
    if not section:
        return "AGENTS.md has no '### Filesystem safety' section to compare against"
    extra = sorted(set(_BACKTICK_PATH.findall(para.group(1)))
                   - set(_BACKTICK_PATH.findall(section.group(1))))
    if not extra:
        return None
    return ("Filesystem safety names %s, which the AGENTS.md section does not"
            % ", ".join("`%s`" % p for p in extra))









_WORKER_SHARED_PARAGRAPHS = (
    
    ("**First call: `get_doc(\"worker-shared-rules.md\")`**", "decision A", {}),
    ("**No filler between tool calls.", "worker narration rule", {}),
)


def _shared_paragraph_drift(sources):
    '{agent basename: [stem, ...]} for every shared paragraph that departs from the\n    most common wording. sources: {agent basename: full text}.'
    def paragraphs(text):
        return [re.sub(r"\s+", " ", p.strip()) for p in re.split(r"\n\s*\n", text)]

    drift = {}
    for stem, _source, exceptions in _WORKER_SHARED_PARAGRAPHS:
        mine = {}
        for name, text in sorted(sources.items()):
            if name in exceptions:
                continue
            hit = next((p for p in paragraphs(text) if p.startswith(stem)), None)
            if hit is not None:
                mine[name] = hit
        if len(mine) < 2:
            continue
        canonical = collections.Counter(mine.values()).most_common(1)[0][0]
        for name, para in mine.items():
            if para != canonical:
                drift.setdefault(name, []).append(stem)
    return drift


def _worker_shared_text_drift(path, src):
    ' worker shared text drift.'
    if "/agents/" not in path.replace(os.sep, "/"):
        return None
    sources = {os.path.basename(p): _read(p) for p in _agents()}
    stems = _shared_paragraph_drift(sources).get(os.path.basename(path))
    if not stems:
        return None
    return "differs from the other workers in: %s" % "; ".join(stems)


def _worker_shared_text_negative_control():
    "Prove the check can fail: alter one agent's copy of a shared paragraph and\n    require it to be flagged. Returns a message on failure, else None. The stem comes\n    from the live registry: a stem the registry no longer lists is never checked, so a\n    control built on one can only fail."
    stem = _WORKER_SHARED_PARAGRAPHS[0][0]
    text = "intro\n\n%s The rule text every worker carries.\n" % stem
    same = _shared_paragraph_drift({"a.md": text, "b.md": text, "c.md": text})
    if same:
        return "identical copies were flagged: %s" % same
    edited = text.replace("every worker", "one worker")
    flagged = _shared_paragraph_drift({"a.md": text, "b.md": text, "c.md": edited})
    if flagged != {"c.md": [stem]}:
        return "an edited copy was not flagged as expected: %s" % flagged
    return None


def _agent_envelope_unstated(path, src):
    "A worker's LIMITS must be visible in the line a dispatcher reads.\n\n    The fix was not a hook, and could not be: it is that each definition states what it\n    cannot do. This asserts the fix stayed applied, which is exactly the half-applied\n    failure the registry exists for -- a sixth worker added next month would silently\n    reintroduce it."
    if "/agents/" not in path.replace(os.sep, "/"):
        return None
    m = re.search(r'^tools:\s*"(.*)"\s*$', src, re.M)
    d = re.search(r'^description:\s*"(.*)"\s*$', src, re.M)
    if not m or not d:
        return None
    tools = {t.strip() for t in m.group(1).split(",")}
    missing = [name for name in ("Bash", "Edit", "Write") if name not in tools]
    if not missing:
        return None
    
    
    if re.search(r"no shell|cannot run|can(?:not| ?'?t) edit|read-only|does not edit|"
                 r"denied|no source-edit|cannot spawn|observes and reports|"
                 r"it cannot|no edit", d.group(1), re.I):
        return None
    return ("grants neither %s, and the description does not say so — a dispatcher "
            "picks from descriptions, so the limit is invisible when it matters"
            % " nor ".join(missing))


def _wired_to_a_different_event(path, src):
    "The hook's declared event and the event it is WIRED to must agree.\n\n    hook-registration-probe already asserts a hook IS registered. It does not ask\n    under WHICH event, and that is exactly the gap this closes."
    name = os.path.basename(path)
    meta = os.path.splitext(path)[0] + ".meta.toml"
    if not os.path.exists(meta):
        meta = path + ".meta.toml"
    try:
        with open(meta, encoding="utf-8", errors="replace") as fh:
            declared = re.search(r'event_type\s*=\s*"([A-Za-z]+)"', fh.read())
    except OSError:
        return None
    if not declared:
        return None
    sync = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "home-settings-sync.py")
    try:
        with open(sync, encoding="utf-8", errors="replace") as fh:
            lines = _code_lines(fh.read())
    except OSError:
        return None
    event, wired = None, set()
    for _, code in lines:
        m = re.search(r'\(\s*"([A-Za-z]+)"\s*,\s*(?:None|"[^"]*")\s*,\s*\[', code)
        if m:
            event = m.group(1)
        if name in code and event:
            wired.add(event)
    if not wired or declared.group(1) in wired:
        return None
    return "declares %s but home-settings-sync wires it under %s" % (
        declared.group(1), ", ".join(sorted(wired)))


_FINGERPRINT_CACHE = {}


def _fingerprint_probe():
    'hook-registration-probe loaded by path (its name has a hyphen), once.'
    if "probe" not in _FINGERPRINT_CACHE:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "hook-registration-probe.py")
        spec = importlib.util.spec_from_file_location("hook_registration_probe", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _FINGERPRINT_CACHE["probe"] = mod
        _FINGERPRINT_CACHE["wired"] = mod.wiring_by_hook()
        _FINGERPRINT_CACHE["manifest"] = mod.load_fingerprints(
            os.path.join(G, "hook-fingerprints.json"))
    return _FINGERPRINT_CACHE["probe"]


def _hook_fingerprint_drift(path, src):
    "A hook's script bytes and wiring must match global/hook-fingerprints.json.\n\n    ECC adoption chunk 5. A guard can be edited, or its matcher changed in\n    home-settings-sync, and every reader-facing surface still reads correct. The\n    manifest turns that into a deliberate step: the edit fails this check until\n    `hook-registration-probe.py --update-fingerprints` is run, and that command\n    refuses while the registration check has failures. The manifest file itself is a\n    site, so an entry for a hook that no longer exists is reported too."
    probe = _fingerprint_probe()
    manifest = _FINGERPRINT_CACHE["manifest"]
    if os.path.basename(path) == "hook-fingerprints.json":
        hooks = set(os.listdir(os.path.dirname(path) + "/hooks"))
        gone = sorted(k for k in (manifest or {}) if k not in hooks)
        return ("lists %s, which is not in global/hooks" % ", ".join(gone)) if gone else None
    if manifest is None:
        return "global/hook-fingerprints.json is missing or unreadable"
    name = os.path.basename(path)
    if name not in manifest:
        return "has no fingerprint in global/hook-fingerprints.json"
    with open(path, "rb") as fh:
        now = probe.fingerprint(fh.read(), _FINGERPRINT_CACHE["wired"].get(name, []))
    if now != manifest[name]:
        return "script or wiring changed since its fingerprint was recorded"
    return None


def _fingerprint_negative_control():
    'Prove the check can fail: an edited body and a changed matcher must both change\n    the fingerprint, and identical inputs must not. Returns a message on failure.'
    probe = _fingerprint_probe()
    body, wiring = b"#!/bin/sh\nexit 0\n", ["PreToolUse:Bash"]
    base = probe.fingerprint(body, wiring)
    if probe.fingerprint(body, list(wiring)) != base:
        return "identical inputs produced different fingerprints"
    if probe.fingerprint(body + b"# edit\n", wiring) == base:
        return "an edited script body did not change the fingerprint"
    if probe.fingerprint(body, ["PreToolUse:Write"]) == base:
        return "a changed matcher did not change the fingerprint"
    if probe.fingerprint(body, []) == base:
        return "an unwired hook fingerprinted the same as a wired one"
    return None


def _standalone_relay_on_the_store_host():
    "The standalone relay's path, when this host holds the store's server code and has\n    one. A link whose target is gone still counts: the harness configs test the same path."
    relay = os.path.join(hp.home(), ".local", "bin", "agent-context")
    if os.path.isdir(os.path.join(STORE, "server", "src")) and os.path.lexists(relay):
        return [relay]
    return []


def _hook_fingerprint_sites():
    manifest = os.path.join(G, "hook-fingerprints.json")
    return _hooks("sh", "py") + ([manifest] if os.path.exists(manifest) else [])


def _lock_record_worktree_roots():
    ' lock record worktree roots.'
    roots = []
    base = os.path.join(STORE, hp.AGENTS_DIRNAME, "worktrees")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return roots
    for name in names:
        root = os.path.join(base, name)
        if os.path.isdir(root):
            roots.append(root)
    return roots


def _lock_record_sites():
    'Every lock record: global/test-locks/*.json in the main checkout, and the same\n    directory in each of its worktrees (L1).'
    out = []
    for root in [STORE] + _lock_record_worktree_roots():
        out += sorted(glob.glob(os.path.join(root, "global", "test-locks", "*.json")))
    return out


def _lock_record_hash_mismatch(path, src):
    "Which checkout a record belongs to is read off the record's OWN path -- one of the\n    worktree roots _lock_record_worktree_roots() lists, or the store root otherwise --\n    never the other checkout, so a worktree's legitimately different copy of the same\n    file is judged against its own record, not main's."
    try:
        doc = json.loads(src)
    except ValueError:
        return "is not valid JSON"
    if not (isinstance(doc, dict) and isinstance(doc.get("path"), str)
            and isinstance(doc.get("sha256"), str)):
        return "is missing path or sha256"
    rel = doc["path"]
    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        return "path %r escapes the checkout" % rel
    root = next((r for r in _lock_record_worktree_roots()
                if path.startswith(r + os.sep + "global" + os.sep + "test-locks" + os.sep)),
                STORE)
    target = os.path.join(root, rel)
    try:
        digest = hashlib.sha256()
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        now = digest.hexdigest()
    except OSError:
        return "names %s, which does not exist in %s" % (rel, root)
    if now != doc["sha256"]:
        return ("names %s in %s, whose content no longer matches this record (locked %s)"
                % (rel, root, doc.get("locked_at") or "at an unknown time"))
    return None


def _stop_hook_talks_to_nobody(path, src):
    'A Stop hook whose only output is `systemMessage` is advising the terminal.\n\n    That matters because every advisory Stop hook here was written on the opposite\n    assumption and said so in its own header. A hook that warns the human is a fine\n    thing to build ON PURPOSE; one that believes it is correcting the next turn and\n    is not, is a guardrail that reads as installed and enforces nothing.'
    if "/hooks/" not in path.replace(os.sep, "/"):
        return None
    if "systemMessage" not in src:
        return None
    
    
    meta = os.path.splitext(path)[0] + ".meta.toml"
    if not os.path.exists(meta):
        meta = path + ".meta.toml"
    try:
        with open(meta, encoding="utf-8", errors="replace") as fh:
            sidecar = fh.read()
    except OSError:
        return None
    if not re.search(r'event_type\s*=\s*"Stop"', sidecar):
        return None
    
    
    
    if re.search(r'"decision"\s*:\s*"block"|decision.{0,12}block', src):
        return None
    return ("Stop hook advises via systemMessage only, which does not enter model "
            "context; re-home to UserPromptSubmit + additionalContext, or say in the "
            "header that the audience is the human")


def _eval_cases_ungrounded(path, src):
    'Every eval case must name the real failure it encodes, and must be scored.\n\n    The quality system has to police itself or it becomes the next thing that rots. An\n    ungrounded case measures an imagined agent and gives false confidence when it\n    passes; an unscored case is a permanently-green check nobody runs, which this store\n    already has a rule about.'
    if os.path.basename(path) != "eval-cases.py":
        return None
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("_ec", path)
        if spec is None or spec.loader is None:
            raise ImportError("no module loader for %s" % path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cases = list(getattr(mod, "CASES", []))
    except Exception as exc:                  
        return "eval-cases.py does not load: %r" % (exc,)
    if not cases:
        return "no cases defined"
    bad = []
    seen = set()
    for c in cases:
        cid = c.get("id") or "<unnamed>"
        if cid in seen:
            bad.append("%s (duplicate id)" % cid)
        seen.add(cid)
        if not c.get("why"):
            bad.append("%s (ungrounded: no `why`)" % cid)
        if not (c.get("checks") or c.get("judge")):
            bad.append("%s (unscored)" % cid)
        
        
        
        
        if not c.get("prompt") and not c.get("turns"):
            bad.append("%s (no prompt and no turns)" % cid)
        if c.get("prompt") and c.get("turns"):
            bad.append("%s (has both prompt and turns; `turns` wins, so one is dead)" % cid)
    return "; ".join(bad) if bad else None


def _hardcoded_home(path, src):
    
    
    
    hits = [str(i) for i, line in _code_lines(src)
            if re.search(r"/Users/" + "user" + r"\b", line)]
    if not hits:
        return None
    return "hardcoded home path at line(s) " + ", ".join(hits)


def _agents_search_passes_home(path, src):
    'An upward search for a .agents/ project marker stops at $HOME.'
    pairs = list(_code_lines(src))
    for k, (num, line) in enumerate(pairs):
        if not re.search(r"\bwhile\b", line):
            continue
        window = "\n".join(text for _, text in pairs[k:k + 5])
        if ".agents" not in window or not re.search(r"dirname|\.parent\b", window):
            continue
        if not re.search(r"HOME|home", line):
            return "line %s: upward .agents/ search does not stop at $HOME" % num
    return None


_SHELL_TRUE = re.compile(r"shell\s*=\s*True")



_BUILDS_CMD = re.compile(r"%\s*[\(\w]|\.format\(|\bf['\"]|\+\s*\w+\s*\+")


def _shell_true_unquoted(path, src):
    "A command built by interpolation and run through a shell must use shlex.quote.\n\n    Under `shell=True` an interpolated path is SYNTAX, not data. post-edit-verify.py\n    pasted the edited file's path straight into its lint command, so every edit under a\n    Next.js route group -- `src/app/(app)/page.tsx`, the ordinary case -- died on\n    `sh: syntax error near unexpected token '('`. The hook then injected that as if it\n    were the project's lint output, reporting a verification failure where nothing had\n    been verified. A space or a `$` in a checkout path does the same thing.\n\n    Scoped to files that BUILD their command, because a literal one is safe as written.\n    Presence of `shlex` is the test rather than per-site matching: a file that has\n    reached for it once has the idea, and pinning the exact call shape would fail on\n    the next legitimate spelling."
    body = _uncommented(_unquoted(src))
    if not _SHELL_TRUE.search(body) or not _BUILDS_CMD.search(body):
        return None
    if "shlex" in body:
        return None
    hits = [str(i) for i, line in _code_lines(src) if _SHELL_TRUE.search(line)]
    return "shell=True with an interpolated command and no shlex.quote, at line(s) " \
        + ", ".join(hits or ["?"])


_MAIN_CHECKOUT = re.compile(r"\bMAIN checkout\b")
_SPAWNS = re.compile(r"\bsubagents?\b|\bspawn\w*\b|\bworker-(?:explore|implement|review|ops|device)\b")
_ORCH_SCOPED = re.compile(
    r"orchestrator|YOURS ALONE|never a subagent|no subagent|not a subagent", re.I)


_PLACES_WORK = re.compile(r"\b(keep|kept|write|writes|written|put|store[sd]?)\b", re.I)


def _main_checkout_exception_unscoped(path, src):
    'A main-checkout instruction inside a spawning procedure must say WHO it is for.\n\n    Scoped to bodies that actually spawn agents: a doc with no subagents in it cannot\n    make this mistake, and flagging it would be the false positive that teaches people\n    to skim.'
    if not _SPAWNS.search(src):
        return None
    lines = src.splitlines()
    bad = []
    for i, line in enumerate(lines, 1):
        if not _MAIN_CHECKOUT.search(line):
            continue
        
        
        
        
        
        
        if not _PLACES_WORK.search(line):
            continue
        
        window = "\n".join(lines[max(0, i - 7):i + 6])
        if not _ORCH_SCOPED.search(window):
            bad.append(str(i))
    if not bad:
        return None
    return ("names the MAIN checkout at line(s) " + ", ".join(bad)
            + " without saying the exception belongs to the orchestrator alone")


def _entity_body_is_executable(path, src):
    'A store entity body carrying the exec bit.\n\n    The bit is meaningless here -- home-materialize\'s project() forces +x on the\n    ~/.claude projection, and does NOT sync perms back -- so a 0755 body is drift,\n    not configuration. It is worse than cosmetic: it lets a caller bare-exec the\n    STORE path and appear to work on the machine where the drift happened, while\n    the same call fails "Permission denied" on every other machine in the fleet.'
    del src
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return ""
    if not mode & 0o111:
        return ""
    return "mode %s -- store entity bodies are 0644; only the projection is +x" % (
        oct(mode & 0o777)[2:])






_BARE_EXEC_REG = re.compile(
    r"""git\s+config\s+["']?merge\.\w+\.driver["']?\s+\\?\s*["'](?P<drv>[^"'\n]+)["']"""
    r"""|["']command["']\s*:\s*["'](?P<cmd>[^"'\n]*\.agent-context/[^"'\n]*)["']"""
    r"""|\(\s*os\.path\.join\(\s*S\s*,\s*["'](?P<mgd>[^"'\n]+)["']\s*\)""")

_INTERPRETERS = ("bash", "sh", "zsh", "python3", "python", "node", "deno", "bun", "env")



_PL_NEEDLE = "holistic" + "|" + "synerg"
_PL_CANON = "plain-language-words.py"


def _redefines_plain_language_words(path, src):
    'A second copy of the banned-word list, which is how the guard drifts.'
    if os.path.basename(path) == _PL_CANON:
        return None
    if _PL_NEEDLE not in src:
        return None
    return ("defines its own copy of the plain-language hard list. Import "
            "global/scripts/%s (python) or evaluate `%s --sh` (bash) instead."
            % (_PL_CANON, _PL_CANON))


_PL_WORDS = []
_PL_FENCE = re.compile(r"```.*?```", re.S)
_PL_SPAN = re.compile(r"`[^`\n]*`")
_RE_CALLS = ("compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn",
             "split")

_MATCHED_LITERALS = {
    "BOILERPLATE": "block-deploy.py splits Claude Code's injected hook text at this phrase",
}


def _pl_words():
    'plain-language-words.py beside this script, loaded once. Beside this file and not\n    under STORE, so a fixture store still checks against the real lists.'
    if not _PL_WORDS:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _PL_CANON)
        spec = importlib.util.spec_from_file_location("pl_words_invariant", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PL_WORDS.append(mod)
    return _PL_WORDS[0]


def _message_strings(src):
    'String constants a hook prints or returns: every literal except docstrings.'
    import ast
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return ""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                skip.add(id(body[0].value))
        
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "re" \
                and node.func.attr in _RE_CALLS and node.args:
            for sub in ast.walk(node.args[0]):
                skip.add(id(sub))
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _MATCHED_LITERALS for t in node.targets):
            for sub in ast.walk(node.value):
                skip.add(id(sub))
    return "\n".join(n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and id(n) not in skip)


def _agent_read_text_unplain(path, src):
    'Blocked language in text every agent reads: hook messages, the global\n    instruction and the worker definitions.'
    pl = _pl_words()
    text = _message_strings(src) if path.endswith(".py") else src
    text = _PL_SPAN.sub(" ", _PL_FENCE.sub(" ", text))
    found = []
    for pat in [pl.alt(pl.HARD)] + list(pl.HARD_PHRASE):
        try:
            found += [m.group(0).strip() for m in re.finditer(pat, text, re.I)]
        except re.error:
            pass
    if pl.EM_DASH in text:
        found.append("em dash")
    distinct = []
    for hit in found:
        if hit.lower() not in [d.lower() for d in distinct]:
            distinct.append(hit)
    if not distinct:
        return None
    return ("blocked language in text agents read: %s"
            % ", ".join('"%s"' % d for d in distinct[:8]))


def _registers_a_bare_store_exec(path, src):
    "A driver/settings/MANAGED registration that exec's a store path uninterpreted."
    bad = []
    for m in _BARE_EXEC_REG.finditer(_uncommented(src)):
        cmd = (m.group("drv") or m.group("cmd") or m.group("mgd") or "").strip()
        if not cmd:
            continue
        parts = cmd.split()
        head = os.path.basename(parts[0]) if parts else ""
        if head in _INTERPRETERS:
            continue
        
        
        
        if m.group("mgd") is not None and head.endswith(".py"):
            continue
        
        
        if m.group("mgd") is None and ".agent-context/" not in cmd:
            continue
        bad.append(cmd[:70])
    if not bad:
        return ""
    return ("registers a bare store path: %s -- prefix an interpreter, or point at "
            "the ~/.claude projection" % "; ".join(sorted(set(bad))[:3]))


def _audit_id_allocation_sites():
    'The server modules that mint an audit-observation id. Three sites grew\n    independently -- one filing path and two mid-merge repair paths -- and only the\n    filing path was ever guarded.'
    return sorted(glob.glob(os.path.join(STORE, "server", "src", "agent_context",
                                         "*.py")))


def _allocates_an_observation_id_unguarded(path, src):
    'An id may only come from the shared allocator, never from the git floor alone.\n\n    `_audit_id_floor_from_git` answers "what have the refs spent", which is HALF the\n    question -- it cannot see this machine\'s own unpushed filings, and it returns 0\n    outright when git is unreadable. `add_audit_observation` always compensated by\n    unioning the on-disk ids; the two mid-merge renumberers did not, and that is the\n    entire defect: the same lesson, applied at the site that filed observations and at\n    neither of the sites that repaired them.'
    body = _uncommented(src)
    if "_audit_id_floor_from_git" not in body:
        return None
    
    if re.search(r"def _audit_id_floor_from_git", body):
        return None
    if "_audit_next_id" in body:
        return None
    return ("seeds an observation id from _audit_id_floor_from_git alone -- call "
            "_audit_next_id(store), which unions the refs with the ids on disk")


def _autocommit_stager_sites():
    
    
    return [os.path.join(STORE, "server", "src", "agent_context", "store.py")]


def _stages_foreign_machine_rows(path, src):
    "An autocommit `add -A` that excludes server/ but not machines/*/.\n\n    machines/<uuid>/ is committed only by that machine's daemon. A stager that sweeps\n    it commits whatever stale copy of another machine's row sits in the tree."
    bad = []
    for i, line in enumerate(_uncommented(src).splitlines(), 1):
        if "-A" not in line or ":!server" not in line or "add" not in line:
            continue
        if "machines/*/" not in line:
            bad.append(str(i))
    if not bad:
        return None
    return ("stages machines/*/ at line(s) " + ", ".join(bad)
            + " -- add ':(exclude,glob)machines/*/**' beside ':!server'")





_LONG_TIMEOUT = re.compile(r"\btimeout\s*=\s*(\d+)")
_CLAUDE_PRINT = re.compile(r"""\[\s*(?:CLAUDE|["'][^"']*claude["'])\s*,\s*["'](?:-p|--print)["']""")


def _long_running_script_sites():
    paths = glob.glob(os.path.join(STORE, "global", "scripts", "*.py"))
    paths += glob.glob(os.path.join(STORE, "global", "hooks", "*.py"))
    return sorted(p for p in paths if os.path.basename(p) != "invariant-check.py")


def _long_run_unobservable(path, src):
    'A script that runs a long child or a claude session with no live progress.\n\n    Run in the background or redirected, such a script shows nothing until it exits, so a\n    running job cannot be told from a hung one. Observable means it flushes progress lines\n    (`flush=True`) or writes a `<name>-progress.json` state file.'
    code = _uncommented(src)
    long_to = [int(n) for n in _LONG_TIMEOUT.findall(code) if int(n) >= 300]
    spawns = bool(_CLAUDE_PRINT.search(code))
    if not long_to and not spawns:
        return None
    if "flush=True" in code or "-progress.json" in code:
        return None
    why = ("a child timeout of %ds" % max(long_to)) if long_to else "a `claude -p` session"
    return "runs " + why + " with no flushed progress line and no -progress.json"


def _ralph_refeed_sites():
    return [p for p in _hooks("sh", "py") + _scripts("sh", "py", "ts")
            if "ralph" in os.path.basename(p) and not os.path.basename(p).startswith("test-")]


def _ralph_refeed_sends_whole_prompt(path, src):
    "A ralph re-feed that sends the loop's whole prompt on every fire."
    if not re.search(r'"reason"|sendUserMessage', src):
        return None
    if ".local.md" not in src:
        return None
    if "full_prompt_every" in src:
        return None
    return ("re-feeds a ralph state file without the full_prompt_every policy: send the "
            "one-line pointer by default and the prompt only when full_prompt_every asks")



_CONSENT_REF = re.compile(
    r"(?:git-write|write-outside-home|test-lock)-consent(?:\\?\.sh)?|consent\\?\.sh|consent script",
    re.I)
_HANDOFF = re.compile(
    r"\bask\s+(?:him|them|user|you|the\s+user)\s+to\s+(?:run|unlock)\b"
    r"|\b(?:his|their|your)\s+own\s+terminal\b"
    r"|leading\s+`!`"
    r"|\b(?:he|user)\s+(?:unlocks|approves)\b"
    r"|\bonly\s+user\s+(?:unlocks|removes)\b"
    r"|\brun\s+by\s+user\b"
    r"|\bONLY_JOHN\b"
    r"|\bhis\s+consent\s+script\b",
    re.I)


def _consent_instruction_sites():
    skip = ("/handoffs/", "/audits/", "archive")
    return ([p for p in _hooks("sh", "py")]
            + [p for p in _scripts("sh", "py") if not os.path.basename(p).startswith("test-")]
            + [p for p in _docs() if not any(s in p for s in skip)]
            + _skills() + _instructions() + _memories()
            + [p for p in [os.path.join(STORE, "AGENTS.md")] if os.path.exists(p)])


def _typed_consent_instruction(path, src):
    'Text that tells an agent to hand user a consent command to type.'
    for ref in _CONSENT_REF.finditer(src):
        window = src[max(0, ref.start() - 300):ref.end() + 300]
        hit = _HANDOFF.search(window)
        if hit:
            line = src.count("\n", 0, ref.start()) + 1
            return "line %d: %r beside %r; point to the approval question instead" % (
                line, " ".join(hit.group(0).split()), ref.group(0))
    return None







_UNSIGNED_FALLBACK_RE = re.compile(
    r"""gpgsign["']?\s*(?:[=: ]|,)\s*["']?(?:false|no|off|0)\b|--no-gpg-sign""", re.IGNORECASE)



_UNSIGNED_ENV_KEY_RE = re.compile(
    r"""["'](?:commit|tag)\.gpgsign["']|GIT_CONFIG_KEY_\w*=["']?(?:commit|tag)\.gpgsign\b""",
    re.IGNORECASE)
_UNSIGNED_ENV_VALUE_RE = re.compile(
    r"""GIT_CONFIG_VALUE_[^\n]*?[=:]\s*["']?(?:false|no|off|0)\b""", re.IGNORECASE)



_UNSIGNED_SELF = ("invariant-check.py",
                  
                  "test-invariant-unsigned-gitconfig.py",
                  "test-invariant-unsigned-env-pairs.py")


def _unsigned_commit_fallback_sites():
    
    tests = os.path.join(STORE, "server", "tests")
    server_tests = ([os.path.join(tests, n) for n in sorted(os.listdir(tests)) if n.endswith(".py")]
                    if os.path.isdir(tests) else [])
    return [p for p in _scripts("py") + _hooks("py") + _server_py() + server_tests
            + _chezmoi_sources()
            if os.path.basename(p) not in _UNSIGNED_SELF]


def _has_unsigned_commit_fallback(path, src):
    ' has unsigned commit fallback.'
    body = _uncommented(src)
    if _UNSIGNED_FALLBACK_RE.search(body) or (
            _UNSIGNED_ENV_KEY_RE.search(body) and _UNSIGNED_ENV_VALUE_RE.search(body)):
        return "creates or configures a commit with commit.gpgsign=false or --no-gpg-sign"
    return None




def _upsert_carry_sites():
    return _server_py()


_CARRY_CALL = "_carry_existing_keys("


def _def_blocks(src):
    "(name, block) per `def`, the block running to the next line at or left of its own\n    indent. A nested def is part of its parent's block as well as being its own."
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("def "):
            continue
        indent = len(line) - len(line.lstrip())
        name = line.lstrip()[4:].split("(")[0].strip()
        end = len(lines)
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                end = j
                break
        yield name, "\n".join(lines[i:end])


def _call_args(block, call):
    'The text of each `call(...)` in `block`, parentheses balanced, so a write spread\n    over several lines is read whole.'
    out, start = [], block.find(call)
    while start != -1:
        depth, i = 0, start + len(call) - 1
        while i < len(block):
            if block[i] == "(":
                depth += 1
            elif block[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(block[start:i + 1])
        start = block.find(call, i + 1)
    return out


def _builds_frontmatter_without_carry(path, src):
    "A function that persists an entity's frontmatter or a sidecar without carrying the\n    keys already stored. store.upsert is the one place that writes them, so a second site\n    would bring back the silent key drop this invariant exists to stop."
    for name, block in _def_blocks(_uncommented(src)):
        writes = _call_args(block, "_write_atomic(")
        if not any("emit_frontmatter(" in w or ".meta.toml" in w or ".meta.json" in w
                   for w in writes):
            continue
        if _CARRY_CALL not in block:
            return f"{name}() writes entity frontmatter or a sidecar without {_CARRY_CALL[:-1]}"
    return None


def _typed_link_write_sites():
    return _server_py()


def _takes_links_without_validating(path, src):
    'A function that accepts `links` must put it through graph.link_fields, the one\n    place an unknown relation is refused and a target is normalized to a vault path.'
    for name, block in _def_blocks(_uncommented(src)):
        head, _, rest = block.partition("):")
        sig = head if rest else block.splitlines()[0]
        if not re.search(r"\blinks\b", sig):
            continue
        if name == "link_fields":
            continue
        if "link_fields(" not in block:
            return f"{name}() takes `links` without calling graph.link_fields"
        if "store=store" not in block:
            return f"{name}() calls graph.link_fields without the store for vault paths"
    return None


_INDEX_BODY_READS = ('.get("body")', ".get('body')", '.get("script_body")',
                     ".get('script_body')", '["body"]', '["script_body"]',
                     ".get(field)", ".get(body_key)", ".get(_BODY_FIELD")

_BODY_SUBSTITUTION = re.compile(r'\b(?:body|script_body|new_body)\s+or\s+""')




_WRITE_CALLS = ("upsert(", "_write_atomic(", "emit_frontmatter(")


_EXACT_BODY_FALLBACKS = ('.get(body_key)', '.get("body")', ".get('body')",
                         '.get(_BODY_FIELD', '_body_to_keep(')


def _body_bytes_sites():
    return _server_py()


def _write_does_not_read_the_file(path, src):
    "A write that re-sends a stored body must send the bytes the FILE holds, and must\n    still have a body when the file cannot be read.\n\n    The index keeps a body with its trailing newlines stripped and `emit_frontmatter`\n    puts one back, so a route that hands the index copy to a write rewrites the file's\n    last byte. `store.exact_body`, `store._body_to_keep` and `shaping._edit_source` are\n    the places that read the file, and every other route goes through one of them.\n\n    The second half is the review finding that followed: `exact_body` answers None for a\n    file whose frontmatter is gone, whose bytes are not UTF-8, or that was deleted under\n    the index, and the first cut read that None as an empty body. So a caller of\n    `exact_body` must name a fallback in the same function."
    for name, block in _def_blocks(_uncommented(src)):
        if name in ("exact_body", "_edit_source", "_body_to_keep"):
            continue
        calls = [c for w in _WRITE_CALLS for c in _call_args(block, w)]
        for call in calls:
            arg = call.split("body=", 1)[1] if "body=" in call else call
            if any(r in arg for r in _INDEX_BODY_READS):
                return (f"{name}() gives a write a body taken from the index; read it "
                        f"with store.exact_body or shaping._edit_source")
            if _BODY_SUBSTITUTION.search(arg):
                return f'{name}() writes "" in place of a body the caller did not pass'
        if "exact_body(" in block and not any(f in block for f in _EXACT_BODY_FALLBACKS):
            return (f"{name}() calls exact_body without a fallback for the file it could "
                    f"not read, so an unreadable body would be written as empty")
    return None


def _graph_file_sites():
    return [os.path.join(G, "scripts", "entity-root-pages.py"),
            os.path.join(STORE, "server", "src", "agent_context", "refs.py")]


def _graph_file_path_missing(path, src):
    if os.path.basename(path) == "entity-root-pages.py":
        if '"## Global files"' not in src or 'owned_markdown_links(os.path.join(ROOT, "global")' not in src:
            return "the global root page does not enumerate global Markdown files"
    elif "file_paths.add(os.path.relpath(path, store.root)" not in src or '| file_paths' not in src:
        return "wikilink validation does not recognize canonical Markdown file paths"
    return None


def _neutral_worktree_sites():
    paths = [
        "global/instructions/global-agent-instructions.md",
        "global/docs/worktrees.md",
        "global/docs/agents-layout.md",
        "global/scripts/home-settings-sync.py",
        "global/scripts/project-materialize.py",
        "global/scripts/wt-sweep.py",
        "global/scripts/worktree-create.py",
        "global/hooks/preflight-core-health.py",
        "global/hooks/guard-git-write.py",
        "global/hooks/record-session-claim.py",
        "global/hooks/require-worktree-edit.py",
        "global/agents/worker-implement.md",
        "global/skills/ralph-maintainability/SKILL.md",
        "projects/example-api/skills/ralph-cicd/SKILL.md",
        "projects/example-web/skills/ralph-cicd/SKILL.md",
        "global/scripts/token-usage-collect.py",
        "global/scripts/wt_finish_core.py",
    ]
    paths += ["projects/%s/scripts/wt-finish-core.py" % name
              for name in ("example-app", "example-web", "example-project", "example.invalid")]
    return [os.path.join(STORE, p) for p in paths]


def _neutral_worktree_missing(path, src):
    rel = os.path.relpath(path, STORE)
    required = {
        "global/scripts/home-settings-sync.py": '("WorktreeCreate", None',
        "global/scripts/project-materialize.py": 'full in worktrees',
        "global/scripts/worktree-create.py": '".agents", "worktrees"',
        "global/scripts/wt-sweep.py": "hp.HARNESS_DIRNAMES",
        "global/hooks/preflight-core-health.py": "hp.worktree_marks()",
        "global/hooks/record-session-claim.py": "(?:agents|claude)",
        "global/scripts/token-usage-collect.py": "(?:agents|claude)",
        "global/hooks/require-worktree-edit.py": "for scope in hp.HARNESS_DIRNAMES",
        "global/scripts/wt_finish_core.py": "hp.HARNESS_DIRNAMES",
        "global/skills/ralph-maintainability/SKILL.md": WORKTREE_GLOB,
        "projects/example-api/skills/ralph-cicd/SKILL.md": WORKTREE_GLOB,
        "projects/example-web/skills/ralph-cicd/SKILL.md": WORKTREE_GLOB,
    }
    if rel in required:
        marker = required[rel]
    elif rel.endswith("/wt-finish-core.py") and _delegates_to_wt_finish_core(src):
        return None
    elif rel.startswith("projects/"):
        marker = '(".agents", ".claude")'
    else:
        marker = ".agents/worktrees"
    if marker not in src:
        return "missing neutral worktree handling: %s" % marker
    return None


def _projection_sites():
    names = []
    for folder in ("hooks", "scripts"):
        base = os.path.join(STORE, "global", folder)
        if os.path.isdir(base):
            names += [os.path.join(base, n) for n in sorted(os.listdir(base))
                      if n.endswith(".py") and not n.startswith("test-")]
    server = os.path.join(STORE, "server", "src", "agent_context")
    if os.path.isdir(server):
        names += [os.path.join(server, n) for n in sorted(os.listdir(server)) if n.endswith(".py")]
    return names


_PROJECTION_ALLOWED = ("home-materialize.py", "check-harness-paths.py", "invariant-check.py",
                       "harness_paths.py", "state-migrate.py", "store-reader-graph.py")
_LEGACY_PROJECTION = re.compile(re.escape(hp.CLAUDE_DIRNAME) + r"/(hooks|scripts|docs)\b")


def _names_claude_projection(path, src):
    if os.path.basename(path) in _PROJECTION_ALLOWED:
        return None
    if _LEGACY_PROJECTION.search(src):
        return "names a retired Claude-side hooks, scripts or docs directory"
    return None


REGISTRY = [
    Invariant(
        "no-claude-projection-paths",
        "store hooks and scripts run in place from the store: no script or hook names the retired "
        "~/.claude hooks, scripts or docs directory",
        "A stale path to a retired ~/.claude copy fails open in a guard, so the guard "
        "stops guarding.",
        _projection_sites,
        _names_claude_projection,
        fix="build the path through harness_paths (hooks_dir, scripts_dir, docs_dir)",
    ),
    Invariant(
        "neutral-worktree-layout",
        "new worktrees live under .agents/worktrees; current guards, finishers and docs accept that path",
        "A harness-specific worktree path was embedded across creation, guards and landing.",
        _neutral_worktree_sites,
        _neutral_worktree_missing,
        fix="move the default to .agents/worktrees and retain legacy .claude/worktrees support",
    ),
    Invariant(
        "global-graph-file-links",
        "generated root pages enumerate every scoped Markdown file and the reference "
        "resolver recognizes their canonical file paths",
        "Global Markdown files were isolated in the graph, and the integrity check called "
        "valid generated project and workspace links dangling.",
        _graph_file_sites,
        _graph_file_path_missing,
        fix="generate global file links on agent-context-store.md and include canonical "
            "file paths in refs._known_targets",
    ),
    Invariant(
        "patch-writes-keep-body-bytes",
        "no write path substitutes an empty string, or the index's stripped copy, for a "
        "body the caller did not pass; an omitted body means the file's bytes stay as "
        "they are",
        "A patch route that hands back the index copy of a body rewrites the last byte "
        "of every entity it touches, because the index strips trailing newlines.",
        _body_bytes_sites,
        _write_does_not_read_the_file,
        fix="pass body=None to store.upsert when the write does not mean to change the "
            "prose, and take an edit's starting text from shaping._edit_source",
    ),
    Invariant(
        "typed-links-validated-once",
        "every server function that accepts `links` routes it through graph.link_fields "
        "with its store, so unknown relations are refused and targets use vault paths",
        "A write path that skips the typed-link validation puts an unknown relation, or "
        "a bare target no resolver can read, into a file.",
        _typed_link_write_sites,
        _takes_links_without_validating,
        allow={"server.py": "the MCP layer forwards `links` to the tool function and "
                            "validates nothing itself, by design"},
        fix="call graph.link_fields(links, store=store) and return its error before writing",
    ),
    Invariant(
        "upsert-carries-existing-keys",
        "every server function that persists an entity's frontmatter or sidecar carries "
        "the keys already stored, through _carry_existing_keys",
        "A full upsert that rebuilds `meta` from the fields a call passed drops every "
        "key it does not repeat.",
        _upsert_carry_sites,
        _builds_frontmatter_without_carry,
        fix="call _carry_existing_keys(meta, existing, fields) in the same function, "
            "before the write, or route the write through store.upsert",
    ),
    Invariant(
        "no-typed-consent-instructions",
        "no hook, script, skill, doc, memory, instruction or bootstrap file tells an agent "
        "to hand user a consent command; approval is an AskUserQuestion that "
        "approval-question grants",
        "Approval is a structured question, and any text that still names a typed "
        "command sends the next agent back to it.",
        _consent_instruction_sites,
        _typed_consent_instruction,
        allow={"hook-test-cases.py": "test fixtures: block-consent-self-grant's cases quote "
                                     "old refusal wording that only names a grant, to "
                                     "prove the guard lets such a line through"},
        fix="replace the command with the approval question: header Approval, options "
            "Approve and Deny, question text from approval-question's HOW_TO_ASK",
    ),
    Invariant(
        "no-unsigned-commit-fallback",
        "no live store, server or chezmoi code passes commit.gpgsign=false or "
        "--no-gpg-sign -- a commit is signed or it is not made",
        "A commit that falls back to unsigned when the signing agent lacks a key stalls "
        "the mirrors, so no commit is ever unsigned.",
        _unsigned_commit_fallback_sites,
        _has_unsigned_commit_fallback,
        allow={"test_fleet_out_of_band.py": "one case sets commit.gpgsign=false on purpose, "
                                            "to prove the store then leaves -S off "
                                            "commit-tree; every other fixture there signs",
"test_commit_on_write.py": "one case makes a deliberately unsigned commit "
                           "(--no-gpg-sign) to prove the push gate refuses "
                           "to publish it: a negative fixture for the gate, "
                           "never a live fallback (user, 2026-09-21)"},
        fix="sign for real instead: a throwaway local key "
            "(`ssh-keygen -t ed25519 -N '' -f <fixture>/key`) plus repo config "
            "gpg.format=ssh, user.signingkey=<fixture>/key.pub, commit.gpgsign=true "
            "needs no ssh-agent. A live site that cannot sign must fail the commit, "
            "not skip signing it.",
    ),
    Invariant(
        "ralph-refeed-is-a-pointer",
        "every ralph re-feed (Stop hook or extension) sends a one-line pointer by default "
        "and the whole prompt only when full_prompt_every opts in",
        "A Stop hook that re-sends the whole loop prompt on every fire costs context a "
        "pointer does not.",
        _ralph_refeed_sites,
        _ralph_refeed_sends_whole_prompt,
        fix="read full_prompt_every from the frontmatter (absent or non-numeric is 0), send "
            "`Ralph N, continue, do not restart. <state path>` unless it selects this fire.",
    ),
    Invariant(
        "long-runs-are-observable",
        "a store script that runs a child for five minutes or more, or a claude -p "
        "session, reports progress while it runs",
        "A long run that prints nothing until the end cannot be told from a hung one.",
        _long_running_script_sites,
        _long_run_unobservable,
        allow={"test-hook-dispatch.py": "a battery: its 1800 s child is hook-test-run "
                                        "--via-dispatch, which prints a line per case; the "
                                        "battery captures that output to assert the summary "
                                        "and prints its own PASS or FAIL per check",
               "test-invariant-relay-mode.py": "a battery: its 600 s child is one "
                                               "invariant-check run, captured to assert its "
                                               "output; the battery prints its own PASS or "
                                               "FAIL per check"},
        fix="print a `[n/total] item started/finished` line with "
            "`file=sys.stderr, flush=True` per unit of work, and for one step that can "
            "run many minutes also write ~/.local/state/agent-context/<name>-progress.json "
            "(tmp then os.replace).",
    ),
    Invariant(
        "autocommit-excludes-foreign-machine-rows",
        "every autocommit stager excludes machines/*/; only the owning daemon commits "
        "its own row",
        "A commit that sweeps in a stale copy of another machine's fleet row makes that "
        "machine conflict on its own row every cycle.",
        _autocommit_stager_sites,
        _stages_foreign_machine_rows,
        fix="exclude ':(exclude,glob)machines/*/**' and, in the daemon, add "
            "machines/<own-uuid>/ explicitly.",
    ),
    Invariant(
        "observation-id-from-the-shared-allocator",
        "every site that mints an audit-observation id goes through _audit_next_id, "
        "never the git floor alone",
        "A renumberer seeded from the git floor alone mints ids the archive already "
        "holds, and each resolved collision becomes a new conflict on the next machine "
        "to sync (policy).",
        _audit_id_allocation_sites,
        _allocates_an_observation_id_unguarded,
        fix="seed from _audit_next_id(store) and probe free ids with _audit_id_taken, "
            "which spans the archive as well as the active directory.",
    ),
    Invariant(
        "store-entity-body-not-executable",
        "a store entity body is mode 0644 -- the exec bit lives on the projection, "
        "never on the source of truth",
        "A consumer that executes a store file by its path depends on a mode bit the "
        "store does not guarantee, and dies with 'Permission denied' where the bit is "
        "absent.",
        lambda: _hooks("py", "sh") + _scripts("py", "sh", "ts"),
        _entity_body_is_executable,
        fix="chmod 0644 the body, and fix the CALLER -- prefix python3/bash, or point it "
            "at ~/.agent-context/global/scripts/. Never restore the bit to satisfy a caller.",
    ),
    Invariant(
        "plain-language-words-single-source",
        "only plain-language-words.py defines the banned-word list",
        "A word list copied into the guard and both sweeps lets the guard block more "
        "than the sweeps can see.",
        lambda: _hooks("py", "sh") + _scripts("py", "sh"),
        _redefines_plain_language_words,
        fix="import global/scripts/plain-language-words.py in python, or eval its "
            "--sh output in bash. Never restate the alternation.",
    ),
    Invariant(
        "agent-read-text-plain",
        "hook messages, the global instruction and worker definitions use no blocked language",
        "Text an agent reads every session sets how it writes, so padding and offers in "
        "that text are taught.",
        lambda: _hooks("py") + _instructions()
        + [p for p in _agents() if os.path.basename(p).startswith("worker-")],
        _agent_read_text_unplain,
        fix="rewrite the sentence as the plain fact. A quotation of a banned phrase goes "
            "in backticks.",
    ),
    Invariant(
        "no-bare-store-exec-registration",
        "a registration that exec's a store path names an interpreter",
        "Git reports a merge driver that fails to exec as an ordinary conflict, which "
        "stops the fleet syncing until a human intervenes.",
        lambda: _hooks("py", "sh") + _scripts("py", "sh"),
        _registers_a_bare_store_exec,
        allow={
            "hook-test-cases.py":
                "a fixture corpus, not a registration site. Every match is a payload "
                "handed to a hook under test ('echo x > <store>/...'), which is the "
                "input the guard must REFUSE -- exactly the string this check hunts, "
                "and correct to have here.",
            "test-hook-parity-run.py":
                "a fixture, not a registration: a stub agents-remateralize writes stamp.txt "
                "into a throwaway store clone and commits it, to prove volatile effects "
                "normalize. Nothing is exec'd from that path.",
        },
        fix="prefix python3/bash in the registration, and assert the exact value instead "
            "of checking that some registration exists -- an existence-only guard leaves "
            "the broken value in place forever on every machine that already has it",
    ),
    Invariant(
        "main-checkout-exception-scoped",
        "a spawning procedure that names the MAIN checkout says the exception is the "
        "ORCHESTRATOR's alone",
        "An unscoped 'keep ledger writes in the main checkout' reads as general guidance "
        "and reaches agents it was not written for.",
        lambda: _skills() + _docs() + _agents(),
        _main_checkout_exception_unscoped,
        fix="say who the exception is for -- 'that is the orchestrator's job, never a "
            "subagent's' -- at EVERY place the main checkout is named",
    ),
    Invariant(
        "daemon-can-be-retired",
        "a long-lived daemon can be asked to exit, and reports which build it is running",
        "An lspd daemon runs the code it was spawned with, so a fix cannot reach a "
        "running daemon unless the daemon can be retired (policy).",
        lambda: _scripts("py", "sh"),
        _daemon_cannot_be_retired,
        fix="give it a verb that makes a running instance exit (lspd.py's "
            "`$/lspd/shutdown` + `--upgrade`), and put its own code fingerprint in "
            "whatever `--status` prints, so staleness is visible before it is puzzling",
    ),
    Invariant(
        "python-hook-crash-handler",
        "every python HOOK wraps its entry point in `except Exception` and still emits a report",
        "A hook that dies with an unhandled exception at SessionStart is swallowed by "
        "the harness and hides the findings it was meant to report.",
        lambda: _hooks("py"),
        _py_hook_no_crash_handler,
        fix="wrap main() in try/except and emit a finding that says the hook itself failed",
    ),
    Invariant(
        "store-git-write-merge-guard",
        "anything that STAGES in the store refuses while a merge/rebase is in progress",
        "A bare `git add -A` during a merge resolves a conflicted file and commits its "
        "conflict markers into shared history.",
        lambda: _hooks("sh", "py") + _scripts("sh", "py"),
        _store_write_no_merge_guard,
        
        
        
        
        
        
        
        
        
        
        
        
        
        fix="exit before staging when MERGE_HEAD/CHERRY_PICK_HEAD/REVERT_HEAD/BISECT_LOG "
            "exists, a rebase dir exists, or `git ls-files --unmerged` is non-empty",
    ),
    Invariant(
        "health-writer-fail-path",
        "a component that records health success also records FAILURE",
        "A step that ends in `|| true` produces no output anyone reads and exits 0, so "
        "its failure looks normal.",
        lambda: _scripts("sh", "py") + _hooks("sh", "py"),
        _health_writer_no_fail_path,
        allow={"test-hook-dispatch.py": "a battery: it asserts the records hook-dispatch writes "
                                        "into a temp HOME and records no health of its own",
               "test-hook-server.py": "a battery: it copies health-record.py beside its fixture "
                                      "dispatcher and records no health of its own"},
        fix="record --fail with a detail line on every failure branch, not just --ok on success",
    ),
    Invariant(
        "set-e-pipeline-guard",
        "under `set -e`, a pipeline starting in ls/find/grep carries `|| true`",
        "A script under `set -e` that dies on its last pipeline statement produces a "
        "false DEGRADED banner.",
        lambda: _hooks("sh") + _scripts("sh"),
        _set_e_pipeline,
        fix="append `|| true` to the pipeline, or drop `set -e` for that block",
    ),
    Invariant(
        "enforcing-hook-has-test",
        "every hook that denies work or speaks to the agent has a test-<name> counterpart",
        "An untested hook that denies work either blocks legal work or permits what it "
        "exists to stop.",
        lambda: [p for p in _hooks("sh", "py") if _speaks_to_the_model(p)],
        _enforcer_without_test,
        fix="add a CASES entry for <hookname>.py to hook-test-cases.py (run via "
            "hook-test-run.py --hook <hookname>), or a standalone global/scripts/"
            "test-<hookname>.py battery, exercising both the deny and the allow path",
    ),
    Invariant(
        "eval-case-grounded",
        "every eval case names the real failure it encodes and is actually scored",
        "An invented case measures an imagined agent, and an unscored one is a check "
        "that is always green.",
        lambda: _scripts("py"),
        _eval_cases_ungrounded,
        fix="give the case a `why` naming the incident, and at least one check or a judge",
    ),
    Invariant(
        "always-loaded-context-within-budget",
        "instructions, `always` memory rows and tool schemas stay inside their budgets",
        "A budget nothing enforces is exceeded while each edit looks reasonable, and "
        "memory rows, AGENTS.md and MCP tool schemas grow unmetered.",
        _context_budget_site,
        _context_over_budget,
        fix="run `python3 ~/.agent-context/global/scripts/context_budget.py` for the bill, then "
            "cut the named line: move rationale to a doc, demote a memory to lazy, or drop a tool",
    ),
    Invariant(
        "check-mode-can-fail",
        "a script accepting --check/--verify has a reachable non-zero exit",
        "A validation mode that cannot exit non-zero is indistinguishable from one that "
        "always passes.",
        lambda: _scripts("sh", "py"),
        _check_mode_cannot_fail,
        fix="parse for real and exit non-zero on the failure, naming what and where",
    ),
    Invariant(
        "memory-description-declares-its-carve-outs",
        "an always-loaded memory whose body carries a carve-out says so in its description",
        "The bootstrap index carries descriptions only, so a description that omits a "
        "carve-out is acted on without the body being read (policy).",
        lambda: _memories(),
        _memory_description_hides_a_carve_out,
        allow={},
        fix="fold the exception into the description, or end it with the marker `→ body`",
    ),
    Invariant(
        "no-conflict-markers-in-entities",
        "no store entity carries committed git conflict markers",
        "An entity with conflict markers keeps loading, so every session reads spliced "
        "versions and nothing says so.",
        lambda: (_docs() + _memories() + _instructions()
                 + _hooks("sh", "py") + _scripts("sh", "py") + _agents()),
        _conflict_markers_committed,
        fix="repair the entity through edit_body, then check what the merge reverted",
    ),
    Invariant(
        "store-writes-are-atomic",
        "no store writer truncates a file before it has the new bytes",
        "A write that dies mid-truncate leaves a 0-byte file that autosync commits, and "
        "the merge then conflicts on every sync cycle.",
        lambda: _server_py(),
        _store_write_not_atomic,
        fix="route the write through store._write_atomic(path, text) — tmp file in "
            "the same directory, fsync, then os.replace",
    ),
    Invariant(
        "fsync-stays-behind-one-switch",
        "no server module flushes to disk outside the durability switch",
        "An unconditional fsync costs what the slowest volume in the fleet charges, "
        "which can push the self-deploy gate past its timeout.",
        lambda: _server_py(),
        _fsync_not_behind_the_switch,
        fix="wrap the call in `if _fsync_enabled():` (store.py) — one switch, so the "
            "gate can turn durability off for a throwaway tmp_path store and nowhere else",
    ),
    Invariant(
        "one-atomic-writer",
        "no server module hand-rolls tmp+os.replace; all go through paths.write_atomic",
        "A hand-rolled tmp+replace that does not remove its temp file on failure leaves "
        "`.tmp` litter in the synced tree.",
        lambda: _server_py(),
        _hand_rolled_atomic_write,
        fix="call paths.write_atomic(path, text); it is stdlib-only so nothing can "
            "cycle on it",
    ),
    Invariant(
        "projection-writes-are-atomic",
        "no projection script truncates a file another process executes or loads",
        "A projected file written with open(path, \"w\") is empty or half-written to a "
        "session that starts in that window.",
        lambda: _projection_py(),
        _store_write_not_atomic,
        fix="write through a tmp file in the same directory and os.replace it; "
            "harness-materialize.write_text_atomic is the reference",
    ),
    Invariant(
        "interpreter-is-rendered",
        "no store script starts a command with a bare python3 or python",
        "A bare python3 resolves to the system interpreter where PATH lacks "
        "~/.local/bin, which holds every store script to that interpreter's syntax.",
        
        
        lambda: [p for p in _scripts("py") + _hooks("py")
                 + _project_scripts("py") + _project_hooks("py")
                 if not os.path.basename(p).startswith("test-")
                 and os.path.basename(p) != "hook-test-cases.py"],
        _bare_python_command,
        allow={"eval-cases.py": "its command strings run inside eval sandboxes, under the "
                                "interpreter the sandbox provides, not this host's",
               "store-reader-graph.py": "a report label names python3 -m as a blind spot; "
                                        "the script runs no command"},
        fix="sys.executable for a subprocess; agent-python.py interpreter() for a command "
            "rendered into a harness config",
    ),
    Invariant(
        "hook-readers-expand-dispatch",
        "every reader of the hooks in settings.json expands dispatched events through "
        "hook-registry.py",
        "A reader that asks settings.json directly which hooks run sees only the "
        "dispatcher and reports every dispatched guard as unregistered.",
        
        
        lambda: [p for p in _scripts("py") + _hooks("py")
                 if not os.path.basename(p).startswith("test-")
                 and os.path.basename(p) not in ("hook-test-cases.py", "hook-registry.py")],
        _hook_reader_skips_registry,
        allow={
            "hook-latency-probe.py": "times the commands Claude Code runs, so it must see the "
                                     "dispatcher itself and not the guards behind it",
            "project-settings-sync.py": "a project's .agents/claude/settings.json, which no "
                                        "dispatched event is ever written to",
            "harness-materialize.py": "copies Claude's settings.json whole for the Xcode agent "
                                      "and writes other harnesses' hook files",
        },
        fix="load hook-registry.py and read effective_hooks(settings_path) instead of "
            "settings['hooks']",
    ),
    Invariant(
        "codex-hooks-derived-from-claude",
        "Codex hook wiring is generated from Claude's reconciled graph through one adapter",
        "A second Codex hook registry would make every hook change a two-site edit "
        "that drifts.",
        lambda: [os.path.join(G, "scripts", name) for name in
                 ("harness-materialize.py", "codex-hook-adapter.py")],
        _codex_hook_parity_not_dynamic,
        fix="derive from ~/.claude/settings.json after home-settings-sync; keep only event and "
            "payload translation in codex-hook-adapter.py",
    ),
    Invariant(
        "codex-test-unlock-human-consent",
        "Codex test unlocks require a structured user reply and block agent self-grants",
        "Claude's AskUserQuestion approval hook cannot see Codex's asynchronous question "
        "reply, so the Codex handler needs its own dispatch wiring and self-grant guard.",
        lambda: [os.path.join(G, "hooks", "codex-test-unlock.py")],
        _codex_test_unlock_not_guarded,
        fix="wire codex-test-unlock on UserPromptSubmit and deny direct agent invocation",
    ),
    Invariant(
        "codex-content-parity",
        "Codex receives every safe Claude MCP and global/project content projection",
        "Missing a Codex call site or manifest flag silently hides a store capability.",
        lambda: [os.path.join(G, "scripts", name) for name in
                 ("harness-materialize.py", "agents-materialize.py")] +
                [os.path.join(G, "mcp-servers.json")],
        _codex_content_parity,
        fix="project global and per-project skills, commands, agents and safe MCP servers",
    ),
    Invariant(
        "decision-regex-is-shared",
        "the decision-phrase list is defined once, in decision-phrases.py",
        "A hand-copied decision regex in two hooks drifts, so a worker can hand back a "
        "question the lead is then blocked for echoing.",
        lambda: _hooks("py") + _scripts("py"),
        _own_decision_regex,
        fix="import ~/.agent-context/global/scripts/decision-phrases.py by path and call find_decisions",
    ),
    Invariant(
        "worker-restates-uninheritable-rules",
        "every worker definition restates the rules a subagent cannot inherit",
        "A subagent gets none of the Global Agent Instructions, so a rule that lives "
        "only there does not reach it (policy).",
        lambda: _agents() + [p for p in [os.path.join(G, "docs", "worker-shared-rules.md")]
                             if os.path.exists(p)],
        _worker_missing_uninheritable_rule,
        fix="state the rule in worker-shared-rules.md, and open each worker with its "
            "get_doc pointer",
    ),
    Invariant(
        "worker-guardrails-match-agents-md",
        "a worker's Filesystem safety paragraph names no path the AGENTS.md section does not",
        "Workers inherit none of AGENTS.md, so a restatement that drifts from it is the "
        "only copy they read.",
        lambda: _agents(),
        _worker_guardrails_drift,
        fix="make the worker paragraph name only paths the AGENTS.md Filesystem safety "
            "section names, or add the path to AGENTS.md first",
    ),
    Invariant(
        "worker-shared-text-identical",
        "a paragraph every worker restates reads the same in every worker that has it",
        "Each worker file restates the shared rules by hand, and the copies drift with "
        "nothing comparing them (policy).",
        lambda: _agents(),
        _worker_shared_text_drift,
        fix="make the paragraph match the other workers, or add the agent to that "
            "paragraph's exception map in _WORKER_SHARED_PARAGRAPHS with a reason",
    ),
    Invariant(
        "agent-envelope-stated",
        "a worker definition that lacks Bash/Edit/Write says so in its description",
        "A dispatcher chooses from descriptions, so a limit visible only in the "
        "`tools:` field is invisible at the moment of choosing (policy, #320).",
        lambda: _agents(),
        _agent_envelope_unstated,
        fix="state the limit in the description — what it cannot do, and who to send "
            "that work to instead",
    ),
    Invariant(
        "hook-wired-to-its-declared-event",
        "a hook's declared event_type matches the event home-settings-sync wires it to",
        "A hook wired under a different event than it declares keeps firing on the old "
        "event, where the payload it expects does not exist.",
        lambda: _hooks("sh", "py"),
        _wired_to_a_different_event,
        fix="move the entry in home-settings-sync's MANAGED list into the block for the "
            "event the hook declares",
    ),
    Invariant(
        "hook-fingerprint-matches",
        "a hook's script and wiring match the fingerprint recorded in hook-fingerprints.json",
        "A guard's body or matcher can change with nothing to say so, and a hook that "
        "never runs looks correct everywhere a reader looks (policy).",
        _hook_fingerprint_sites,
        _hook_fingerprint_drift,
        fix="review the change, then run `python3 ~/.agent-context/global/scripts/"
            "hook-registration-probe.py --update-fingerprints` and commit the manifest",
    ),
    Invariant(
        "stop-hook-reaches-the-model",
        "a Stop hook meant to change the agent's next turn does not rely on systemMessage",
        "A Stop hook's systemMessage is terminal output for the human and never enters "
        "model context.",
        lambda: _hooks("sh", "py"),
        _stop_hook_talks_to_nobody,
        fix="move the check to UserPromptSubmit and emit hookSpecificOutput."
            "additionalContext, looking BACKWARD at the turn that just finished",
    ),
    Invariant(
        "embedded-python-is-heredoc-quoted",
        "a multi-line python program inside a shell hook is carried in a quoted "
        "heredoc, not `python3 -c '...'`",
        "An apostrophe inside a `python3 -c '...'` block closes the string, and a hook "
        "that runs on every Bash call then fails every command.",
        lambda: _hooks("sh") + _scripts("sh"),
        _embedded_python_in_single_quotes,
        
        
        
        
        
        
        
        
        fix="carry the program in a quoted heredoc -- `python3 - <<'PY' ... PY` -- "
            "passing the payload by environment variable, since the heredoc takes stdin",
    ),
    Invariant(
        "stat-portability",
        "every `stat -f` has a `stat -c` fallback somewhere in reach",
        "`stat -f %m` is BSD-only, so without the GNU fallback every file looks "
        "unmodified since the epoch on a Linux host.",
        lambda: _hooks("sh") + _scripts("sh"),
        _stat_not_portable,
        fix="stat -f %m F 2>/dev/null || stat -c %Y F 2>/dev/null || echo — then treat "
            "the empty case as unknown and suppress, never as 0",
    ),
    Invariant(
        "prose-guard-covers-every-store-writer",
        "a general guard over store writes covers every tool that carries text, "
        "and can read each one",
        "A hand-typed list of store write tools is short as soon as the server gains "
        "one, and what the guard misses is pushed to every remote within seconds.",
        lambda: _hooks("sh", "py"),
        _prose_guard_is_short,
        fix="take the matcher from `store-prose-tools --matcher`, and teach the "
            "extractor the new tools' field names from `--fields`",
    ),
    Invariant(
        "hook-deploys-what-its-row-declares",
        "every hook's wiring in ~/.claude/settings.json carries the event and the "
        "full matcher its store row declares",
        "A guard's declaration and its deployment are maintained separately, so a guard "
        "can be declared for every store writer and deployed for one.",
        lambda: _hooks("sh", "py"),
        _hook_deploy_diverges,
        fix="re-run `home-materialize.py` (which invokes home-settings-sync) if the "
            "machine is merely behind; if the matcher itself is wrong, fix the "
            "MANAGED table in home-settings-sync.py -- and prefer deriving the "
            "matcher (see `store-prose-tools --matcher`) over typing a list, since "
            "a typed list has now been short three times",
    ),
    Invariant(
        "landing-gate-judges-relevance",
        "every project's wt-finish-core.py asks whether the gate can affect the change, "
        "through the shared helper, itself or by delegating to wt_finish_core.py, which asks",
        "Landing a change that no build can see should not cost a full build or an "
        "interruption, and the judgment belongs in one shared script.",
        _wt_finish_scripts,
        _landing_gate_judges_relevance,
        fix="call $HOME/.agent-context/global/scripts/gate-relevance.py with this repo's own "
            "build-irrelevant globs, and clear the run_* flags when it exits 0",
    ),
    Invariant(
        "no-hardcoded-home",
        "no script hardcodes /Users/user; the fleet has Linux hosts",
        "A hardcoded home path fails only on the machines where that home does not "
        "exist, which is where nobody is watching a session.",
        lambda: _hooks("sh", "py") + _scripts("sh", "py"),
        _hardcoded_home,
        allow={
            "invariant-check.py": "this checker necessarily contains the path it searches for -- in its own search pattern and in the finding message it prints. Exempted by name rather than by a cleverer regex, because an exemption a reader can see beats a pattern a reader has to decode",
        },
        fix="use $HOME / os.path.expanduser('~'), or AGENT_CONTEXT_STORE",
    ),
    Invariant(
        "side-effecting-script-parses-its-flags",
        "a script whose BARE RUN changes the machine must refuse an unknown flag and answer "
        "--help without doing the work",
        "A script with no argument parsing treats `--help` as a bare run and performs "
        "the side effect.",
        _side_effecting_scripts,
        _script_does_not_parse_flags,
        fix="parse sys.argv[1:] at the top of the entry point: print __doc__ and exit 0 for "
            "-h/--help, exit 2 naming the flag for anything unrecognized. See home-materialize.py, "
            "self-heal.py or refresh-intellij-server.py for the shape.",
    ),
    Invariant(
        "shell-true-quotes-its-interpolations",
        "a command built by interpolation and run with shell=True must shlex.quote every "
        "value it pastes in",
        "A path pasted unquoted into a shell command fails on a parenthesis, a space or "
        "a `$`, and the hook reports that as the project's own lint output.",
        lambda: _hooks("py") + _scripts("py"),
        _shell_true_unquoted,
        fix="wrap every interpolated value in shlex.quote() -- the file path AND the tool "
            "path. See post-edit-verify.py's `declared`/`detected` for the shape.",
    ),
    Invariant(
        "fleet-row-fields-earn-their-place",
        "every key the in-tree fleet row publishes is classified as static, state, "
        "unhealthy-only, bucketed or masked, and the bucketed/masked ones are wired",
        "A raw timestamp in a fleet row rewrites the row on every projection, and each "
        "rewrite is a commit every machine has to merge.",
        lambda: _server_source("fleet.py"),
        _fleet_row_fields_unaccounted,
        fix="add the key to _FLEET_ROW_FIELDS / _FLEET_ADOPTION_FIELDS in invariant-check.py "
            "with its churn class; bucket a timestamp with _BUCKET_SECS or list a "
            "display-only fact in fleet._VOLATILE_FIELDS",
    ),
    Invariant(
        "uvicorn-shares-the-daemon-log-format",
        "every uvicorn.Config(...) in the server passes log_config= so uvicorn's loggers "
        "propagate to the root handler and every log line carries the timestamp",
        "Undated log lines make a date-bounded review attribute old events to the "
        "window under review.",
        lambda: _server_source("server.py"),
        _uvicorn_config_without_log_config,
        fix="pass log_config=None (root-handler propagation) or a dict that reuses "
            "server._LOG_FORMAT",
    ),
    Invariant(
        "agents-search-stops-at-home",
        "an upward search for a .agents/ project marker stops at $HOME",
        "An upward search for `.agents` that reaches ~/.agents treats every session "
        "under home as a project.",
        lambda: _hooks("sh", "py") + _scripts("sh", "py"),
        _agents_search_passes_home,
        fix="add `[ \"$d\" != \"$HOME\" ]` (or the Python equivalent) to the loop condition",
    ),
    Invariant(
        "shell-is-launcher-only",
        "every shell file is a named launcher: POSIX sh, at most 25 code lines, setting env "
        "and running one program with exec; all other logic is Python",
        "Shell hooks that start python3 anyway pay for two interpreters, and a guard "
        "change spans two languages.",
        _shell_sites,
        _shell_is_not_a_launcher,
        allow=_SHELL_ALLOW,
        fix="port the file to Python; if it only sets env and runs one program with exec, "
            "make it POSIX sh and add its installed name to _LAUNCHERS.",
    ),
    Invariant(
        "python-bin-shebang-rendered",
        "every Python executable chezmoi installs into ~/.local/bin starts with the "
        "python-shebang template include, in a .tmpl source",
        "`#!/usr/bin/env python3` resolves to an old system interpreter under ssh and "
        "cron, and to none of ours under launchd.",
        _chezmoi_bin,
        _python_bin_shebang_not_rendered,
        fix="make line 1 `{{ template \"python-shebang\" . }}` and give the source a .tmpl "
            "suffix; the template renders /opt/homebrew/bin/python3.14 on macOS and "
            "~/.local/bin/python3.14 elsewhere",
    ),
    Invariant(
        "no-retired-script-reference",
        "no store code, config, sidecar or chezmoi file names a store .sh script that was "
        "ported to Python or retired",
        "A caller that tests for a script's file first skips with no message when the "
        "script is renamed.",
        _retired_ref_sites,
        _names_retired_script,
        allow={
            "test-invariant-retired-scripts.py": "this invariant's battery: names retired "
                                                 "scripts on purpose to prove they are caught",
            "test-invariant-project-scope.py": "this invariant's project-scope battery: "
                                               "names retired-shape stems in fixtures, and "
                                               "the real wt-sweep.sh shim name, on purpose "
                                               "to prove each is caught or correctly spared",
            "invariant-check.py": "this checker's own project-scope allowlist keys "
                                  "(example-api/wt-sweep.sh and siblings) and an existing "
                                  "allow reason both name a retired stem as DATA, never a "
                                  "runnable reference -- same shape as its no-hardcoded-home "
                                  "self-exemption above",
            "test-ralph-patch-guard.py": "proves the 2026-09-04 PATCH E, which sourced "
                                         "the retired stat helper, is re-patched away",
            "test_precommit_gate_install.py": "simulates the old wiring: a symlink to the "
                                              "retired gate that the launcher must replace",
            "project-settings-sync.py": "names each project's own wt-sweep.sh shim, kept as the "
                                        "marker that wires the sweep hooks (user, 2026-09-14)",
            "test-project-settings-sync.py": "fixtures of that marker shim and of the old "
                                             "shim-exec wiring the sync must replace",
            "test-project-settings-sync.py.meta.toml": "that battery's description names the "
                                                       "marker shim",
            "test-hook-dispatch.py": "synthetic refusal text: the old name only fills the "
                                     "[guard file] slot the firing report parses",
            "test-hook-dispatch-edges.py": "synthetic transcript lines: the old names only "
                                           "fill the [guard file] slot the firing report parses",
            "test-hook-parity-run.py": "fixture hooks written as shell files, to exercise the "
                                       "parity runner's shell-guard and neutralize paths",
        },
        fix="point the caller at the .py (run it with python3, drop the `bash`), or remove "
            "the reference if the script was retired with no port.",
    ),
    Invariant(
        "node-tools-pinned",
        "no store or chezmoi code runs npx or a global npm install (the pnpm bootstrap in "
        "install-packages aside), no MCP manifest server runs through npx or fetches @latest, "
        "and every tool in the store's pnpm manifest is `latest`",
        "An `npx -y` on an @latest package at session start pulls unreviewed code each "
        "time, so Node tools install through one synced manifest.",
        _node_sites,
        _runs_unpinned_node,
        allow=_NODE_ALLOW,
        fix="add the package at `latest` to global/node-tools/package.json, install it with "
            "node-tools-sync.py, and run its installed bin.",
    ),
    Invariant(
        "deps-manifest-valid",
        "the fleet dependency manifest (global/deps/manifest.toml) parses and passes "
        "deps-check.py's validator: known channels, a fix for every tool, defined roles, "
        "verify-only relay tools, no role that names a tool its machine's OS cannot run",
        "deps-check.py reads this manifest on every host, so an invalid one blinds the "
        "whole fleet check.",
        _deps_manifest_sites,
        _deps_manifest_invalid,
        fix="run `python3.14 ~/.agent-context/global/scripts/deps-check.py --no-record` and fix the "
            "manifest lines it prints.",
    ),
    Invariant(
        "deps-chezmoi-parity",
        "every name in chezmoi's package lists, its verified language servers and "
        "global/node-tools/package.json is a dependency manifest tool (through `package` or "
        "`node_package`) or an [unmanaged] entry with a reason, and every tool that names a package "
        "finds it there",
        "A tool added to chezmoi's install lists and not to the manifest never reaches "
        "the fleet check, or the reverse.",
        _deps_parity_sites,
        _deps_chezmoi_parity,
        fix="add the missing name to global/deps/manifest.toml (a tool with `package` or "
            "`node_package`, or an [unmanaged] entry with a reason), or remove the stale entry.",
    ),
    Invariant(
        "lock-record-matches-file",
        "a lock record's declared sha256 matches the file it names, in the checkout "
        "the record itself lives in",
        "Nothing else notices a lock record that has drifted from its file, whether by "
        "a hand-resolved rebase or an edit made around the lock.",
        _lock_record_sites,
        _lock_record_hash_mismatch,
        fix="re-lock the file with test-lock.py to record its current hash, or restore "
            "the file to the hash the record names",
    ),
    Invariant(
        "store-host-starts-the-bridge-from-the-clone",
        "the host with the store clone has no standalone relay at ~/.local/bin/agent-context",
        "Every harness prefers that file over the clone's script, and agent-context-refresh "
        "skips the store host, so a relay installed there is never updated: server-host's "
        "bridges ran code from 2026-09-25 for ten days, with no wake route (policy).",
        _standalone_relay_on_the_store_host,
        lambda path, src: "a standalone relay is installed on the store host, and nothing "
                          "refreshes it",
        fix="remove ~/.local/bin/agent-context, run `python3 ~/.agent-context/global/scripts/"
            "harness-materialize.py`, then `chezmoi apply ~/.codex/config.toml "
            "~/.config/opencode/opencode.json`; new sessions start the bridge from "
            "server/.venv/bin/agent-context",
    ),
]




NEEDS_STORE_CHECKOUT: dict[str, bool] = {
    "no-claude-projection-paths": False,
    "neutral-worktree-layout": True,
    "global-graph-file-links": True,
    "patch-writes-keep-body-bytes": True,
    "typed-links-validated-once": True,
    "upsert-carries-existing-keys": True,
    "no-superseded-jira-target": False,
    "no-typed-consent-instructions": False,
    "no-unsigned-commit-fallback": False,
    "ralph-refeed-is-a-pointer": False,
    "long-runs-are-observable": False,
    "autocommit-excludes-foreign-machine-rows": False,
    "observation-id-from-the-shared-allocator": True,
    "store-entity-body-not-executable": True,
    "plain-language-words-single-source": False,
    "agent-read-text-plain": False,
    "no-bare-store-exec-registration": False,
    "main-checkout-exception-scoped": False,
    "daemon-can-be-retired": False,
    "python-hook-crash-handler": False,
    "store-git-write-merge-guard": False,
    "health-writer-fail-path": False,
    "set-e-pipeline-guard": False,
    "enforcing-hook-has-test": False,
    "eval-case-grounded": False,
    "always-loaded-context-within-budget": True,
    "check-mode-can-fail": False,
    "memory-description-declares-its-carve-outs": True,
    "no-conflict-markers-in-entities": False,
    "store-writes-are-atomic": True,
    "fsync-stays-behind-one-switch": True,
    "one-atomic-writer": True,
    "projection-writes-are-atomic": False,
    "interpreter-is-rendered": False,
    "hook-readers-expand-dispatch": False,
    "codex-hooks-derived-from-claude": False,
    "codex-test-unlock-human-consent": False,
    "codex-content-parity": False,
    "decision-regex-is-shared": False,
    "worker-restates-uninheritable-rules": False,
    "worker-guardrails-match-agents-md": False,
    "worker-shared-text-identical": False,
    "agent-envelope-stated": False,
    "hook-wired-to-its-declared-event": False,
    "hook-fingerprint-matches": True,
    "store-host-starts-the-bridge-from-the-clone": True,
    "stop-hook-reaches-the-model": False,
    "embedded-python-is-heredoc-quoted": False,
    "stat-portability": False,
    "prose-guard-covers-every-store-writer": False,
    "hook-deploys-what-its-row-declares": True,
    "landing-gate-judges-relevance": True,
    "no-hardcoded-home": False,
    "side-effecting-script-parses-its-flags": False,
    "shell-true-quotes-its-interpolations": False,
    "fleet-row-fields-earn-their-place": True,
    "uvicorn-shares-the-daemon-log-format": True,
    "agents-search-stops-at-home": False,
    "shell-is-launcher-only": False,
    "python-bin-shebang-rendered": False,
    "no-retired-script-reference": False,
    "node-tools-pinned": False,
    "deps-manifest-valid": False,
    "deps-chezmoi-parity": True,
    "lock-record-matches-file": True,
}








LOCAL_SCOPE_IDS = {
    "hook-wired-to-its-declared-event",
    "hook-deploys-what-its-row-declares",
    "prose-guard-covers-every-store-writer",
    "python-bin-shebang-rendered",
    "node-tools-pinned",
    "deps-chezmoi-parity",
    "store-host-starts-the-bridge-from-the-clone",
}


def _has_store_checkout():
    'A store checkout has a .git entry or a server/ directory; a relay home has neither.'
    return (os.path.exists(os.path.join(STORE, ".git"))
            or os.path.isdir(os.path.join(STORE, "server")))


def _limit_set(paths):
    return {os.path.realpath(p) for p in paths} if paths else None


def main(argv):
    args = argv[1:]
    as_json = "--json" in args
    do_health = "--health" in args
    do_list = "--list" in args
    do_verify = "--verify" in args
    scope = args[args.index("--scope") + 1] if "--scope" in args else None
    limit = None
    if "--paths" in args:
        limit = _limit_set(args[args.index("--paths") + 1:])

    if do_list:
        for inv in REGISTRY:
            print("\n%s" % inv.id)
            print("  rule    : %s" % inv.rule)
            print("  incident: %s" % inv.incident)
            print("  sites   : %d, %d allowed" % (len(inv.sites()), len(inv.allow)))
        return 0

    skip_reasons = {}
    if not _has_store_checkout():
        for inv in REGISTRY:
            if NEEDS_STORE_CHECKOUT[inv.id]:
                skip_reasons[inv.id] = "a store checkout"
    if not os.path.isfile(_chezmoi_config_template()):
        skip_reasons.setdefault("deps-chezmoi-parity", "a chezmoi source")
    if scope == "store":
        for ident in LOCAL_SCOPE_IDS:
            skip_reasons.setdefault(ident, "the local scope (run with --scope local, or with no --scope)")
    elif scope == "local":
        for inv in REGISTRY:
            if inv.id not in LOCAL_SCOPE_IDS:
                skip_reasons.setdefault(inv.id, "the store scope (run with --scope store, or with no --scope)")
    skipped_ids = sorted(skip_reasons)

    if do_verify:
        rot = 0
        for inv in REGISTRY:
            if inv.id in skipped_ids:
                continue
            for base, why in inv.verify_allow():
                print("STALE ALLOW  %s: %s -- %s" % (inv.id, base, why))
                rot += 1
        control = _worker_shared_text_negative_control()
        if control:
            print("NEGATIVE CONTROL FAILED  worker-shared-text-identical: %s" % control)
            rot += 1
        control = _fingerprint_negative_control()
        if control:
            print("NEGATIVE CONTROL FAILED  hook-fingerprint-matches: %s" % control)
            rot += 1
        print("allowlists clean" if not rot else "%d stale allow entr(ies)" % rot)
        return 1 if rot else 0

    results, total = [], 0
    for inv in REGISTRY:
        if inv.id in skipped_ids:
            continue
        found, checked = inv.run(limit)
        total += checked
        if found:
            results.append({"invariant": inv.id, "rule": inv.rule,
                            "incident": inv.incident, "fix": inv.fix,
                            "violations": found})

    report = {"invariants": len(REGISTRY), "sites_checked": total,
              "skipped": skipped_ids, "skip_reasons": skip_reasons, "failing": results}
    _print_report(report, as_json)
    if do_health or (not results and limit is None and scope is None):
        _record_health(results)
    return 1 if results else 0


def _print_report(report, as_json):
    if as_json:
        print(json.dumps(report, indent=1))
        return
    results = report["failing"]
    if not results:
        print("invariant-check: %d invariants hold across %d site checks."
              % (report["invariants"] - len(report["skipped"]), report["sites_checked"]))
    for ident in report["skipped"]:
        print("  %s: skipped: needs %s" % (ident, report["skip_reasons"].get(ident, "a store checkout")))
    
    
    
    
    
    
    for r in results:
        print("\n=== %s -- %d violation(s) ===" % (r["invariant"], len(r["violations"])))
        print("  rule    : %s" % r["rule"])
        print("  incident: %s" % r["incident"])
        print("  fix     : %s" % r["fix"])
        for v in r["violations"]:
            print("    - %-34s %s" % (v["site"], v["detail"]))


def _verdict_failing():
    "True when this host's invariants verdict says a rule is broken."
    try:
        with open(os.path.join(hp.state_dir(), "health", "invariants.json"),
                  encoding="utf-8") as fh:
            return json.load(fh).get("ok") is False
    except (OSError, ValueError, AttributeError):
        return False





def _record_health(results):
    if not os.path.exists(HEALTH_RECORD):
        return
    if not results and not _verdict_failing():
        return
    if results:
        n = sum(len(r["violations"]) for r in results)
        detail = ("%d half-applied invariant(s) across %d rule(s): %s. These are "
                  "lessons this store already paid for that are not applied at "
                  "every site. Run: invariant-check.py"
                  % (n, len(results),
                     "; ".join("%s (%d)" % (r["invariant"], len(r["violations"]))
                               for r in results)))
        
        
        
        subprocess.run([sys.executable, HEALTH_RECORD, "invariants", "--fail", detail,
                        "--replace"], check=False)
    else:
        subprocess.run([sys.executable, HEALTH_RECORD, "invariants", "--ok"],
                       check=False)







def _forward_store_half(passthrough):
    
    return store_task.run("invariant-check", ["--scope", "store"] + passthrough, deadline=600)


def _run_and_merge(argv):
    
    
    
    
    
    args = argv[1:]
    if "--scope" in args or "--list" in args or "--verify" in args:
        return main(argv)             

    as_json = "--json" in args
    do_health = "--health" in args
    base = [a for a in args if a not in ("--json", "--health")]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([argv[0], "--scope", "local", "--json"] + base)
    local_report = json.loads(buf.getvalue())

    try:
        remote = _forward_store_half(["--json"] + base)
    except (store_task.store_mcp.StoreUnreachable, store_task.store_mcp.ToolError) as exc:
        sys.stderr.write("invariant-check: the store did not run its half (%s)\n" % exc)
        return store_task.UNREACHABLE_EXIT
    if remote.get("exit") not in (0, 1):
        sys.stderr.write(remote.get("stderr") or "")
        return int(remote.get("exit") or 1)
    remote_report = json.loads(remote.get("stdout") or "{}")

    merged = {
        
        "invariants": local_report.get("invariants") or remote_report.get("invariants", 0),
        "sites_checked": local_report.get("sites_checked", 0) + remote_report.get("sites_checked", 0),
        
        
        "skipped": sorted(set(local_report.get("skipped", [])) & set(remote_report.get("skipped", []))),
        "skip_reasons": {**remote_report.get("skip_reasons", {}), **local_report.get("skip_reasons", {})},
        "failing": local_report.get("failing", []) + remote_report.get("failing", []),
    }
    _print_report(merged, as_json)
    if do_health or (not merged["failing"] and "--paths" not in base):
        _record_health(merged["failing"])
    return 1 if merged["failing"] else 0


if __name__ == "__main__":
    if store_task.in_server() or not store_task.targets_live_store():
        sys.exit(main(sys.argv))
    sys.exit(_run_and_merge(sys.argv))
