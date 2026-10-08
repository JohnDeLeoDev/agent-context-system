#!/usr/bin/env python3
'Reader graph for the agent-context store: who reads each bundle file.\n\nFor every key in a bundle class (hooks, scripts, docs, skills, commands, agents,\nmanifests) list the files that reference it, with file:line evidence, so a later change\ncan prove which files must exist on disk.\n\nReader kinds:\n  exec     settings/manifest/config/launcher/unit entries, script or hook code that runs\n           or loads the file, a command\'s shell block reading a doc by path.\n  mention  documentation text, comments, bare names.\n\nLoader lines inside test files are listed (in_test_file) but do not make keys uncertain;\n"test_loader_effect" lists the keys whose reach that changed.\n\nVerdicts: needed-on-disk (an exec reader), uncertain (a dynamic reference such as an\nf-string, computed name, glob or directory listing could match the key),\nmention-only, no-reader-found. Self-references do not count.\n\nReach (report-only, from real roots): root-reachable (a chain of exec readers from a\nsettings/manifest/harness-config/launcher/unit entry or a command\'s shell block),\ntest-only (reached only through test files), unreachable, uncertain (a dynamic reference\ncould match; beats test-only and unreachable). Each key carries "chain", the shortest\nreader chain, and test-only/unreachable keys carry "reach_caveat" naming the roots not\ninspected. Top-level "loader_lines" lists every dynamic reference with the number of keys\nit could match; "unchecked_roots" adds the laptop, which --checked never removes.\n\nUsage:\n  store-reader-graph.py [--store DIR] [--home DIR] [--class NAME]... [--json]\n                        [--out FILE] [--checked MACHINE]...\n\n  --store    store root (default $AGENT_CONTEXT_STORE, else ~/.agent-context)\n  --home     home searched for harness configs and launchers (default $HOME)\n  --class    limit to one class; repeatable\n  --json     print the report as JSON\n  --out      also write the JSON report to FILE: under $HOME, outside the store\n  --checked  mark a machine as already checked; repeatable\n\nRead-only: the only file it ever writes is --out. Unreadable, binary and invalid JSON\nfiles go in the report\'s "unreadable" list and never stop the run. Dynamic references\ncount only when the same line names a bundle directory (hooks, scripts, docs, skills,\ncommands, agents).'

import argparse
import json
import os
import re
import socket
import sys
from collections import defaultdict, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp  
import store_task  


CD = hp.CLAUDE_DIRNAME
CJ = hp.CLAUDE_JSON_NAME

CLASSES = ("hooks", "scripts", "docs", "skills", "commands", "agents", "manifests")
DEFAULT_UNCHECKED = ["m4", "rp", "pc", "mirror-a", "mirror-b"]
LAPTOP_ROOT = "laptop launchers and units (not inspected)"
VERDICTS = ("needed-on-disk", "uncertain", "mention-only", "no-reader-found")
REACH_VALUES = ("root-reachable", "test-only", "unreachable", "uncertain")
TEST_PREFIXES = ("test-", "test_")
TEST_BASENAMES = ("hook-test-cases.py", "hook-test-run.py")

HOME_VIA = (
    (f"~/{CD}/", "root:settings"),
    ("~/.codex/", "root:harness-config"), ("~/.copilot/", "root:harness-config"),
    ("~/.config/opencode/", "root:harness-config"), ("~/.pi/", "root:harness-config"),
    ("~/Library/LaunchAgents/", "root:unit"), ("~/.config/systemd/", "root:unit"),
)
SKIP_DIRS = {".git", ".agents", "node_modules", "__pycache__", ".cache", ".venv", "venv",
             ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
                 ".woff", ".woff2", ".so", ".dylib", ".sqlite", "-shm", "-wal")
MAX_READERS_SHOWN = 20
DOC_SUFFIXES = (".md", ".txt", ".rst", ".jsonl")
CODE_SUFFIXES = (".sh", ".py", ".ts", ".js", ".mjs")

MANIFEST_ROOTS = ("global/hooks-manifest.json", "global/mcp-servers.json")
SHELL_DOC_VIA = (("global/commands/", "root:command-shell"), ("global/skills/", "root:skill-shell"),
                 ("global/agents/", "root:agent-shell"))
