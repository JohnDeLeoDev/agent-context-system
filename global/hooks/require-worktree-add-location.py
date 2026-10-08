#!/usr/bin/env python3
'require-worktree-add-location: a new git worktree lands under .agents/worktrees/.\n\nA worktree already at legacy .claude/worktrees/<name> stays valid until it lands (the\nglobal instruction says so); this hook only judges a NEW `git worktree add`, never an\nexisting one, so re-running an old command against an existing worktree is unaffected.\n\nFails OPEN on any parse error, an unresolvable path, or a base directory that is not\nitself a git repository: a guard that cannot read a command must not brick every Bash\ncall. Its correctness rests on test-require-worktree-add-location.py.'
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

SCRIPTS = hp.scripts_dir()
SCAN_TOOL = os.environ.get("SHELL_COMMAND_SCAN") or os.path.join(SCRIPTS, "shell-command-scan.py")



FAST_PATH = re.compile(r"(^|[^A-Za-z0-9_./-])worktree([^A-Za-z0-9_./-]|$)")

ONE_ARG_OPTS = {"-b", "-B", "--orphan", "--reason", "--track", "-c"}

DENY = """BLOCKED by require-worktree-add-location: a new git worktree must be created under
.agents/worktrees/<name> (the 2026-09-04 worktree mandate), and this one does not:
%s
Use this instead:
%s
This applies on every harness. Codex has no native worktree-creation event, so this is
the only place that mandate is enforced for it; a worktree already at legacy
.claude/worktrees/<name> stays valid until it lands, but a NEW one never goes there.
"""


def load_module(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def worktree_add_calls(cmd, cwd, scan_mod):
    "[(path, base_dir)] for each `git worktree add` simple command in `cmd`. `path` is\n    the literal path argument as written; `base_dir` is the segment's own cd-tracked\n    directory (scan() already follows a leading `cd`), further adjusted for a leading\n    `git -C DIR`."
    try:
        _redirects, segments = scan_mod.parse(cmd, cwd)
    except Exception:
        return []
    out = []
    for seg_dir, toks, _sep in segments:
        words = [t.text for t in toks]
        if not words or words[0] != "git":
            continue
        i, base = 1, seg_dir
        while i < len(words) and words[i] != "worktree":
            if words[i] == "-C" and i + 1 < len(words):
                base = words[i + 1]
                i += 2
                continue
            i += 1
        if i >= len(words) or words[i] != "worktree":
            continue
        i += 1
        if i >= len(words) or words[i] != "add":
            continue
        i += 1
        path = None
        while i < len(words):
            w = words[i]
            if w in ONE_ARG_OPTS:
                i += 2
                continue
            if w.startswith("-"):
                i += 1
                continue
            path = w
            break
        if path:
            out.append((path, base))
    return out


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    raw = (payload.get("tool_input") or {}).get("command") if isinstance(
        payload.get("tool_input"), dict) else None
    if not isinstance(raw, str) or not raw or not FAST_PATH.search(raw):
        return 0
    try:
        scan_mod = load_module(SCAN_TOOL, "shell_command_scan")
    except Exception:
        return 0
    cmd, cwd = scan_mod.payload_command(payload)
    if not cmd:
        return 0
    hits = worktree_add_calls(cmd, cwd, scan_mod)
    if not hits:
        return 0
    blocked = []
    for path, base in hits:
        
        
        
        if any(ch in s for s in (path, base) for ch in "$`"):
            continue
        path, base = os.path.expanduser(path), os.path.expanduser(base)
        base_dir = base if os.path.isabs(base) else os.path.join(cwd, base)
        if not os.path.isdir(base_dir):
            continue
        try:
            top = subprocess.run(["git", "-C", base_dir, "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if top.returncode != 0 or not top.stdout.strip():
            continue
        root = os.path.realpath(top.stdout.strip())
        target = path if os.path.isabs(path) else os.path.join(base_dir, path)
        target = os.path.realpath(target)
        wanted = os.path.join(root, ".agents", "worktrees")
        if target != wanted and not target.startswith(wanted + os.sep):
            blocked.append((path, root))
    if not blocked:
        return 0
    named = "".join("  %s\n" % p for p, _r in blocked)
    fixed = "".join(
        "  git worktree add %s -b %s\n" % (
            os.path.join(root, ".agents", "worktrees", os.path.basename(p.rstrip("/")) or "wt"),
            os.path.basename(p.rstrip("/")) or "wt")
        for p, root in blocked)
    sys.stderr.write(DENY % (named, fixed))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("require-worktree-add-location: failed open on an internal error: %r\n"
                         % (exc,))
        sys.exit(0)
