#!/usr/bin/env python3
"Deterministic inventory of every file in the agent-context system.\n\nSources: every git-tracked file in the store, plus the harness projections\noutside it. Output: inventory.tsv in the data dir, sorted by path, one row\nper file, with sha256 so a later pass can detect files that changed after\nreview. Re-running keeps status/reviewer/verdict/notes for rows whose sha256 is\nunchanged and resets them to 'unread' when the file changed.\n\nData dir: $CONTEXT_INVENTORY_DIR, default ~/.local/state/agent-context/context-inventory.\nPipeline: context-inventory-build.py -> context-inventory-batch.py -> reviewers\nwrite results/<batch>.tsv -> context-inventory-merge.py.\n\nReads only; writes only inventory.tsv in the data dir."
import csv, hashlib, os, re, subprocess, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task  

STORE = Path(os.environ.get("AGENT_CONTEXT_STORE") or hp.store_root())
DATA = Path(os.environ.get("CONTEXT_INVENTORY_DIR",
                           Path.home() / ".local/state/agent-context/context-inventory"))
OUT = DATA / "inventory.tsv"

CL = hp.CLAUDE_DIRNAME
EXTERNAL = [
    f"~/{CL}/CLAUDE.md", f"~/{CL}/settings.json", f"~/{CL}/agents",
    f"~/{CL}/skills", f"~/{CL}/commands", "~/.codex/AGENTS.md",
    "~/.codex/config.toml", "~/.config/opencode", "~/.pi/agent/agents",
]

EXTERNAL_SKIP = re.compile(r"/node_modules/|/package-lock\.json$|/\.gitignore$")





RULES = [
    (r"^AGENTS\.md$|^FORMAT\.md$|^README\.md$", "entry", "full"),
    (r"^global/instructions/", "instruction", "full"),
    (r"^global/agents/", "agent", "full"),
    (r"^global/skills/", "skill", "full"),
    (r"^global/commands/", "command", "full"),
    (r"^global/memory/", "memory", "full"),
    (r"^global/docs/(archive|handoffs)/", "doc-history", "meta"),
    (r"^global/docs/", "doc", "full"),
    (r"^global/hooks/.*test", "hook-test", "meta"),
    (r"^global/hooks/.*\.py$", "hook", "full"),
    (r"^global/hooks/", "hook-meta", "full"),
    (r"^global/scripts/.*\.toml$", "script-meta", "meta"),
    (r"^global/scripts/.*test", "script-test", "meta"),
    (r"^global/scripts/", "script", "strings"),
    (r"^global/audit-observations/", "audit-open", "meta"),
    (r"^global/audit-observations-archive/", "audit-archive", "data"),
    (r"^global/test-locks/", "test-lock", "data"),
    (r"^global/(state|deps|node-tools)/", "global-data", "data"),
    (r"^global/[^/]+$", "global-config", "full"),
    (r"^(projects|workspaces)/[^/]+/instructions/", "proj-instruction", "full"),
    
    (r"^(projects|workspaces|global)/([^/]+/)?skills/[^/]+/references/", "skill-vendored-ref", "meta"),
    (r"^(projects|workspaces)/[^/]+/(agents|skills|commands)/", "proj-agent-skill", "full"),
    (r"^(projects|workspaces)/[^/]+/hooks/", "proj-hook", "full"),
    (r"^(projects|workspaces)/[^/]+/memory/", "proj-memory", "full"),
    (r"^(projects|workspaces)/[^/]+/docs/.*(handoff|archive)", "proj-doc-history", "meta"),
    
    (r"^(projects|workspaces)/[^/]+/docs/", "proj-doc", "meta"),
    (r"^(projects|workspaces)/[^/]+/scripts/.*\.toml$", "proj-script-meta", "meta"),
    (r"^(projects|workspaces)/[^/]+/scripts/", "proj-script", "strings"),
    (r"^(projects|workspaces)/[^/]+/[^/]+\.toml$", "proj-config", "full"),
    (r"^server/src/", "server", "strings"),
    (r"^server/", "server-support", "meta"),
    (r"^tests/", "test", "meta"),
    (r"^templates/", "template", "full"),
    (r"^shared-(docs|skills)/", "projection", "verify"),
    (r"^machines/", "machine-data", "data"),
    (r"^\.obsidian/", "editor-config", "data"),
    (r"^[^/]+$", "root-config", "full"),
]
FIELDS = ["path", "bytes", "sha256", "class", "depth", "status", "reviewer", "verdict", "notes"]
KEEP = ("status", "reviewer", "verdict", "notes")


def classify(rel):
    for pat, cls, depth in RULES:
        if re.search(pat, rel):
            return cls, depth
    return "unclassified", "full"


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def rows():
    tracked = subprocess.run(["git", "-C", str(STORE), "ls-files", "-z"],
                             capture_output=True, check=True).stdout.decode().split("\0")
    for rel in filter(None, tracked):
        p = STORE / rel
        if p.is_file():
            yield (rel, p, *classify(rel))
    home = str(Path.home())
    for ext in EXTERNAL:
        base = Path(os.path.expanduser(ext))
        if base.is_file():
            files = [base]
        elif base.is_dir():
            files = sorted(q for q in base.rglob("*") if q.is_file())
        else:
            files = []
        for p in files:
            rel = str(p).replace(home, "~", 1)
            if EXTERNAL_SKIP.search(rel):
                continue
            if f"/{CL}/skills/synced/" in rel:
                
                
                yield rel, p, "external-thirdparty", "meta"
            else:
                yield rel, p, "external-projection", "verify"


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    old = {}
    if OUT.exists():
        with open(OUT, newline="") as f:
            old = {r["path"]: r for r in csv.DictReader(f, delimiter="\t")}
    out = []
    for rel, p, cls, depth in rows():
        r = {"path": rel, "bytes": p.stat().st_size, "sha256": sha(p), "class": cls,
             "depth": depth, "status": "unread", "reviewer": "", "verdict": "", "notes": ""}
        prev = old.get(rel)
        if prev and prev["sha256"] == r["sha256"]:
            r.update({k: prev[k] for k in KEEP})
        elif prev and prev["status"] != "unread":
            r["notes"] = "changed since review"
        out.append(r)
    out.sort(key=lambda r: r["path"])
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(out)
    n, b = Counter(), Counter()
    for r in out:
        n[(r["class"], r["depth"])] += 1
        b[r["class"]] += int(r["bytes"])
    for (cls, depth), k in sorted(n.items(), key=lambda x: -x[1]):
        print(f"{k:5d} {b[cls]:>9d}B  {cls:22s} {depth}")
    print(f"total {len(out)} files; unread {sum(r['status'] == 'unread' for r in out)}")


if __name__ == "__main__":
    store_task.main_or_forward("context-inventory-build", main, store=str(STORE))