SCAN_BLIND_SPOTS = [
    "chezmoi source (~/.local/share/chezmoi)",
    "crontab",
    "shell rc files (.zshenv, .zshrc, .bash_profile, .profile)",
    f"~/{CD}/hooks, commands and scripts directories",
    "symlinked directories (not followed)",
    "systemd .service.d drop-in directories",
    f"~/{CJ}",
    "symlink launchers in ~/.local/bin",
    f"doc-typed areas (templates/, machines/, projects/*/{CD} configs)",
    "home markdown files",
    "inline !`cmd` and ~~~ / non-shell fences",
    "lists read via xargs (.txt, .md, .rst, .jsonl)",
    ".git/hooks in the store",
    "python3 -m and __import__ references",
    f"~/{CD}/plugins and other ~/{CD} subdirectories",
    "/etc/systemd/system and /etc/cron.d",
    f"per-repo {CD}/settings.json outside the store",
    "~/.config/fish, autostart and environment.d",
]
NOTICE = ("keep-list evidence only: this report lists what reads a file and must not be used "
          "to remove files; every unreachable or test-only key can still be live. "
          "known_gaps and known_gaps_extra are not exhaustive.")

KNOWN_GAPS = [
    ("G1", "test- prefixed files never count as roots, so a live script named test-* "
           "reads as test-only."),
    ("G2", "doc-typed store areas (templates, machines, projects) are never readers, so a "
           "real call from a config there is missed."),
    ("G3", "~~~ fences are not recognized, so shell inside them is never an exec reader."),
    ("G4", "inline !`cmd` spans in the middle of a line are not exec readers; only lines "
           "that start with ! count."),
    ("G5", "a python fence or any non-shell fence in a command, skill or agent is text, so "
           "calls made from it are missed."),
    ("G6", "frontmatter hook commands in a skill, command or agent are read outside a "
           "shell fence and count as mentions."),
    ("G7", "home .md files are doc-typed, so a launcher instruction in one is a mention."),
    ("G8", "systemd .service.d drop-in directories are not scanned, so a unit override "
           "that starts a file is missed."),
    ("G9", "symlinked directories and symlink launchers are not followed, so what they "
           "start is missed."),
    ("G10", "a dynamic reference (listdir, glob, computed name) counts only when its line "
            "names a bundle directory, so one built from a variable path is missed."),
    ("G11", "computed-name patterns know only the py, md, sh, json, ts, js and toml "
            "suffix set; other suffixes and names built across lines are missed."),
]
KNOWN_GAPS_EXTRA = [
    ("G12", "lists read via xargs from a .txt, .md, .rst or .jsonl file are not followed, so a "
            "file named only there is missed."),
    ("G13", "the .git/hooks directory in the store is skipped, so a hook installed there that "
            "starts a file is missed."),
    ("G14", "a python3 -m module run and __import__ calls are not resolved, so the file they "
            "load is missed."),
    ("G15", f"~/{CD}/plugins and other ~/{CD} subdirectories are not scanned, so a plugin "
            "that starts a file is missed."),
    ("G16", "system-level units and jobs (/etc/systemd/system, /etc/cron.d) and per-repo "
            f"{CD}/settings.json outside the store are not scanned, so what they start is "
            "missed."),
]
CAVEAT_POINTER = "see scan_blind_spots"
STORE_SIDECAR_SUFFIX = ".meta.toml"
STORE_TEXT_FILES = (".gitattributes", ".gitignore")

DATA_ROOTS = ("global/docs", "shared-docs", "global/audit-observations", "global/audit-observations-archive",
              "global/memory", "global/instructions", "global/state", "global/test-locks",
              "machines", "projects", "workspaces", "templates", ".obsidian")

SHELL_FENCES = {"", "bash", "sh", "shell", "zsh", "console"}

HOME_SOURCES = (
    (CD, False), (".codex", False), (".copilot", False),
    (".config/opencode", True), (".pi/agent", False), (".pi/agent/extensions", True), (".local/bin", False),
    ("Library/LaunchAgents", False), (".config/systemd/user", False),
    (".bashrc", False), (".zshrc", False), (".profile", False), (".zprofile", False),
)

TOKEN = re.compile(r"\w[\w.-]*")
PATH_FORM = re.compile(r"\b(skills|commands|agents)/([\w-]+)")
IMPORT_STATEMENT = re.compile(r"^\s*(import|from)\s+(.+)")
IMPORT_MODULE_LITERAL = re.compile(r"import_module\(\s*[\"']([\w.]+)[\"']")
IMPORT_MODULE_DYNAMIC = re.compile(r"import_module\(\s*[^\s\"')]")
FENCE = re.compile(r"^\s*```(\w*)")
CLASS_WORD = re.compile(r"(?<![A-Za-z])(hooks|scripts|docs|skills|commands|agents)(?![A-Za-z])",
                        re.IGNORECASE)
