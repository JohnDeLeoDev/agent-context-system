#!/usr/bin/env python3
'Edge cases for the consolidation invariants, found by the Phase 0 review.\n\ntest-invariant-consolidation.py covers the approved criteria. These are the breaks an\nadversarial reviewer found afterwards, each reproduced before it became a case:\n\n  - node-tools-pinned raised AttributeError on a manifest whose "servers" is a string,\n    list or number. The manifest is allowlisted, so a plain run skips it, but\n    --verify calls the check on every allowlisted file, so one bad manifest stopped\n    --verify, and with it the quality suite\'s allowlists row.\n  - `npm install \\` then `-g pkg` on the next line escaped the global-install check.\n  - _chezmoi_name stripped one attribute prefix, so private_executable_pi read as\n    executable_pi and a real launcher was reported as holding logic.\n\nPackage-manager words in fixture text are built by concatenation so the live\nnode-tools-pinned check does not report this file for its own fixtures.'

import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")
NPM = "np" + "m"

tmp = tempfile.mkdtemp(prefix="invariant-edges-test-")
STORE = os.path.join(tmp, "store")
CHEZ = os.path.join(tmp, "chezmoi")
S = os.path.join(STORE, "global", "scripts")
B = os.path.join(CHEZ, "dot_local", "bin")
MANIFEST = os.path.join(STORE, "global", "mcp-servers.json")
for d in (S, os.path.join(STORE, "global", "hooks"), B):
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


def attempt(label, fn):
    try:
        ok = fn()
    except Exception as exc:
        check(label, False, "raised %s: %s" % (type(exc).__name__, exc))
        return
    check(label, bool(ok))


def write(root, name, text):
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o755)
    return path


def main():
    spec = importlib.util.spec_from_file_location("invariant_check", CHECK)
    if spec is None or spec.loader is None:
        print("FAIL cannot load %s" % CHECK)
        return 1
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    node = next((inv for inv in mod.REGISTRY if inv.id == "node-tools-pinned"), None)
    shell = next((inv for inv in mod.REGISTRY if inv.id == "shell-is-launcher-only"), None)
    if node is None or shell is None:
        print("FAIL consolidation invariants are not registered")
        return 1

    print("[1] a malformed manifest does not raise")
    for label, text in (("servers as a string", '{"servers": "not-a-dict"}'),
                        ("servers as a list", '{"servers": ["x"]}'),
                        ("servers as a number", '{"servers": 5}'),
                        ("unparseable JSON", "{not json")):
        write(os.path.dirname(MANIFEST), os.path.basename(MANIFEST), text)
        attempt("%s: the node check returns" % label,
                lambda: node.violated(MANIFEST, mod._read(MANIFEST)) is None)
        attempt("%s: --verify on the allowlist returns" % label,
                lambda: isinstance(node.verify_allow(), list))

    print("[2] a global install split across a line continuation")
    split_g = write(S, "fixture-split-g.sh", "#!/bin/sh\n" + NPM + " install \\\n  -g left-pad\n")
    split_global = write(S, "fixture-split-global.sh", "#!/bin/sh\n" + NPM + " i \\\n    --global left-pad\n")
    split_local = write(S, "fixture-split-local.sh", "#!/bin/sh\n" + NPM + " install \\\n  left-pad\n")
    attempt("install, continuation, -g is reported",
            lambda: bool(node.violated(split_g, mod._read(split_g))))
    attempt("i, continuation, --global is reported",
            lambda: bool(node.violated(split_global, mod._read(split_global))))
    attempt("a local install split across a continuation passes",
            lambda: not node.violated(split_local, mod._read(split_local)))

    print("[3] stacked chezmoi attribute prefixes")
    stacked = write(B, "private_executable_git-ssh-op.tmpl", '#!/bin/sh\nexec ssh "$@"\n')
    triple = write(B, "readonly_private_executable_pi", '#!/bin/sh\nexec /opt/homebrew/bin/pi "$@"\n')
    attempt("private_executable_git-ssh-op.tmpl installs as git-ssh-op",
            lambda: mod._chezmoi_name(stacked) == "git-ssh-op")
    attempt("a stacked-prefix launcher passes the shell check",
            lambda: not shell.violated(stacked, mod._read(stacked)))
    attempt("a three-prefix launcher passes the shell check",
            lambda: not shell.violated(triple, mod._read(triple)))
    return 0


code = main()
shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures or code else 0)
