#!/usr/bin/env python3

'PreToolUse(Bash): the Bash-side counterpart to require-worktree-edit.\n\nWhy this exists. require-worktree-edit matches on\nWrite|Edit|MultiEdit|NotebookEdit, so it cannot see a file written by a heredoc,\na `sed -i`, or a `>` redirect. The harness itself, in bypass-permissions mode,\ntells agents to prefer sed/heredocs over the Edit/Write tools -- an agent that\ncomplies routes every write around the worktree mandate. This hook extracts\nwrite targets from the command and runs each one through the same policy as\nrequire-worktree-edit.py, so every one\nof that policy\'s exemptions applies here automatically.\n\nFast path (performance only; this hook runs on every Bash call): a payload containing none\nof a fixed vocabulary of redirects/writer-names/interpreter-names cannot\nproduce a write target for either check below, so it is allowed without\nstarting the heavier parse.\n\nStore entities are never edited through the shell. A store entity is\na file on disk and a row in the server\'s in-memory index; a shell write updates\nonly the first, so the store keeps serving the old body until the next MCP\nwrite reverts the change with no error. Checked two ways: `store_interpreter_write`\ncatches `python3 - <<PY ... open(path, "w") ... PY`, which the target extractor\nbelow cannot see because it drops heredoc bodies (their words are\ndata, not arguments); the extracted-targets loop catches every\nother write shape (redirect, sed -i, tee, cp/mv).\n\nThe extractor. Regexes over the raw\ncommand text read shell syntax out of context -- an fd-dup misread as a\nredirect, a `>` inside a quoted string, a heredoc body scanned for redirects.\nNo regex fixes that, because the same character means\ndifferent things depending on quote/heredoc state, which a regex does not\ncarry. So the targets come from `shell-command-scan.write_targets`, the shared, hardened\nscanner every write guard on the fleet reads from. The module and this hook\nare projected by the same step, so a failed import means the projection\nitself is broken; this hook fails open on that, with a stderr note, the same\nas block-write-outside-home.py.\n\nFails open on any parse error: a guard that cannot read a command must not\nbrick every Bash call. Its correctness rests on\nscripts/test-require-worktree-edit-bash.py.'
import importlib.util
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

FAST_PATH = re.compile(
    r'>|of=|(^|[^A-Za-z0-9_./-])(tee|sed|perl|ruby|python3?|gsed|cp|mv|install|rsync|ln|'
    r'truncate|shred)([^A-Za-z0-9_./-]|$)', re.MULTILINE)

ENTITY_PATTERNS = (
    "*/.agent-context/*/hooks/*", "*/.agent-context/*/scripts/*",
    "*/.agent-context/*/agents/*", "*/.agent-context/*/commands/*",
    "*/.agent-context/*/skills/*", "*/.agent-context/*/docs/*",
    "*/.agent-context/*/memory/*", "*/.agent-context/*/instructions/*",
)


def glob_match(pattern, s):
    'Match a shell `case` glob (only `*` is special, and it matches `/` too).'
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, s) is not None


def any_glob(patterns, s):
    return any(glob_match(p, s) for p in patterns)




_KINDS = r"(hooks|scripts|agents|commands|skills|docs|memory|instructions)/"



STORE_RE = re.compile(r"(\.agent-context/)?global/" + _KINDS +
                      r"|\.agent-context/(projects|workspaces)/[^/]+/" + _KINDS)

TEMPLATE_RE = re.compile(r"/\.agent-context/(?:\.agents/worktrees/[^/]+/)?templates/")
WRITE_RE = re.compile(r"\.write\(|\.write_text\(|,\s*[\"'][wa][\"']|open\([^)]*[\"'][wa]")
INTERP_RE = re.compile(r"\b(python3?|perl|ruby)\b")