PATH_PART_CHARS = "/\"'{}$_"
EXTENSIONS = r"(py|md|sh|json|ts|js|toml)"

BUNDLE_DIRECTORIES = ("hooks", "scripts", "docs", "skills", "commands", "agents")
COMPUTED_NAME_PATTERNS = (
    ("computed name", re.compile(r"\+\s*[\"']\." + EXTENSIONS + r"[\"']")),
    ("placeholder before extension", re.compile(r"\}[\w-]*\." + EXTENSIONS + r"\b")),
    ("shell variable before extension", re.compile(r"\$\w+[\w-]*\." + EXTENSIONS + r"\b")),
    ("format placeholder", re.compile(r"%[sd][\w-]*\." + EXTENSIONS + r"\b")),
)

GLOB_NAME_PATTERN = ("glob pattern", re.compile(r"\*[\w-]*\." + EXTENSIONS + r"\b"))
LISTING_PATTERN = re.compile(
    r"\b(os\.listdir|os\.scandir|os\.walk|iterdir|glob\.glob|glob\.iglob|rglob|readdir|"
    r"fnmatch)\b|\bglob\(")


def make_source(path, label, origin):
    return {"path": path, "label": label, "origin": origin,
            "kind": source_kind(label, origin)}


def is_shell_doc_label(label):
    if label.startswith("global/skills/"):
        return os.path.basename(label) == "SKILL.md"
    return label.endswith(".md") and label.startswith(("global/commands/", "global/agents/"))


def source_kind(label, origin):
    'code: exec-capable; doc: mention only; shell-doc: exec only in shell blocks.'
    if origin == "store" and is_shell_doc_label(label):
        return "shell-doc"
    if label.endswith(DOC_SUFFIXES):
        return "doc"
    if origin == "store" and label.startswith(tuple(root + "/" for root in DATA_ROOTS)) \
            and label.endswith(CODE_SUFFIXES) and not label.startswith("global/"):
        return "code"
    if origin == "store" and (label.endswith(STORE_SIDECAR_SUFFIX) or label in STORE_TEXT_FILES
                              or label.startswith(tuple(root + "/" for root in DATA_ROOTS))):
        return "doc"
    return "code"


def list_files(base, recursive):
    found = []
    for directory, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        found.extend(os.path.join(directory, n) for n in sorted(names)
                     if not n.endswith(SKIP_SUFFIXES))
        if not recursive:
            break
    return found


def collect_store_sources(store):
    return [make_source(path, os.path.relpath(path, store).replace(os.sep, "/"), "store")
            for path in list_files(store, True)]


def collect_home_sources(home):
    sources = []
    for rel, recursive in HOME_SOURCES:
        base = os.path.join(home, rel)
        paths = [base] if os.path.isfile(base) else (
            list_files(base, recursive) if os.path.isdir(base) else [])
        for path in paths:
            label = "~/" + os.path.relpath(path, home).replace(os.sep, "/")
            sources.append(make_source(path, label, "home"))
    return sources


def files_under(store, rel, wanted):
    base = os.path.join(store, rel)
    if not os.path.isdir(base):
        return []
    return [os.path.relpath(p, store).replace(os.sep, "/") for p in list_files(base, True)
            if wanted(p)]


def is_bundle_file(path):
    return not path.endswith(STORE_SIDECAR_SUFFIX)


def discover_keys(store, classes):
    docs_root = "global/docs" if os.path.isdir(os.path.join(store, "global/docs")) \
        else "shared-docs"
    finders = {
        "hooks": lambda: files_under(store, "global/hooks", is_bundle_file),
        "scripts": lambda: files_under(store, "global/scripts", is_bundle_file),
        "docs": lambda: files_under(store, docs_root, is_bundle_file),
        "skills": lambda: files_under(store, "global/skills",
                                      lambda p: os.path.basename(p) == "SKILL.md"),
        "commands": lambda: files_under(store, "global/commands",
                                        lambda p: p.endswith(".md")),
        "agents": lambda: files_under(store, "global/agents", lambda p: p.endswith(".md")),
        "manifests": lambda: files_under(store, "global", lambda p: p.endswith(".json")
                                         and os.path.dirname(p) == os.path.join(store, "global")),
    }
    return {cls: sorted(finders[cls]()) for cls in classes}


