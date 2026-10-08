"Materialized-content projection: return the store's files as a {path: content} map.\n\nThe map is what a relay writes to its local config dir (~/.claude/). Paths are\nrelative to that root. Frontmatter is stripped and key fields re-emitted the same\nway home-materialize.py does it, so the content is byte-identical to the on-disk\nprojection."

from __future__ import annotations

import os

_SKIP_SUFFIXES = (".meta.toml", ".meta.json")
_SKIP_NAMES = frozenset({".DS_Store"})
MANIFEST_FILES = ("mcp-servers.json", "hooks-manifest.json")





EXTRA_MANIFESTS = ("deps/manifest.toml", "lsp-canaries.json")



EXTRA_TREES = ("node-tools",)

ROOT_FILES = ("AGENTS.md",)
_JUNK_DIRS = frozenset({"__pycache__"})
_PROJECT_SCOPE_DIRS = ("skills", "commands", "agents", "scripts", "hooks", "parity", "ship")
_WORKSPACE_SCOPE_DIRS = ("skills", "commands")



_HARNESS_RENAME = {"allowed_tools": "allowed-tools",
                   "disable_model_invocation": "disable-model-invocation",
                   "argument_hint": "argument-hint",
                   "permission_mode": "permissionMode"}
_SKILL_KEYS = ("name", "description", "allowed_tools", "disable_model_invocation")
_COMMAND_KEYS = ("description", "allowed_tools", "disable_model_invocation", "argument_hint")
_AGENT_KEYS = ("name", "description", "tools", "model", "effort", "permission_mode")


def _harness_keep(head, keys):
    '`head` lines for `keys`, renamed to harness spelling. Drops a `false` boolean\n    line: that is the CLEAR sentinel for disable_model_invocation (a bool has no "" to\n    clear with, unlike a string field), and an absent key means the same thing to every\n    harness as one explicitly set to false, so the two must render identically.'
    keep = []
    for ln in head:
        k = ln.split(":", 1)[0]
        if k not in keys or ln[len(k):].strip(": ") == "false":
            continue
        keep.append(ln.replace(k + ":", _HARNESS_RENAME.get(k, k) + ":", 1))
    return keep


def _skip(fn: str) -> bool:
    return fn in _SKIP_NAMES or fn.endswith(_SKIP_SUFFIXES)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _strip_frontmatter(text: str, path: str) -> str:
    'Strip the store frontmatter block (the one carrying `uuid:`) and re-emit\n    key fields for SKILL.md and agent definitions. Mirrors home-materialize.py.'
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None or not any(ln.startswith("uuid:") for ln in lines[1:end]):
        return text
    head = lines[1:end]
    rest = lines[end + 1:]
    while rest and rest[0].strip() == "":
        rest.pop(0)
    if rest and rest[0].strip() == "---":
        return "\n".join(rest)  
    if os.path.basename(path) == "SKILL.md":
        keys = _SKILL_KEYS
    elif (os.sep + "commands" + os.sep) in path:
        keys = _COMMAND_KEYS
    elif (os.sep + "agents" + os.sep) in path:
        keys = _AGENT_KEYS
    else:
        return "\n".join(rest)
    keep = _harness_keep(head, keys)
    if any(ln.startswith("description:") for ln in keep):
        rest = ["---", *keep, "---", "", *rest]
    return "\n".join(rest)


