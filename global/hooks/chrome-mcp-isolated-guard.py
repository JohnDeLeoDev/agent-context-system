#!/usr/bin/env python3
"SessionStart. Keeps `--isolated` on the chrome-devtools-mcp plugin's MCP server.\n\nWHY A HOOK AND NOT JUST AN EDIT. The server args live in the plugin's own\n`.claude-plugin/plugin.json`, inside a git clone under the plugin CACHE. A plugin\nupdate re-clones or fast-forwards that checkout and silently reverts the edit --\nand the revert is invisible until the next time two sessions collide, which is\nexactly the failure this is meant to end. So the edit is re-asserted every\nsession, for every installed version.\n\nThe server also accepts NO environment variable for this (`isolated` and\n`userDataDir` are argv-only in bin/chrome-devtools-mcp-cli-options.ts), and a\nplugin-provided MCP server cannot be overridden from ~/.claude.json, so editing\nthe plugin manifest is the only lever that exists.\n\nSilent when already correct. Prints one line when it repairs a reverted manifest,\nbecause that is a real event worth seeing: it means a plugin update just landed."
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp


def main():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    cache = os.path.join(hp.claude_home(home), "plugins", "cache")
    if not os.path.isdir(cache):
        return 0

    pattern = os.path.join(cache, "*", "chrome-devtools-mcp", "*", ".claude-plugin",
                            "plugin.json")
    repaired = []

    for path in glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue

        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            continue

        changed = False
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            args = spec.get("args")
            if not isinstance(args, list):
                continue
            
            
            
            if any(str(a).startswith(("--userDataDir", "--browserUrl", "--wsEndpoint"))
                   for a in args):
                continue
            if "--isolated" in (str(a) for a in args):
                continue
            args.append("--isolated")
            changed = True

        if changed:
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                os.replace(tmp, path)
                repaired.append(path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    if repaired:
        ver = os.path.basename(os.path.dirname(os.path.dirname(repaired[0])))
        print(
            "chrome-mcp-isolated-guard: applied --isolated to chrome-devtools-mcp %s "
            "(it was absent -- either a fresh install or a plugin update that reverted "
            "it). Without it, a second concurrent session cannot launch a browser at "
            "all: they collide on the one shared Chrome profile (audit policy). "
            "Restart or /reload-plugins for it to take effect this session." % ver
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
