#!/usr/bin/env python3
'store-orphan-probe -- find store files the index does not know about.\n\nA store entity is a content file plus its metadata. Two formats\ncarry that metadata, and which one applies is decided by the bucket:\n\n  scripts/, hooks/   a `<file>.meta.toml` sidecar   (.py/.sh cannot hold frontmatter)\n  everything else    YAML frontmatter in the .md itself\n\nLose the metadata and the file still sits in git, still gets projected into\n~/.claude, still runs -- but it is not an entity. `get_entity` answers null and\n`bulk_edit` answers "not found". Nothing raises; the only symptom is one line in\na bulk result that nobody reads entry by entry.\n\nReport-only. It does not auto-register what it finds: an entity\'s\ndescription is the text every session loads, and inventing one for a file whose\npurpose you have not read is worse than a loud "this is not registered". So it\nnames the file and the exact call that fixes it.\n\nRun: python3 ~/.agent-context/global/scripts/store-orphan-probe.py [--quiet]\nWired into home-materialize.py at SessionStart.\n\nObservations guarded: #276.'
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task  

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
TREES = ("global", "projects", "workspaces")
CONTENT = (".md", ".sh", ".py", ".fish")

SIDECAR, FRONTMATTER = "sidecar", "frontmatter"
BUCKETS = {
    "scripts":      (SIDECAR, "upsert_script"),
    "hooks":        (SIDECAR, "upsert_hook"),
    "docs":         (FRONTMATTER, "upsert_doc"),
    "memory":       (FRONTMATTER, "upsert_memory"),
    "instructions": (FRONTMATTER, "upsert_instruction"),
    "commands":     (FRONTMATTER, "upsert_command"),
    "agents":       (FRONTMATTER, "upsert_agent_definition"),
    "skills":       (FRONTMATTER, "upsert_skill"),
}
SKIP_PARTS = ("/.git/", "/__pycache__/", "/references/", "/vendor/",
              "/node_modules/", "/archive/", "/" + hp.CLAUDE_DIRNAME + "/worktrees/",
              "/" + hp.AGENTS_DIRNAME + "/worktrees/", "/templates/")


def _has_frontmatter(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readline().strip() == "---"
    except Exception:
        return True                      


def orphans():
    found = []
    for tree in TREES:
        root = os.path.join(STORE, tree)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__", "server")]
            if any(s in dirpath + "/" for s in SKIP_PARTS):
                continue
            parts = dirpath[len(STORE) + 1:].split(os.sep)
            bucket = next((b for b in parts if b in BUCKETS), None)
            if bucket is None:
                continue                 
            style, tool = BUCKETS[bucket]
            for fn in sorted(filenames):
                if not fn.endswith(CONTENT) or fn.endswith(".meta.toml"):
                    continue
                full = os.path.join(dirpath, fn)
                if bucket == "skills" and fn != "SKILL.md":
                    continue             
                if style == SIDECAR:
                    bad = not os.path.exists(full + ".meta.toml")
                else:
                    bad = not _has_frontmatter(full)
                if bad:
                    found.append((os.path.relpath(full, STORE), style, tool))
    return found


def record(component, ok, detail):
    hr = os.path.join(hp.scripts_dir(), "health-record.py")
    if not os.path.exists(hr):
        return
    args = ([sys.executable, hr, component, "--ok"] if ok
            else [sys.executable, hr, component, "--fail", detail])
    try:
        subprocess.run(args, capture_output=True, timeout=20)
    except Exception:
        pass


def main():
    quiet = "--quiet" in sys.argv
    
    
    
    if not os.path.isdir(os.path.join(STORE, "server")):
        record("store-orphan", True, "")
        return 0
    try:
        found = orphans()
    except Exception as exc:                              
        record("store-orphan", False, f"store-orphan-probe itself failed: {exc!r}")
        return 0

    if not found:
        record("store-orphan", True, "")
        if not quiet:
            print("store-orphan-probe: every store content file is a registered entity")
        return 0

    lines = [f"{rel} — no {style}; register with {tool}" for rel, style, tool in found]
    record("store-orphan", False,
           "Store files that are not registered entities, so the index cannot see them, "
           "get_entity returns null, and every bulk_edit skips them with a 'not found' "
           "buried in its result: " + "; ".join(lines))
    print("store-orphan-probe: UNREGISTERED store file(s) — unreachable by every MCP tool:",
          file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    store_task.main_or_forward("store-orphan-probe", main, store=STORE)