def build_index(keys_by_class):
    'Lookup tables from a name seen in text to the keys it can refer to.'
    index = {"full": defaultdict(list), "bare": defaultdict(list),
             "path_form": defaultdict(list), "imports": defaultdict(list)}
    for cls, keys in keys_by_class.items():
        for key in keys:
            base = os.path.basename(key)
            stem = os.path.splitext(base)[0]
            if cls == "skills":
                name = os.path.basename(os.path.dirname(key))
                index["bare"][name].append((cls, key))
                index["path_form"][(cls, name)].append((cls, key))
                continue
            index["full"][base].append((cls, key))
            if cls in ("commands", "agents"):
                index["bare"][stem].append((cls, key))
                index["path_form"][(cls, stem)].append((cls, key))
            if cls in ("hooks", "scripts") and base.endswith(".py"):
                index["imports"][stem].append((cls, key))
    return index


def note_unreadable(unreadable, source, reason):
    unreadable.append({"file": source["label"], "reason": reason})


def read_text(source, unreadable):
    path = source["path"]
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        note_unreadable(unreadable, source, "unreadable: " + (error.strerror or str(error)))
        return None
    if b"\0" in raw[:8192]:
        note_unreadable(unreadable, source, "binary")
        return None
    
    text = raw.decode("utf-8", errors="replace")
    if path.endswith(".json"):
        try:
            json.loads(text)
        except ValueError:
            
            note_unreadable(unreadable, source, "invalid JSON")
    return text


def is_comment(line):
    stripped = line.lstrip()
    return (stripped.startswith("#") and not stripped.startswith("#!")) or \
        stripped.startswith(("//", "/*"))


def code_exec_flags(lines):
    "Comments are '#', '//' and /* */ blocks; a '*' line outside a block is code."
    flags, in_block = [], False
    for line in lines:
        if in_block:
            flags.append(False)
            in_block = "*/" not in line
        elif line.lstrip().startswith("/*"):
            flags.append(False)
            in_block = "*/" not in line.lstrip()[2:]
        else:
            flags.append(not is_comment(line))
    return flags


def is_shell_command_line(line):
    stripped = line.lstrip()
    return stripped.startswith("!") and not stripped.startswith("![")


def exec_flags(source, lines):
    'Per line: True when a reference there runs or loads the file.'
    if source["kind"] == "doc":
        return [False] * len(lines)
    if source["kind"] == "shell-doc":
        flags, fence = [], None
        for line in lines:
            match = FENCE.match(line)
            if match:
                fence = None if fence is not None else match.group(1).lower()
                flags.append(False)
            else:
                flags.append(fence in SHELL_FENCES or is_shell_command_line(line))
        return flags
    return code_exec_flags(lines)


def is_self_reference(source, cls, key):
    if source["origin"] != "store":
        return False
    label = source["label"]
    if label == key or label == key + STORE_SIDECAR_SUFFIX:
        return True
    return cls == "skills" and label.startswith(os.path.dirname(key) + "/")


def imported_names(line):
    'Module names a Python line imports: \'import a, b\', \'from x import y\', import_module("m").'
    names = set()
    match = IMPORT_STATEMENT.match(line)
    if match:
        if match.group(1) == "from":
            names.update(re.findall(r"\w+", match.group(2)))
        else:
            names.update(part.split()[0].split(".")[0] for part in match.group(2).split(",")
                         if part.split())
    for module in IMPORT_MODULE_LITERAL.findall(line):
        names.update(module.split("."))
    return names


def references_in_line(line, index):
    'Yield (cls, key, exec_capable) for each key the line names.'
    seen = set()
    for token in TOKEN.findall(line):
        token = token.rstrip(".")
        for target in index["full"].get(token, ()):
            seen.add((target, True))
        for target in index["bare"].get(token, ()):
            seen.add((target, False))
    for word, name in PATH_FORM.findall(line):
        for target in index["path_form"].get((word, name), ()):
            seen.add((target, True))
    for name in imported_names(line):
        for target in index["imports"].get(name, ()):
            seen.add((target, True))
    for (cls, key), exec_capable in seen:
        yield cls, key, exec_capable


def directory_names_in(line):
    'Bundle directory names used as a path part or variable, not as a word in prose.'
    names = set()
    for match in CLASS_WORD.finditer(line):
        before = line[match.start() - 1] if match.start() else ""
        after = line[match.end()] if match.end() < len(line) else ""
        if before in PATH_PART_CHARS or after in PATH_PART_CHARS:
            names.add(match.group(1).lower())
    return names


