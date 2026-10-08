#!/usr/bin/env python3
'Edge cases for node-tools-pinned, from the Consolidation Phase 6a adversarial review.\n\nFixture cases build a throwaway store and chezmoi source, point invariant-check at them\nthrough AGENT_CONTEXT_STORE and AGENT_CONTEXT_CHEZMOI_SOURCE, and ask the registered\nInvariant.'

import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")
IDENT = "node-tools-pinned"

tmp = tempfile.mkdtemp(prefix="invariant-node-tools-edges-")
STORE = os.path.join(tmp, "store")
CHEZ = os.path.join(tmp, "chezmoi")
T = os.path.join(STORE, "global", "node-tools")
for d in (os.path.join(STORE, "global", "scripts"), os.path.join(STORE, "global", "hooks"), T,
          os.path.join(CHEZ, "dot_local", "bin")):
    os.makedirs(d)
os.environ["AGENT_CONTEXT_STORE"] = STORE
os.environ["AGENT_CONTEXT_CHEZMOI_SOURCE"] = CHEZ

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def write(root, name, text):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def real(p):
    return os.path.realpath(p)


spec = importlib.util.spec_from_file_location("invariant_check", CHECK)
if spec is None or spec.loader is None:
    sys.exit("cannot load %s" % CHECK)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
found_inv = next((i for i in mod.REGISTRY if i.id == IDENT), None)
if found_inv is None:
    sys.exit("%s is not registered in invariant-check.py" % IDENT)
inv = found_inv


def site_and_reported(path, expect):
    site = real(path) in {real(p) for p in inv.sites()}
    found, _ = inv.run()
    rep = any(real(f["path"]) == real(path) for f in found)
    return site and rep == expect, "site=%s reported=%s" % (site, rep)


BASE = {"name": "fixture", "private": True, "dependencies": {"left-pad": "latest"}}


print("[1] pins hidden outside the dependency fields")
for label, extra in (
    ("an `overrides` pin is reported", {"overrides": {"left-pad": "1.3.0"}}),
    ("a `resolutions` pin is reported", {"resolutions": {"left-pad": "1.3.0"}}),
    ("a `pnpm.overrides` pin is reported", {"pnpm": {"overrides": {"left-pad": "1.3.0"}}}),
    ("a `peerDependencies` range is reported", {"peerDependencies": {"left-pad": "^1.3.0"}}),
):
    data = dict(BASE)
    data.update(extra)
    ok, why = site_and_reported(write(T, "package.json", json.dumps(data)), True)
    check(label, ok, why)
data = dict(BASE, peerDependencies={"left-pad": "latest"})
ok, why = site_and_reported(write(T, "package.json", json.dumps(data)), False)
check("`peerDependencies` at latest passes", ok, why)
write(T, "package.json", json.dumps(BASE))


print("[2] pnpm-workspace.yaml")
CLEAN_WS = ("allowBuilds:\n  geckodriver: false\npatchedDependencies:\n"
            "  copilot-api: patches/copilot-api.patch\n")
ok, why = site_and_reported(write(T, "pnpm-workspace.yaml", CLEAN_WS), False)
check("the store's workspace file (allowBuilds, patchedDependencies) is a site and passes", ok, why)
for label, text in (
    ("a workspace `overrides` pin is reported", CLEAN_WS + "overrides:\n  left-pad: 1.3.0\n"),
    ("a workspace `catalog` is reported", CLEAN_WS + "catalog:\n  left-pad: ^1.3.0\n"),
    ("workspace `catalogs` are reported", CLEAN_WS + "catalogs:\n  tools:\n    left-pad: 1.3.0\n"),
):
    ok, why = site_and_reported(write(T, "pnpm-workspace.yaml", text), True)
    check(label, ok, why)
ok, why = site_and_reported(write(T, "pnpm-workspace.yaml", "# overrides: are forbidden here\n" + CLEAN_WS), False)
check("`overrides` named only in a comment passes", ok, why)
write(T, "pnpm-workspace.yaml", CLEAN_WS)


print("[3] pnpm bootstrap shapes in install-packages")
for label, body, expect in (
    ("`sudo -H npm i -g pnpm` passes", "sudo -H npm i -g pnpm\n", False),
    ("a bootstrap with a trailing comment passes", "npm install -g pnpm@latest  # latest, per user\n", False),
    ("a second global install chained with && is reported",
     "npm install -g pnpm@latest && npm install -g left-pad\n", True),
    ("a second global install chained with ; is reported",
     "npm install -g pnpm@latest; npm i -g left-pad\n", True),
):
    ok, why = site_and_reported(write(CHEZ, "run_onchange_after_install-packages.sh.tmpl", "#!/bin/sh\n" + body), expect)
    check(label, ok, why)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
