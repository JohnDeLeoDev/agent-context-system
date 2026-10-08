#!/usr/bin/env python3
"Tests for the deps-chezmoi-parity invariant in invariant-check.py (fleet dependency management, C).\n\nThe manifest (global/deps/manifest.toml) declares which tools each machine needs. chezmoi's package\nlists, its language server checks and global/node-tools/package.json declare installs. The invariant\nkeeps the two from drifting: every name on the install side is a manifest tool (through `package`\nor `node_package`) or is listed under [unmanaged] with a reason, and every tool that names a\npackage finds it on the install side. It changes nothing in chezmoi, so no run_onchange script\nre-runs.\n\nFixture cases build a throwaway store and chezmoi source and point invariant-check at them through\nAGENT_CONTEXT_STORE and AGENT_CONTEXT_CHEZMOI_SOURCE. Scratch lives under ~/.cache."

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")
IDENT = "deps-chezmoi-parity"

BASE = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-tests")
os.makedirs(BASE, exist_ok=True)
ROOT = os.path.realpath(tempfile.mkdtemp(dir=BASE, prefix="deps-parity-"))
STORE = os.path.join(ROOT, "store")
CHEZ = os.path.join(ROOT, "chezmoi")
G = os.path.join(STORE, "global")
os.makedirs(os.path.join(G, "scripts"))
os.makedirs(os.path.join(G, "deps"))
os.makedirs(os.path.join(G, "node-tools"))
os.makedirs(CHEZ)
os.environ["AGENT_CONTEXT_STORE"] = STORE
os.environ["AGENT_CONTEXT_CHEZMOI_SOURCE"] = CHEZ
for name in os.listdir(HERE):
    if name in ("deps-check.py", "harness_paths.py"):
        shutil.copy(os.path.join(HERE, name), os.path.join(G, "scripts", name))

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        failures.append("%s %s" % (label, detail))
        print("FAIL:", label, detail)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


spec = importlib.util.spec_from_file_location("invariant_check", CHECK)
assert spec is not None and spec.loader is not None
mod: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
inv = next((i for i in mod.REGISTRY if i.id == IDENT), None)
if inv is None:
    sys.exit("%s is not registered in invariant-check.py" % IDENT)

MANIFEST = """schema = 1
[machine.box]
os = "mac"
roles = ["base"]
[role.base]
tools = ["jq", "tsgo", "uv"]
[tool.jq]
check = ["jq", "--version"]
package = "jq"
channel = "manual"
fix = "install jq"
[tool.uv]
check = ["uv", "--version"]
channel = "manual"
fix = "install uv"
[tool.tsgo]
check = ["tsgo", "--version"]
node_package = "@typescript/native-preview"
channel = "node-tools-sync"
fix = "run node-tools-sync"
[unmanaged]
"zsh" = "shell"
"gh" = "interactive"
"iterm2" = "GUI app"
"intellij-server" = "Kotlin server"
"brew" = "package manager"
"copilot-api" = "MCP proxy"
"""
PACKAGES = 'universal_packages = ["zsh", "jq", "gh"]\n{{ if x }}\nos_specific_packages = ["iterm2"]\n{{ else }}\nos_specific_packages = []\n{{ end }}\n'
INSTALL = ('#!/bin/bash\n_want_lsp tsgo "Developer/web" TypeScript\n'
           '_want_lsp "$HOME/.local/opt/intellij-server/current/bin/intellij-server" \\\n    "Developer/app" Kotlin\n')
TOOLCHAIN = '#!/usr/bin/env bash\nif command -v brew >/dev/null 2>&1; then\n  :\nelif command -v uv >/dev/null 2>&1; then\n  :\nfi\n'
NODE = {"name": "x", "dependencies": {"@typescript/native-preview": "latest", "copilot-api": "latest"}}


def build(manifest=MANIFEST, packages=PACKAGES, install=INSTALL, toolchain=TOOLCHAIN, node=NODE):
    write(os.path.join(G, "deps", "manifest.toml"), manifest)
    write(os.path.join(CHEZ, ".chezmoi.toml.tmpl"), packages)
    write(os.path.join(CHEZ, "run_onchange_after_install-packages.sh.tmpl"), install)
    write(os.path.join(CHEZ, "run_onchange_after_install-lsp-toolchain.sh.tmpl"), toolchain)
    write(os.path.join(G, "node-tools", "package.json"), json.dumps(node))


def findings():
    assert inv is not None
    found, _ = inv.run()
    return " | ".join(f["detail"] for f in found)


build()
check("the manifest is a site when a chezmoi source exists", len(inv.sites()) == 1)
check("a matching fixture holds", findings() == "", findings())

build(packages=PACKAGES.replace('"gh"', '"gh", "ripgrep"'))
check("a chezmoi package with no manifest tool and no reason is reported", "ripgrep" in findings(), findings())
build(manifest=MANIFEST + '"ripgrep" = "search tool for people"\n',
      packages=PACKAGES.replace('"gh"', '"gh", "ripgrep"'))
check("an unmanaged reason clears it", "ripgrep" not in findings(), findings())