def _walk_sub(root: str, sub: str, strip: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    d = os.path.join(root, sub)
    if not os.path.isdir(d):
        return out
    for dp, _dns, fns in os.walk(d):
        for fn in fns:
            if _skip(fn):
                continue
            full = os.path.join(dp, fn)
            content = _read(full)
            if content is None:
                continue
            if strip:
                content = _strip_frontmatter(content, full)
            out[os.path.relpath(full, root)] = content
    return out


def _walk_raw(root: str, sub: str) -> dict[str, str]:
    'Raw files under root/sub, keyed relative to root. Sidecars stay; junk does not.'
    out: dict[str, str] = {}
    d = os.path.join(root, sub)
    for dp, dns, fns in os.walk(d):
        dns[:] = [n for n in dns if n not in _JUNK_DIRS]
        for fn in fns:
            if fn in _SKIP_NAMES:
                continue
            full = os.path.join(dp, fn)
            content = _read(full)
            if content is not None:
                out["/".join(os.path.relpath(full, root).split(os.sep))] = content
    return out


def _scope_files(store_root: str, kind: str, toml_name: str,
                 subdirs: tuple[str, ...]) -> dict[str, str]:
    "`<kind>/<name>/<rel>` for each scope's TOML file and allowed subdirs, raw."
    out: dict[str, str] = {}
    top = os.path.join(store_root, kind)
    if not os.path.isdir(top):
        return out
    for name in sorted(os.listdir(top)):
        scope_dir = os.path.join(top, name)
        if not os.path.isdir(scope_dir):
            continue
        content = _read(os.path.join(scope_dir, toml_name))
        if content is not None:
            out[f"{kind}/{name}/{toml_name}"] = content
        for sub in subdirs:
            if os.path.isdir(os.path.join(scope_dir, sub)):
                for rel, text in _walk_raw(scope_dir, sub).items():
                    out[f"{kind}/{name}/{rel}"] = text
    return out


def scope_bundle(bundle: dict[str, str], project_ids: set[str]) -> dict[str, str]:
    '`bundle` without any projects/ or workspaces/ key except those of the projects whose\n    project.toml uuid is in `project_ids` and the workspaces those files name. Ids only ever\n    select among keys already in `bundle`: they are never used as a path.'
    from .store import parse_toml
    keep: list[str] = []
    for key, text in bundle.items():
        parts = key.split("/")
        if len(parts) != 3 or parts[0] != "projects" or parts[2] != "project.toml":
            continue
        try:
            fields = parse_toml(text)
        except Exception:
            continue
        if fields.get("uuid") in project_ids:
            keep.append(f"projects/{parts[1]}/")
            if fields.get("workspace"):
                keep.append(f"workspaces/{fields['workspace']}/")
    prefixes = tuple(keep)
    return {k: v for k, v in bundle.items()
            if not k.startswith(("projects/", "workspaces/")) or k.startswith(prefixes)}


def read_global_doc(store_root: str, rel: str) -> str | None:
    'The frontmatter-stripped body of `global/docs/<rel>`, as GET /doc serves it (the bundle\n    carries no docs). None when the doc is absent or resolves outside `global/docs/`.\n\n    ValueError for a path that is not a plain relative `.md` path (empty, absolute, a `.` or `..`\n    or empty segment, a backslash or NUL).'
    parts = rel.split("/")
    if (not rel or rel.startswith("/") or "\\" in rel or "\0" in rel or not rel.endswith(".md")
            or any(part in ("", ".", "..") for part in parts) or parts[-1] == ".md"):
        raise ValueError("not a plain relative .md path")
    root = os.path.realpath(os.path.join(store_root, "global", "docs"))
    full = os.path.realpath(os.path.join(root, *parts))
    if not full.startswith(root + os.sep):
        return None
    content = _read(full)
    return None if content is None else _strip_frontmatter(content, full)


def build_materialized_map(store_root: str) -> dict[str, str]:
    "Walk the store's global/ tree and return {local_path: content}.\n\n    `store_root` is the store root (~/.agent-context). Paths in the map are\n    relative to the local config dir root (~/.claude/): hooks/, scripts/,\n    skills/, commands/, agents/.\n\n    Docs are not in the map: they are read over MCP with get_doc (memory\n    mcp-only-no-local-files), and GET /doc serves a global doc on its own."
    g = os.path.join(store_root, "global")
    result: dict[str, str] = {}

    
    result.update(_walk_sub(g, "hooks", strip=False))
    result.update(_walk_sub(g, "scripts", strip=False))

    
    result.update(_walk_sub(g, "skills", strip=True))
    result.update(_walk_sub(g, "commands", strip=True))
    result.update(_walk_sub(g, "agents", strip=True))

    
    for name in MANIFEST_FILES:
        content = _read(os.path.join(g, name))
        if content is not None:
            result[f"manifests/{name}"] = content
    for name in EXTRA_MANIFESTS:
        content = _read(os.path.join(g, *name.split("/")))
        if content is not None:
            result[f"manifests/{name}"] = content
    for tree in EXTRA_TREES:
        for rel, content in _walk_sub(g, tree, strip=False).items():
            result[f"manifests/{rel}"] = content

    
    
    for name in ROOT_FILES:
        content = _read(os.path.join(store_root, name))
        if content is not None:
            result[f"root/{name}"] = content

    
    result.update(_scope_files(store_root, "projects", "project.toml", _PROJECT_SCOPE_DIRS))
    result.update(_scope_files(store_root, "workspaces", "workspace.toml",
                               _WORKSPACE_SCOPE_DIRS))

    return result
