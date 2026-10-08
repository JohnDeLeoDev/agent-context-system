#!/usr/bin/env python3
"Tests for Consolidation Phase 6a's changes to node-tools-pinned in invariant-check.py.\n\nFixture cases build a throwaway store and chezmoi source, point invariant-check at them\nthrough AGENT_CONTEXT_STORE and AGENT_CONTEXT_CHEZMOI_SOURCE, and ask the registered\nInvariant. The allowlist cases read the real module constants."

import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")
IDENT = "node-tools-pinned"

tmp = tempfile.mkdtemp(prefix="invariant-node-tools-test-")
STORE = os.path.join(tmp, "store")
CHEZ = os.path.join(tmp, "chezmoi")
S = os.path.join(STORE, "global", "scripts")
H = os.path.join(STORE, "global", "hooks")
T = os.path.join(STORE, "global", "node-tools")
for d in (S, H, T, os.path.join(CHEZ, "dot_local", "bin")):
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
    os.chmod(path, 0o755)
    return path


def real(p):
    return os.path.realpath(p)


spec = importlib.util.spec_from_file_location("invariant_check", CHECK)
if spec is None or spec.loader is None:
    sys.exit("cannot load %s" % CHECK)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

inv = next((i for i in mod.REGISTRY if i.id == IDENT), None)
if inv is None:
    sys.exit("%s is not registered in invariant-check.py" % IDENT)


def site_set():
    assert inv is not None
    return {real(p) for p in inv.sites()}


def reported(path):
    assert inv is not None
    found, _ = inv.run()
    return any(real(f["path"]) == real(path) for f in found)


def is_site_and_reported(path, expect):
    site = real(path) in site_set()
    rep = reported(path)
    return site and rep == expect, "site=%s reported=%s" % (site, rep)


LATEST = {"name": "fixture", "private": True, "dependencies": {"left-pad": "latest"}}


print("[1] sites")
prov = write(CHEZ, "run_onchange_after_fixture-provision.sh.tmpl",
             "#!/bin/sh\nnpm install -g @typescript/native-preview || true\n")
tools = write(T, "package.json", json.dumps(LATEST))
check("a chezmoi run_* provisioning script is a site", real(prov) in site_set())
check("the store's pnpm tools manifest is a site", real(tools) in site_set())


print("[2] global npm installs in provisioning")
ok, why = is_site_and_reported(prov, True)
check("npm install -g in a run_* script is reported", ok, why)


def install_packages(body):
    return write(CHEZ, "run_onchange_after_install-packages.sh.tmpl", "#!/bin/sh\n" + body)


for label, body, expect in (
    ("the pnpm bootstrap `npm install -g pnpm@latest` passes",
     "npm install -g pnpm@latest || true\n", False),
    ("the pnpm bootstrap under sudo passes",
     "sudo npm install -g pnpm@latest\n", False),
    ("the pnpm bootstrap `npm install --global pnpm` inside a guard passes",
     "if ! command -v pnpm >/dev/null 2>&1; then\n    sudo npm install --global pnpm\nfi\n", False),
    ("a pinned pnpm bootstrap is reported (pnpm tracks the latest release)",
     "npm install -g pnpm@10.0.0\n", True),
    ("a bootstrap that also installs another package is reported",
     "npm install -g pnpm@latest left-pad\n", True),
    ("another global install beside the bootstrap is reported",
     "npm install -g pnpm@latest\nnpm install -g @typescript/native-preview\n", True),
    ("npx in install-packages is reported",
     "npx left-pad\n", True),
):
    ok, why = is_site_and_reported(install_packages(body), expect)
    check(label, ok, why)
install_packages("echo clean\n")

store_boot = write(S, "fixture-pnpm-bootstrap.sh", "#!/bin/sh\nnpm install -g pnpm@latest\n")
check("the pnpm bootstrap outside install-packages is still reported", reported(store_boot))
os.remove(store_boot)


print("[3] every tool floats to latest")


def put_tools(data):
    text = data if isinstance(data, str) else json.dumps(data)
    return write(T, "package.json", text)


p = put_tools({"name": "fixture", "private": True, "agentContextBins": ["a"],
               "dependencies": {"a": "latest", "@scope/b": "latest"},
               "devDependencies": {"c": "latest"}, "optionalDependencies": {"d": "latest"}})
ok, why = real(p) in site_set() and not reported(p), "reported=%s" % reported(p)
check("every specifier `latest` passes", ok, why)

for bad in ("1.2.3", "7.0.0-dev.20260707.2", "^1.2.3", "~1.2.3", "*", ">=1.0.0", "1.x", "next",
            "github:user/repo", "file:../pkg", "npm:left-pad@latest", "https://example.com/p.tgz", ""):
    p = put_tools({"name": "fixture", "private": True, "dependencies": {"a": "latest", "left-pad": bad}})
    ok, why = is_site_and_reported(p, True)
    check("dependency specifier %r is reported" % bad, ok, why)

for field in ("devDependencies", "optionalDependencies"):
    p = put_tools({"name": "fixture", "private": True, field: {"left-pad": "1.3.0"}})
    ok, why = is_site_and_reported(p, True)
    check("a pin in %s is reported" % field, ok, why)

p = put_tools({"name": "fixture", "private": True, "dependencies": {"left-pad": "1.3.0"}})
msg = inv.violated(p, mod._read(p)) or ""
check("the finding names the pinned package", "left-pad" in msg, repr(msg))

for label, text in (("a malformed tools manifest does not crash the check", "{not json"),
                    ("a tools manifest whose dependencies is not an object does not crash the check",
                     json.dumps({"dependencies": ["left-pad"]}))):
    p = put_tools(text)
    try:
        inv.violated(p, mod._read(p))
        inv.run()
        check(label, real(p) in site_set(), "not a site")
    except Exception as ex:  
        check(label, False, repr(ex))
put_tools(LATEST)


print("[4] allowlists")
node_allow = getattr(mod, "_NODE_ALLOW", None)
shell_allow = getattr(mod, "_SHELL_ALLOW", None)
check("_NODE_ALLOW no longer lists mcp-servers.json",
      isinstance(node_allow, dict) and "mcp-servers.json" not in node_allow, repr(node_allow))
check("_NODE_ALLOW no longer lists executable_build-copilot-api",
      isinstance(node_allow, dict) and "executable_build-copilot-api" not in node_allow, repr(node_allow))
check("no _NODE_ALLOW reason is tagged Phase 6",
      isinstance(node_allow, dict) and all("Phase 6" not in why for why in node_allow.values()), repr(node_allow))
check("_SHELL_ALLOW no longer lists executable_build-copilot-api",
      isinstance(shell_allow, dict) and "executable_build-copilot-api" not in shell_allow,
      "not a dict" if not isinstance(shell_allow, dict) else "still listed")

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