def dynamic_reference(line):
    'Return (reason, classes, extensions or None) when the line builds names at runtime.'
    classes = directory_names_in(line)
    patterns = COMPUTED_NAME_PATTERNS + ((GLOB_NAME_PATTERN,) if classes else ())
    reasons, extensions = [], set()
    for reason, pattern in patterns:
        for match in pattern.finditer(line):
            reasons.append(reason)
            extensions.add(match.group(1))
    if extensions:
        
        return ", ".join(sorted(set(reasons))), classes or set(BUNDLE_DIRECTORIES), extensions
    if classes and LISTING_PATTERN.search(line):
        return "directory listing or glob", classes, None
    if IMPORT_MODULE_DYNAMIC.search(line):
        return "dynamic import", {"hooks", "scripts"}, {"py"}
    return None


def snippet_of(line):
    return line.strip()[:160]


def via_of(source):
    'How a source runs what it names: a root kind, or script for code run by other code.'
    label = source["label"]
    if source["origin"] == "home":
        return next((via for prefix, via in HOME_VIA if label.startswith(prefix)),
                    "root:launcher")
    if source["kind"] == "shell-doc":
        return next(via for prefix, via in SHELL_DOC_VIA if label.startswith(prefix))
    if label in MANIFEST_ROOTS:
        return "root:manifest"
    return "script"


def scan_sources(sources, index, unreadable):
    readers = defaultdict(dict)
    dynamic = []
    files_searched = 0
    for source in sources:
        text = read_text(source, unreadable)
        if text is None:
            continue
        files_searched += 1
        lines = text.splitlines()
        via = via_of(source)
        for number, (line, exec_context) in enumerate(zip(lines, exec_flags(source, lines)), 1):
            for cls, key, exec_capable in references_in_line(line, index):
                if is_self_reference(source, cls, key):
                    continue
                kind = "exec" if exec_capable and exec_context else "mention"
                slot = readers[(cls, key)]
                if kind == "exec" or (source["label"], number) not in slot:
                    slot[(source["label"], number)] = {
                        "kind": kind, "file": source["label"], "line": number,
                        "snippet": snippet_of(line),
                        "via": via if kind == "exec" else "mention"}
            found = dynamic_reference(line) if exec_context else None
            if found:
                reason, classes, extensions = found
                dynamic.append({"file": source["label"], "line": number,
                                "snippet": snippet_of(line), "reason": reason,
                                "classes": classes, "extensions": extensions})
    return readers, dynamic, files_searched


def uncertain_sources_for(cls, key, source_label, dynamic_refs):
    extension = os.path.splitext(key)[1].lstrip(".").lower()
    found = []
    for ref in dynamic_refs:
        if cls not in ref["classes"] or ref["file"] == source_label:
            continue
        if ref["extensions"] is not None and extension not in ref["extensions"]:
            continue
        found.append({"file": ref["file"], "line": ref["line"],
                      "snippet": ref["snippet"], "reason": ref["reason"]})
    return found


def verdict_for(exec_count, uncertain_count, reader_count):
    if exec_count:
        return "needed-on-disk"
    if uncertain_count:
        return "uncertain"
    return "mention-only" if reader_count else "no-reader-found"


def is_test_file(label):
    base = os.path.basename(label)
    return base.startswith(TEST_PREFIXES) or base in TEST_BASENAMES


def exec_edges(readers):
    'source file label -> [(target, reader record)], for exec readers only.'
    forward = defaultdict(list)
    for target, slot in readers.items():
        for record in sorted(slot.values(), key=lambda r: (r["file"], r["line"])):
            if record["kind"] == "exec":
                forward[record["file"]].append((target, record))
    return forward


def step_of(record):
    return {"file": record["file"], "line": record["line"], "snippet": record["snippet"],
            "via": record["via"]}


def shortest_chains(seeds, forward, may_expand):
    'Breadth-first from seeds ({target: one-step chain}); the first chain to a target wins.'
    chains = dict(seeds)
    queue = deque(sorted(seeds))
    while queue:
        node = queue.popleft()
        if not may_expand(node[1]):
            continue
        for target, record in forward.get(node[1], ()):
            if target not in chains:
                chains[target] = chains[node] + [step_of(record)]
                queue.append(target)
    return chains


def seed_chains(readers, wanted):
    seeds = {}
    for target, slot in sorted(readers.items()):
        for record in sorted(slot.values(), key=lambda r: (r["file"], r["line"])):
            if record["kind"] == "exec" and wanted(record):
                seeds.setdefault(target, [step_of(record)])
    return seeds


