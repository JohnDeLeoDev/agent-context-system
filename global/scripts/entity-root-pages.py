'Usage: entity-root-pages.py <out-dir> [store-root]\n\nWrites one body per page into <out-dir> and prints a JSON manifest: path, title,\nproject, workspace, body_path. The caller upserts each through MCP (upsert_doc takes\nbody_path), so the pages land as real docs with store frontmatter. Anything under the\n`## Notes` marker in the page already in the store is carried forward.'
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OUT = None
ROOT = None
KEEP = "## Notes"
LINK = re.compile(r"\[\[([^\]\[]+)\]\]")
CODE = re.compile(r"```.*?```|~~~.*?~~~|``.+?``|`[^`\n]+`", re.DOTALL)


def toml_map(path):
    d = {}
    with open(path, errors="ignore") as source:
        for line in source:
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                d[k.strip()] = v.strip().strip('"')
    return d


def md_count(base, sub):
    d = os.path.join(base, sub)
    return sum(1 for r, _, fs in os.walk(d) for f in fs if f.endswith(".md")) if os.path.isdir(d) else 0


def counts_line(base):
    parts = [(md_count(base, "docs"), "docs"), (md_count(base, "memory"), "memories"),
             (md_count(base, "skills"), "skills"), (md_count(base, "commands"), "commands"),
             (md_count(base, "instructions"), "instructions")]
    return ", ".join(f"{n} {w[:-1] if n == 1 and w.endswith('s') else w}" for n, w in parts if n)


def owned_markdown_links(base, root_page):
    'Path-qualified Obsidian links for every Markdown file owned by a scope.'
    links = []
    for directory, dirs, files in os.walk(base):
        dirs.sort()
        for filename in sorted(files):
            if not filename.endswith(".md"):
                continue
            full = os.path.join(directory, filename)
            relative = os.path.relpath(full, ROOT).replace(os.sep, "/")
            if relative == root_page:
                continue
            links.append(f"- [[{relative[:-3]}|{filename[:-3]}]]")
    return links


def in_degree():
    deg = collections.Counter()
    for d, dirs, fs in os.walk(ROOT):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("server", "node_modules")]
        for f in fs:
            if not f.endswith(".md"):
                continue
            with open(os.path.join(d, f), errors="ignore") as source:
                body = source.read()
            for t in set(LINK.findall(CODE.sub(lambda m: " " * len(m.group(0)), body))):
                deg[t.split("|")[0].split("#")[0].strip()] += 1
    return deg


def top_linked(base, deg, limit=8):
    rows = []
    for sub in ("memory", "docs"):
        d = os.path.join(base, sub)
        for r, _, fs in (os.walk(d) if os.path.isdir(d) else []):
            for f in fs:
                if f.endswith(".md") and deg.get(f[:-3]):
                    rows.append((deg[f[:-3]], f[:-3]))
    rows.sort(key=lambda x: (-x[0], x[1]))
    return rows[:limit]


def carried(disk_path):
    full = os.path.join(ROOT, disk_path)
    if not os.path.exists(full):
        return ""
    with open(full, errors="ignore") as source:
        text = source.read()
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        text = text[end + 5:] if end != -1 else text
    i = text.find(KEEP)
    return text[i + len(KEEP):].strip("\n") if i != -1 else ""