def store_check_segments(cmd):
    'Split on newline/&&/;, keeping a heredoc body glued to its opener -- the\n    recurring shape assigns the path on one line and opens it forty lines later\n    inside the same heredoc, and splitting naively on newlines would miss it.'
    segments, cur, term = [], [], None
    for line in cmd.splitlines():
        if term is not None:
            cur.append(line)
            if line.strip() == term:
                term = None
            continue
        m = re.search(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", line)
        cur.append(line)
        if m:
            term = m.group(1)
            continue
        segments.append("\n".join(cur))
        cur = []
    if cur:
        segments.append("\n".join(cur))
    flat = []
    for seg in segments:
        flat.extend(re.split(r"&&|;", seg) if "<<" not in seg else [seg])
    return flat





_ASSIGN_RE = re.compile(r"""^\s*([A-Za-z_]\w*)\s*=\s*(?:Path\(\s*)?(['"])([^'"]+)\2""", re.M)
_OPEN_W_RE = re.compile(r"""\bopen\(\s*([^,()]+?)\s*,\s*(?:mode\s*=\s*)?['"][wax]""")
_WRITE_TEXT_RE = re.compile(r"""(?:\bPath\(\s*([^()]+?)\s*\)|\b([A-Za-z_]\w*))\.write_(?:text|bytes)\(""")
_LITERAL_RE = re.compile(r"""(['"])([^'"]*)\1""")


def interpreter_write_targets(seg):
    '[(resolved, target)] for each write call in `seg` whose target is visible:\n    resolved is False when the target is neither a literal nor a name assigned one.'
    names = {m.group(1): m.group(3) for m in _ASSIGN_RE.finditer(seg)}
    exprs = [m.group(1) for m in _OPEN_W_RE.finditer(seg)]
    exprs += [m.group(1) or m.group(2) for m in _WRITE_TEXT_RE.finditer(seg)]
    out = []
    for expr in (e.strip() for e in exprs):
        lit = _LITERAL_RE.fullmatch(expr)
        if lit:
            out.append((True, lit.group(2)))
        elif expr in names:
            out.append((True, names[expr]))
        else:
            out.append((False, expr))
    return out


def store_interpreter_write(cmd):
    "True when an interpreter segment naming a store path writes into the store.\n\n    Each write call's target is resolved (a literal, or a name the segment assigns a\n    literal) and only a store target is refused, so a script that reads the store and\n    writes a scratch file passes. A target that cannot be resolved, or a\n    write shape with no visible target, keeps the blunt refusal: split the command."
    for seg in store_check_segments(cmd):
        if not (INTERP_RE.search(seg) and STORE_RE.search(seg)):
            continue
        targets = interpreter_write_targets(seg)
        if not targets:
            if WRITE_RE.search(seg):
                return True
            continue
        if any(not resolved or STORE_RE.search(t) for resolved, t in targets):
            return True
    return False





def extract_targets(cmd, cwd):
    'shell-command-scan.write_targets, the shared, hardened scanner every write\n    guard on the fleet reads from. Raises on any parse error; main() below fails\n    the whole guard open when that happens, with a stderr note, matching\n    block-write-outside-home.py. The module and this hook are projected by the\n    same step, so a failed import means the projection itself is broken.'
    scan_path = os.environ.get("SHELL_COMMAND_SCAN") or os.path.join(
        hp.scripts_dir(), "shell-command-scan.py")
    spec = importlib.util.spec_from_file_location("scs", scan_path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + scan_path)
    scs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scs)
    return scs.write_targets(cmd, cwd)


STORE_INTERPRETER_DENY = """BLOCKED by require-worktree-edit (store side): this looks like an interpreter script
writing an agent-context STORE ENTITY.

A store entity is a file on disk and a row in the server's in-memory index. A shell or
python write updates only the first, so the store keeps serving the old body and the next
edit_body or upsert reverts your work -- with no error when it happens.

Use the MCP tools, which write both:
  edit_body(kind=..., key=..., old_string=..., new_string=...)   one exact occurrence
  bulk_edit(...)                                                 several at once
  upsert_hook / upsert_script / upsert_doc / upsert_memory       whole body or new entity

kind is hook | script | doc | memory | instruction | skill | command; the key is the
entity name without its extension (eval-cases).

False positive? This fires on an interpreter that mentions a store path and writes
something, because the two are usually the same file and proving otherwise needs
static analysis. If your script only reads the store and writes somewhere else, split it
into two commands -- the read half will pass untouched.
"""