def is_root_record(record, unkeyed_code):
    'A root reader is a harness/launcher/unit/manifest/command entry, or store code that\n    is no bundle key (its own callers are unknowable here, so it counts as a root).'
    if is_test_file(record["file"]):
        return False
    return record["via"].startswith("root:") or record["file"] in unkeyed_code


def reach_of(node, uncertain_count, root_chains, test_chains):
    if node in root_chains:
        return "root-reachable"
    if uncertain_count:
        return "uncertain"
    return "test-only" if node in test_chains else "unreachable"


def analyze_reach(all_keys, readers, uncertain, unkeyed_code):
    'Return {node: (reach, chain)} where node is (cls, key).'
    forward = exec_edges(readers)
    root_chains = shortest_chains(
        seed_chains(readers, lambda r: is_root_record(r, unkeyed_code)), forward,
        lambda label: not is_test_file(label))
    test_chains = shortest_chains(
        seed_chains(readers, lambda r: is_test_file(r["file"])), forward, lambda label: True)
    result = {}
    for cls, keys in all_keys.items():
        for key in keys:
            node = (cls, key)
            reach = reach_of(node, len(uncertain[node]), root_chains, test_chains)
            chain = root_chains.get(node) if reach == "root-reachable" else test_chains.get(node)
            if chain is None:
                chain = [step_of(r) for r in sorted(
                    (r for r in readers[node].values() if r["kind"] == "exec"),
                    key=lambda r: (r["file"], r["line"]))[:1]]
            result[node] = (reach, chain)
    return result


def annotate_steps(analysis):
    'Give every chain step the reach of the file it names; roots outside the keys are reachable.'
    reach_by_label = {key: reach for (_cls, key), (reach, _chain) in analysis.items()}
    for _reach, chain in analysis.values():
        for step in chain:
            step["reach"] = reach_by_label.get(step["file"], "root-reachable")


def unchecked_roots_for(unchecked):
    return list(unchecked) + [LAPTOP_ROOT]


def reach_caveat_for(unchecked_roots):
    return ("no path from inspected roots; not checked on: " + ", ".join(unchecked_roots)
            + "; " + CAVEAT_POINTER)


def caveat_for(verdict, reach, unchecked, unchecked_roots):
    if verdict == "no-reader-found":
        
        return ("no reader on this machine; not checked on: " + ", ".join(unchecked_roots)
                if unchecked else "")
    return reach_caveat_for(unchecked_roots) if reach in ("test-only", "unreachable") else ""


def build_key_entry(key_readers, uncertain_via, reach, chain, unchecked, unchecked_roots):
    ordered = sorted(key_readers, key=lambda r: (r["kind"] != "exec", r["file"], r["line"]))
    exec_count = sum(1 for r in ordered if r["kind"] == "exec")
    verdict = verdict_for(exec_count, len(uncertain_via), len(ordered))
    entry = {"verdict": verdict, "reader_count": len(ordered), "exec_count": exec_count,
             "readers": ordered[:MAX_READERS_SHOWN],
             "uncertain_via": uncertain_via[:MAX_READERS_SHOWN],
             "reach": reach, "chain": chain}
    caveat = caveat_for(verdict, reach, unchecked, unchecked_roots)
    if caveat:
        entry["caveat"] = caveat
    if reach in ("test-only", "unreachable"):
        entry["reach_caveat"] = reach_caveat_for(unchecked_roots)
    return entry


def loader_lines_of(dynamic, uncertain):
    matched = defaultdict(int)
    for refs in uncertain.values():
        for ref in refs:
            matched[(ref["file"], ref["line"])] += 1
    return [{"file": ref["file"], "line": ref["line"], "snippet": ref["snippet"],
             "matched_keys": matched[(ref["file"], ref["line"])],
             "in_test_file": is_test_file(ref["file"])}
            for ref in sorted(dynamic, key=lambda r: (r["file"], r["line"]))]


def unkeyed_store_code(sources, all_keys):
    keys = {key for keys in all_keys.values() for key in keys}
    return {s["label"] for s in sources if s["origin"] == "store" and s["kind"] == "code"
            and s["label"] not in keys and not is_test_file(s["label"])}


def uncertain_for_all_keys(all_keys, dynamic):
    return {(cls, key): uncertain_sources_for(cls, key, key, dynamic)
            for cls, keys in all_keys.items() for key in keys}