def main():
    global OUT, ROOT
    OUT = sys.argv[1]
    ROOT = (sys.argv[2] if len(sys.argv) > 2
            else os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context"))
    os.makedirs(OUT, exist_ok=True)
    deg = in_degree()
    pages = []
    ws_members = collections.defaultdict(list)
    projects = []
    for name in sorted(os.listdir(os.path.join(ROOT, "projects"))):
        base = os.path.join(ROOT, "projects", name)
        tf = os.path.join(base, "project.toml")
        if os.path.isdir(base) and os.path.exists(tf):
            p = toml_map(tf)
            projects.append((name, p))
            if p.get("workspace"):
                ws_members[p["workspace"]].append(name)

    for name, p in projects:
        base = os.path.join(ROOT, "projects", name)
        rows = [f"# {name}", "", "| | |", "|---|---|"]
        for label, key in (("Stack", "stack"), ("Integration branch", "integration_branch")):
            if p.get(key):
                rows.append(f"| {label} | `{p[key]}` |")
        if p.get("workspace"):
            rows.append(f"| Workspace | [[{p['workspace']}]] |")
        if p.get("canonical_remote"):
            rows.append(f"| Remote | `{p['canonical_remote']}` |")
        rows += ["", f"Holds {counts_line(base) or 'nothing yet'}.", ""]
        top = top_linked(base, deg)
        if top:
            rows.append("## Most linked to here")
            rows += [f"- [[{k}]] ({n})" for n, k in top]
        root_page = f"projects/{name}/docs/{name}.md"
        rows += ["", "## Project files"]
        rows += owned_markdown_links(base, root_page) or ["- none"]
        rows += ["", "Part of [[agent-context-store]]."]
        pages.append((root_page, name, name, None, rows))

    for name in sorted(os.listdir(os.path.join(ROOT, "workspaces"))):
        base = os.path.join(ROOT, "workspaces", name)
        if not os.path.isdir(base):
            continue
        rows = [f"# {name}", "", "A workspace: every project below inherits its instructions,",
                "skills and commands.", "", "## Projects"]
        rows += [f"- [[{m}]]" for m in sorted(ws_members.get(name, []))] or ["- none"]
        root_page = f"workspaces/{name}/docs/{name}.md"
        rows += ["", f"Holds {counts_line(base) or 'nothing of its own'}.", "",
                 "## Workspace files"]
        rows += owned_markdown_links(base, root_page) or ["- none"]
        rows += ["", "Part of [[agent-context-store]]."]
        pages.append((root_page, name, None, name, rows))

    machines = []
    mdir = os.path.join(ROOT, "machines")
    for f in sorted(os.listdir(mdir)):
        if not f.endswith(".toml"):
            continue
        m = toml_map(os.path.join(mdir, f))
        mid = m.get("machine_id") or f[:-5]
        uid = m.get("machine_uuid", f[:-5])
        machines.append(mid)
        rows = [f"# {mid}", "", "| | |", "|---|---|"]
        for label, key in (("Hostname", "hostname"), ("Platform", "platform"),
                           ("Home", "home_dir"), ("Display name", "display_name")):
            if m.get(key):
                rows.append(f"| {label} | `{m[key]}` |")
        rows += [f"| Machine uuid | `{uid}` |", "",
                 f"Its synced counters live in `machines/{uid}/`: usage, daemon status",
                 "and token-usage rollups.", "", "Part of [[agent-context-store]]."]
        pages.append((f"global/docs/machines/{mid}.md", mid, None, None, rows))

    rows = ["# agent-context-store", "",
            "The store every agent reads: instructions, memory, docs, skills, commands,",
            "hooks and scripts, as files in a git repository synced across the fleet.",
            "How to use it: [[store-operations.md]].", "", "## Projects"]
    rows += [f"- [[{n}]]" for n, _ in projects]
    rows += ["", "## Workspaces"] + [f"- [[{w}]]" for w in sorted(ws_members)]
    rows += ["", "## Machines"] + [f"- [[{m}]]" for m in sorted(machines)]
    rows += ["", f"Global scope holds {counts_line(os.path.join(ROOT, 'global'))}."]
    rows += ["", "## Global files"]
    rows += owned_markdown_links(os.path.join(ROOT, "global"),
                                 "global/docs/agent-context-store.md") or ["- none"]
    pages.append(("global/docs/agent-context-store.md", "agent-context-store", None, None, rows))

    manifest = []
    for i, (disk, title, project, workspace, rows) in enumerate(pages):
        body = "\n".join(rows).rstrip("\n") + f"\n\n{KEEP}\n"
        keep = carried(disk)
        if keep:
            body += keep + "\n"
        out = os.path.join(OUT, f"{i:02d}.md")
        with open(out, "w") as destination:
            destination.write(body)
        store_path = disk
        if project or workspace:
            store_path = "/".join(disk.split("/")[3:])
        else:
            store_path = disk[len("global/docs/"):]
        manifest.append({"path": store_path, "title": title, "project": project,
                         "workspace": workspace, "body_path": out})
    print(json.dumps(manifest, indent=1))
    return 0


if __name__ == "__main__":
    
    
    if os.environ.get("AGENT_CONTEXT_SERVER_TASK") == "1":
        sys.exit(main())
    import store_task
    _store = (sys.argv[2] if len(sys.argv) > 2
              else os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context"))
    store_task.main_or_forward("entity-root-pages", main, store=_store)