build(packages=PACKAGES.replace('"jq", ', ""))
check("a manifest tool whose package no chezmoi list carries is reported",
      "jq" in findings() and "no chezmoi list" in findings(), findings())

build(packages=PACKAGES.replace('["iterm2"]', '["iterm2", "obsidian"]'))
check("the OS-specific list is read too", "obsidian" in findings(), findings())

build(install=INSTALL + '_want_lsp csharp-ls "Developer/api" C#\n')
check("a language server the install script verifies must be declared", "csharp-ls" in findings(), findings())
build(install=INSTALL, toolchain=TOOLCHAIN + "if command -v sourcekit-lsp >/dev/null 2>&1; then :; fi\n")
check("a language server the toolchain script checks must be declared", "sourcekit-lsp" in findings(), findings())
build(manifest=MANIFEST + '"csharp-ls" = "not needed here"\n', install=INSTALL + "_want_lsp csharp-ls x C#\n")
check("an unmanaged reason clears a language server", "csharp-ls" not in findings(), findings())
build(install=INSTALL.replace("tsgo", "tsgo-nightly"))
check("a quoted path is compared by its final name", "intellij-server" not in findings(), findings())

build(node={"name": "x", "dependencies": {"@typescript/native-preview": "latest", "copilot-api": "latest",
                                          "new-mcp": "latest"}})
check("a node-tools dependency with no tool and no reason is reported", "new-mcp" in findings(), findings())
build(node={"name": "x", "dependencies": {"copilot-api": "latest"}})
check("a node_package that package.json does not carry is reported", "@typescript/native-preview" in findings(),
      findings())

build(manifest=MANIFEST + '"never-installed" = "left over"\n')
check("an unmanaged entry that names nothing is reported as stale", "never-installed" in findings()
      and "names nothing" in findings(), findings())
build(manifest=MANIFEST.replace('"zsh" = "shell"\n', '"jq" = "shell"\n'))
check("an unmanaged name that is also a tool does not pass", findings() == "", findings())

build(manifest="not = = toml [")
check("an invalid manifest is left to deps-manifest-valid", findings() == "", findings())
build(manifest=MANIFEST.replace('node_package = "@typescript/native-preview"\n', ""))
check("a manifest that fails validation is left to deps-manifest-valid", findings() == "", findings())



build(packages='universal_packages = ["zsh", "jq", "gh"{{ if eq .os "mac" }}, "x"{{ end }}]\n'
                'os_specific_packages = ["iterm2"]\n')
check("a name inside a template expression is not a list entry", "mac" not in findings(), findings())
build(packages='# universal_packages = ["ghost"]\nuniversal_packages = ["zsh", "jq", # drop "ghost2"\n "gh"]\n'
                'os_specific_packages = ["iterm2"]\n')
check("a name in a comment is not a list entry", "ghost" not in findings(), findings())
build(packages='universal_packages = ["zsh", # see [1]\n "jq", "gh"]\nos_specific_packages = ["iterm2"]\n')
check("a bracket in a comment does not cut the list short", findings() == "", findings())
build(packages="universal_packages = ['zsh', 'jq', 'gh']\nos_specific_packages = ['iterm2']\n")
check("single-quoted names are read", findings() == "", findings())
build(install=INSTALL + '  _want_lsp csharp-ls "Developer/api" C#\n')
check("an indented _want_lsp is read", "csharp-ls" in findings(), findings())
build(toolchain=TOOLCHAIN + 'if [ -x x ] && command -v "ghost3" >/dev/null; then :; fi\n'
                            'if command -v -p ghost4; then :; fi\n')
check("command -v is read after other tests and with quotes", "ghost3" in findings(), findings())
check("an option after command -v is not a name", "-p" not in findings().split(), findings())
os.remove(os.path.join(CHEZ, "run_onchange_after_install-packages.sh.tmpl"))
build_missing = findings()
check("a missing install script is reported, not read as empty", "is missing" in build_missing, build_missing)



shutil.rmtree(CHEZ)
check("with no chezmoi source there are no sites", inv.sites() == [])
env = dict(os.environ, AGENT_CONTEXT_STORE=os.path.expanduser("~/.agent-context"),
           AGENT_CONTEXT_CHEZMOI_SOURCE=os.path.join(ROOT, "no-chezmoi-here"))
done = subprocess.run([sys.executable, CHECK, "--json"], capture_output=True, text=True, env=env)
try:
    doc = json.loads(done.stdout)
except ValueError:
    doc = {}
check("the runner lists it as skipped", IDENT in doc.get("skipped", []), done.stdout[-300:] + done.stderr[-300:])
check("a skipped invariant is not a failure", IDENT not in [f["invariant"] for f in doc.get("failing", [])])
plain = subprocess.run([sys.executable, CHECK], capture_output=True, text=True, env=env)
check("the plain report says why", "%s: skipped: needs a chezmoi source" % IDENT in plain.stdout, plain.stdout[-400:])

shutil.rmtree(ROOT, ignore_errors=True)
print("%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