def test_loader_effect_of(analysis, analysis_with_tests, uncertain_with_tests):
    'Keys whose reach differs once test-file loader lines stop counting, with those lines.'
    changed = {}
    for node, (reach, _chain) in analysis.items():
        if analysis_with_tests[node][0] != reach:
            changed[node[1]] = [ref for ref in uncertain_with_tests[node]
                                if is_test_file(ref["file"])]
    return {"keys_changed": len(changed), "keys": changed}


def build_report(store, home, classes, unchecked):
    
    all_keys = discover_keys(store, CLASSES)
    index = build_index(all_keys)
    unreadable = []
    sources = collect_store_sources(store) + collect_home_sources(home)
    readers, dynamic, files_searched = scan_sources(sources, index, unreadable)
    unkeyed_code = unkeyed_store_code(sources, all_keys)
    
    uncertain_with_tests = uncertain_for_all_keys(all_keys, dynamic)
    uncertain = uncertain_for_all_keys(
        all_keys, [ref for ref in dynamic if not is_test_file(ref["file"])])
    analysis = analyze_reach(all_keys, readers, uncertain, unkeyed_code)
    effect = test_loader_effect_of(
        analysis, analyze_reach(all_keys, readers, uncertain_with_tests, unkeyed_code),
        uncertain_with_tests)
    annotate_steps(analysis)
    unchecked_roots = unchecked_roots_for(unchecked)
    report = {"host": socket.gethostname(), "store": store, "home": home,
              "unchecked_machines": unchecked, "unchecked_roots": unchecked_roots,
              "loader_lines": loader_lines_of(dynamic, uncertain_with_tests),
              "test_loader_effect": effect, "scan_blind_spots": SCAN_BLIND_SPOTS,
              "removal_safe": False, "notice": NOTICE,
              "known_gaps": [{"id": gap_id, "summary": summary}
                             for gap_id, summary in KNOWN_GAPS],
              "known_gaps_extra": [{"id": gap_id, "summary": summary}
                                   for gap_id, summary in KNOWN_GAPS_EXTRA],
              "classes": {}, "unreadable": unreadable}
    for cls in classes:
        keys = {key: build_key_entry(list(readers[(cls, key)].values()), uncertain[(cls, key)],
                                     *analysis[(cls, key)], unchecked, unchecked_roots)
                for key in all_keys[cls]}
        counts = {verdict: 0 for verdict in VERDICTS}
        reach_counts = {value: 0 for value in REACH_VALUES}
        for entry in keys.values():
            counts[entry["verdict"]] += 1
            reach_counts[entry["reach"]] += 1
        report["classes"][cls] = {"files_searched": files_searched, "key_count": len(keys),
                                  "verdicts": counts, "reach_counts": reach_counts,
                                  "keys": keys}
    return report


def render_header(report):
    lines = [report["notice"], "removal_safe: false", "unchecked roots:"]
    lines.extend("  " + root for root in report["unchecked_roots"])
    lines.append("scan blind spots:")
    lines.extend("  " + spot for spot in report["scan_blind_spots"])
    lines.append("known gaps: %d %s" % (len(report["known_gaps"]),
                                        " ".join(gap["id"] for gap in report["known_gaps"])))
    lines.extend("  %s: %s" % (gap["id"], gap["summary"]) for gap in report["known_gaps"])
    extra = report["known_gaps_extra"]
    lines.append("known gaps (more): %d %s" % (len(extra), " ".join(gap["id"] for gap in extra)))
    lines.extend("  %s: %s" % (gap["id"], gap["summary"]) for gap in extra)
    return lines


def detail_of(entry):
    'One line under a key: its reach caveat and, for no-reader-found, its caveat.'
    parts = []
    if "reach_caveat" in entry:
        parts.append("reach %s: %s" % (entry["reach"], entry["reach_caveat"]))
    if entry["verdict"] == "no-reader-found" and entry.get("caveat"):
        parts.append("caveat: " + entry["caveat"])
    return "; ".join(parts)


def render_text(report):
    lines = render_header(report)
    lines += ["host: " + report["host"], "store: " + report["store"],
              "unchecked machines: " + (", ".join(report["unchecked_machines"]) or "none")]
    for cls, section in report["classes"].items():
        counts = ", ".join("%s %d" % (v, n) for v, n in section["verdicts"].items())
        reach = " ".join("%s=%d" % (v, n) for v, n in section["reach_counts"].items())
        lines.append("%s: %d keys, %d files searched; %s; %s"
                     % (cls, section["key_count"], section["files_searched"], counts, reach))
        for key, entry in section["keys"].items():
            first = entry["readers"][0] if entry["readers"] else None
            evidence = " (%s:%d)" % (first["file"], first["line"]) if first else ""
            lines.append("  %-15s %s%s" % (entry["verdict"], key, evidence))
            detail = detail_of(entry)
            if detail:
                lines.append("      " + detail)
    changed = report["test_loader_effect"]["keys_changed"]
    if changed:
        lines.append("test-file loaders: %d key(s) no longer uncertain" % changed)
    if report["unreadable"]:
        lines.append("unreadable: %d" % len(report["unreadable"]))
        lines.extend("  %s: %s" % (u["file"], u["reason"]) for u in report["unreadable"])
    return "\n".join(lines)