STORE_HITS_DENY = """BLOCKED by require-worktree-edit (store side): this command writes an agent-context
STORE ENTITY through the shell:
%s
A store entity is a file on disk and a row in the server's in-memory index. The shell
updates only the first, so the store keeps serving the old body and the next edit_body
or upsert reverts your work -- with no error at the moment it happens.

Use the MCP tools, which write both:
  edit_body(kind=..., key=..., old_string=..., new_string=...)   one exact occurrence
  bulk_edit(...)                                                 several at once
  upsert_hook / upsert_script / upsert_doc / upsert_memory       whole body or new entity

kind is hook | script | doc | memory | instruction | skill | command, and the key is the
entity name without its extension (eval-cases).

Reading is unaffected -- grep and Read work normally. This blocks writes only.
"""

BLOCKED_DENY = """BLOCKED by require-worktree-edit (Bash side): this command writes project source
in the main checkout, via a shell redirect / sed -i / tee / heredoc:
%s
Writing through the shell does not make the worktree mandate not apply -- it only
makes it invisible to the Edit-side hook. Do this instead:
  git worktree add .agents/worktrees/<short-desc> -b <short-desc>
  # edit there, with the Edit/Write tools, then:
  bash <main>/.agents/scripts/wt-finish.sh

NOTE: if your environment told you to prefer sed/heredocs over the Edit and Write
tools, that instruction does not apply to project source. Use Edit/Write inside a
worktree; the shell is still the right tool for reading, searching and building.

Exempt as always: *.md docs, .claude/, .agents/, .agent-context/, Secrets/, .git/,
gitignored paths (build output), scratch clones under a temp root, and anything
already inside a linked worktree.
"""


def main():
    raw = sys.stdin.read()
    if not FAST_PATH.search(raw):
        return 0

    policy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "require-worktree-edit.py")
    if not os.path.exists(policy):
        
        
        sys.stderr.write(
            "require-worktree-edit-bash: failing open -- policy sibling not found:\n"
            "  %s\n"
            "  This guard is disabled. If you are testing it, run the\n"
            "  store copy under ~/.agent-context/global/hooks/ instead.\n" % policy)
        return 0

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    ti = data.get("tool_input") or data.get("tool_args") or data.get("params") or {}
    if not isinstance(ti, dict):
        ti = {}
    cmd = ti.get("command") or ti.get("cmd") or ""
    cwd = data.get("cwd") or os.getcwd()
    if not isinstance(cmd, str) or not cmd:
        return 0
    cmd = cmd.replace("\\\n", " ")

    
    
    ti_raw = data.get("tool_input")
    ti_narrow = ti_raw if isinstance(ti_raw, dict) else {}
    cmd_for_store = ti_narrow.get("command") or ""
    if store_interpreter_write(cmd_for_store):
        sys.stderr.write(STORE_INTERPRETER_DENY)
        return 2

    try:
        targets = extract_targets(cmd, cwd)
    except Exception as e:
        sys.stderr.write(
            "require-worktree-edit-bash: failing open, cannot parse with shell-command-scan.py "
            "(%r). This guard is disabled for this call.\n" % (e,))
        return 0
    if not targets:
        return 0

    store_hits = []
    for t in sorted(set(targets)):
        if not t:
            continue
        if glob_match("*/.agent-context/*/skills/*/SKILL.md", t):
            pass  
        elif glob_match("*/.agent-context/*/skills/*/*", t):
            continue  
        
        
        if any_glob(ENTITY_PATTERNS, t) and not TEMPLATE_RE.search(t):
            store_hits.append(t)

    if store_hits:
        sys.stderr.write(STORE_HITS_DENY % "".join("  %s\n" % h for h in store_hits))
        return 2

    blocked = []
    for t in sorted(set(targets)):
        if not t:
            continue
        
        
        
        payload = json.dumps({"tool_name": "Write", "cwd": data.get("cwd") or "",
                              "tool_input": {"file_path": t}})
        try:
            proc = subprocess.run([sys.executable, policy], input=payload,
                                  capture_output=True, text=True)
            denied_by_policy = proc.returncode != 0
        except OSError:
            denied_by_policy = False
        if denied_by_policy:
            blocked.append(t)

    if not blocked:
        return 0

    sys.stderr.write(BLOCKED_DENY % "".join("  %s\n" % b for b in blocked))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("require-worktree-edit-bash: failed open on an internal error: %r\n" % (exc,))
        sys.exit(0)
