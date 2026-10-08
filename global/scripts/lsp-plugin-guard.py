#!/usr/bin/env python3
'Assert the store\'s one-code-intelligence-client rule in a project\'s settings.\n\nSo the rule is asserted from the store, on every materialization, on every machine. This\nalso closes gap (1) of that observation -- "nothing cross-references enabledPlugins against\nmcp-servers.json" -- by making the manifest the single source: a project gets a `false` for\nexactly the LSP servers the manifest scopes to it, and for nothing else.\n\nA project-scoped `enabledPlugins` entry silently overrides the global one, and BOTH\nsettings files carry that scope, so both are asserted. settings.local.json is only touched\nwhen it already names the plugin: it is the user\'s own file, and creating keys in it would\nbe the store colonising the one place a machine is allowed to differ.\n\nUsage: lsp-plugin-guard.py <project-dir> [--store <dir>] [--quiet]\nExit 0 always: a materializer step must never be the reason a session fails to start.'
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.path.expanduser("~")
SUFFIX = "@claude-plugins-official"


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def catalog_plugin_ids():
    'Full plugin ids the local catalog cache knows, or None if unreadable.\n\n    `catalog.plugins` is an object keyed by the id itself ("swift-lsp@claude-plugins-\n    official"), which is exactly the string written into enabledPlugins -- so it is read\n    directly rather than reassembled. Walking the tree for `name` keys instead finds the\n    names of every skill and command inside every plugin, which happens to contain some\n    of the right strings and is not the same set.\n\n    Used only to AVOID writing an id for a plugin that does not exist. When the cache is\n    missing (a fresh machine, a cleared cache) the filter is skipped rather than guessed\n    at: a redundant `false` for a plugin nobody has is inert, while skipping a real one\n    would leave the second client enabled, which is the fault being closed.'
    cfg = load(os.path.join(hp.claude_home(HOME), "plugins", "plugin-catalog-cache.json"))
    if not isinstance(cfg, dict):
        return None
    plugins = (cfg.get("catalog") or {}).get("plugins")
    if isinstance(plugins, dict) and plugins:
        return set(plugins)
    if isinstance(plugins, list) and plugins:
        ids = {p.get("id") or p.get("key") for p in plugins if isinstance(p, dict)}
        ids.discard(None)
        return ids or None
    return None


def scoped_lsp_servers(manifest, project_dir):
    "Plugin ids that would contend with this project's store-declared LSP bridges."
    known = catalog_plugin_ids()
    out = []
    real = os.path.realpath(project_dir)
    for name, spec in (manifest.get("servers") or {}).items():
        if not re.fullmatch(r".+-lsp", name):
            continue
        if "claude" not in (spec.get("harnesses") or []):
            continue
        if not any(os.path.realpath(os.path.join(HOME, rel)) == real
                   for rel in spec.get("projects") or []):
            continue
        
        args = spec.get("args") or []
        for candidate in {name, args[-1] if args else name}:
            pid = candidate + SUFFIX
            if known is not None and pid not in known:
                continue
            out.append(pid)
    return sorted(set(out))


def assert_false(path, plugin_ids, create):
    'Force each plugin id to false. Returns the ids actually changed.\n\n    `create` distinguishes the file the store owns from the one it only corrects:\n    without it, a plugin absent from the file is left absent rather than added.'
    cfg = load(path)
    if cfg is None:
        if not create:
            return []
        cfg = {}
    if not isinstance(cfg, dict):
        return []
    plugins = cfg.get("enabledPlugins")
    if not isinstance(plugins, dict):
        if not create:
            return []
        plugins = {}
    changed = []
    for pid in plugin_ids:
        if pid not in plugins and not create:
            continue
        if plugins.get(pid) is not False:
            plugins[pid] = False
            changed.append(pid)
    if not changed:
        return []
    cfg["enabledPlugins"] = plugins
    
    
    
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return changed


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    quiet = "--quiet" in argv
    store = HOME + "/.agent-context"
    if "--store" in argv:
        store = argv[argv.index("--store") + 1]
    if not args:
        print("usage: lsp-plugin-guard.py <project-dir>", file=sys.stderr)
        return 0
    project = args[0]

    manifest = load(os.path.join(store, "global", "mcp-servers.json"))
    if not manifest:
        return 0
    ids = scoped_lsp_servers(manifest, project)
    if not ids:
        return 0

    agents = os.path.join(project, ".agents", "claude")
    if not os.path.isdir(agents):
        return 0

    changed = assert_false(os.path.join(agents, "settings.json"), ids, create=True)
    changed += assert_false(
        os.path.join(agents, "settings.local.json"), ids, create=False)
    if changed and not quiet:
        print("lsp-plugin-guard: disabled %s (the store's MCP bridge is the one client)"
              % ", ".join(sorted(set(changed))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