def home_directory():
    return os.path.realpath(os.environ.get("HOME") or os.path.expanduser("~"))


def inside_home(path):
    home = home_directory()
    target = os.path.realpath(path)
    return target == home or target.startswith(home + os.sep)


def inside_store(path, store):
    root = os.path.realpath(store)
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


def hidden_home_entry_refused(parts):
    'Deny every hidden top-level entry of $HOME and Library, except .cache and .local/state.'
    first = parts[0].casefold()
    if first == "library":
        return True
    if not first.startswith("."):
        return False
    if first == ".cache":
        return len(parts) < 2
    if first == ".local":
        return not (len(parts) > 2 and parts[1].casefold() == "state")
    return True


def in_protected_home_area(path):
    home = home_directory()
    target = os.path.realpath(path)
    if target == home:
        return True
    return hidden_home_entry_refused(os.path.relpath(target, home).split(os.sep))


def in_scanned_root(path, scan_home):
    'Anything under a directory this tool reads as a root would feed itself back in.'
    target = os.path.realpath(path)
    for rel, _recursive in HOME_SOURCES:
        root = os.path.realpath(os.path.join(scan_home, rel))
        if target == root or target.startswith(root + os.sep):
            return True
    return False


def out_refusal(out_path, store, scan_home):
    'Return why --out is refused, or None. Runs before any directory is looked at.'
    if out_path.endswith(os.sep) or os.path.basename(out_path) in ("", ".", ".."):
        return "--out must name a file, not a directory"
    if os.path.lexists(out_path):
        return "--out target already exists"
    if not inside_home(out_path):
        return "--out must be under $HOME"
    if inside_store(out_path, store):
        return "--out must be outside the store"
    if in_protected_home_area(out_path):
        return "--out must not be in a hidden $HOME entry (except .cache and .local/state)"
    if in_scanned_root(out_path, scan_home):
        return "--out must not be in a directory this tool scans"
    return None


def remove_out_temp(out_temp):
    try:
        os.unlink(out_temp)
    except OSError:
        pass


def write_out(report, out_path):
    'Return an error message, or None once the report is published.'
    target = os.path.abspath(out_path)
    out_temp = os.path.join(os.path.dirname(target), ".%s.%d.%s.tmp" % (
        os.path.basename(target), os.getpid(), os.urandom(4).hex()))
    out_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    try:
        out_descriptor = os.open(out_temp, out_flags, 0o600)
    except OSError as error:
        return "cannot write --out: %s" % (error.strerror or error)
    try:
        with os.fdopen(out_descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        
        os.link(out_temp, target)
    except OSError as error:
        remove_out_temp(out_temp)
        return "cannot write --out: %s" % (error.strerror or error)
    remove_out_temp(out_temp)
    return None


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="store-reader-graph.py", allow_abbrev=False,
        description="Reader graph for the agent-context store: keep-list evidence only.")
    parser.add_argument("--store")
    parser.add_argument("--home")
    parser.add_argument("--class", dest="classes", action="append", choices=CLASSES)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--checked", action="append", default=[])
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    store = args.store or os.environ.get("AGENT_CONTEXT_STORE") \
        or os.path.expanduser("~/.agent-context")
    home = args.home or os.environ.get("HOME") or os.path.expanduser("~")
    refusal = out_refusal(args.out, store, home) if args.out else None
    if refusal:
        print("store-reader-graph.py: " + refusal, file=sys.stderr)
        return 2
    classes = [c for c in CLASSES if not args.classes or c in args.classes]
    unchecked = [m for m in DEFAULT_UNCHECKED if m not in args.checked]
    report = build_report(store, home, classes, unchecked)
    problem = write_out(report, args.out) if args.out else None
    if problem:
        print("store-reader-graph.py: " + problem, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    _pre = parse_args(sys.argv[1:])
    _store = _pre.store or os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
    store_task.main_or_forward("store-reader-graph", lambda: main(sys.argv[1:]), store=_store)
