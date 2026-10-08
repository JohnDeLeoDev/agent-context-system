#!/usr/bin/env python3
"Tests for the two consolidation invariants in invariant-check.py.\n\nThe consolidation plan (store doc consolidation/plan.md) moves all tooling logic to\nPython, keeps POSIX sh only for launchers, and moves Node tools to pinned pnpm\ninstalls. Phase 0 adds two invariants so the old patterns cannot grow back while the\nport runs:\n\n  shell-is-launcher-only  every shell file is a named launcher: POSIX sh and at most\n                          25 code lines. Today's logic files are allowlisted by name,\n                          each tagged with the phase that ports it, so --verify flags\n                          the entry once the file is gone.\n  node-tools-pinned       no code runs npx or a global npm install, and no MCP\n                          manifest server runs npx or fetches @latest.\n\nEach case builds a throwaway store and chezmoi source, points invariant-check at them\nthrough AGENT_CONTEXT_STORE and AGENT_CONTEXT_CHEZMOI_SOURCE, and asks the registered\nInvariant directly. Fixtures are written only inside a temp dir; the one read outside\nit is the default chezmoi path, to prove the override leaves the default alone."

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")

tmp = tempfile.mkdtemp(prefix="invariant-consolidation-test-")
STORE = os.path.join(tmp, "store")
CHEZ = os.path.join(tmp, "chezmoi")
S = os.path.join(STORE, "global", "scripts")
H = os.path.join(STORE, "global", "hooks")
B = os.path.join(CHEZ, "dot_local", "bin")
for d in (S, H, B):
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


def code(n):
    'n shell code lines.'
    return "".join('echo "step %d"\n' % i for i in range(n))


def real(p):
    return os.path.realpath(p)


spec = importlib.util.spec_from_file_location("invariant_check", CHECK)
if spec is None or spec.loader is None:
    sys.exit("cannot load %s" % CHECK)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def invariant(ident):
    return next((inv for inv in mod.REGISTRY if inv.id == ident), None)


def site_set(inv):
    return {real(p) for p in inv.sites()}


def violates(inv, path):
    return bool(inv.violated(path, mod._read(path)))


def reported(inv, path):
    found, _ = inv.run()
    return any(real(f["path"]) == real(path) for f in found)


def phase_tagged(inv):
    
    
    return all(re.search(r"\bPhase [0-9]\b", why) for why in inv.allow.values())


def run_group(title, ident, build):
    print(title)
    inv = invariant(ident)
    for label, fn in build():
        if inv is None:
            check(label, False, "%s is not registered in invariant-check.py" % ident)
            continue
        try:
            ok = fn(inv)
        except Exception as exc:
            check(label, False, "raised %s: %s" % (type(exc).__name__, exc))
            continue
        check(label, bool(ok))






def shell_cases():
    logic_script = write(S, "fixture-logic.sh",
                         '#!/bin/sh\nset -eu\nif [ -n "${1:-}" ]; then\n  echo "$1"\nfi\n')
    logic_hook = write(H, "fixture-hook.sh",
                       '#!/usr/bin/env bash\nread -r payload\n[ -n "$payload" ] || exit 0\necho ok\n')
    no_shebang = write(H, "fixture-noshebang.sh", "echo hi\n")
    posix_launcher = write(B, "executable_token-usage-collect",
                           '#!/bin/sh\n# launch the collector\n'
                           'exec python3 "$HOME/.claude/scripts/token-usage-collect.py" "$@"\n')
    bash_launcher = write(B, "executable_pi", '#!/usr/bin/env bash\nexec /opt/homebrew/bin/pi "$@"\n')
    long_launcher = write(B, "executable_derived-data-prune", "#!/bin/sh\n" + code(30) + "exec true\n")
    edge_launcher = write(B, "executable_intellij-server-refresh", "#!/bin/sh\n" + code(24) + "exec true\n")
    commented = write(B, "executable_agent-notify-watch",
                      "#!/bin/sh\n" + "# a note about this launcher\n\n" * 40 + code(9) + "exec true\n")
    py_bin = write(B, "executable_fixture-py", "#!/usr/bin/env python3\nprint('hi')\n")
    py_script = write(S, "fixture-tool.py", "#!/usr/bin/env python3\nprint('hi')\n")

    def chezmoi_absent(inv):
        os.environ["AGENT_CONTEXT_CHEZMOI_SOURCE"] = os.path.join(tmp, "absent")
        try:
            sites = site_set(inv)
            return real(logic_script) in sites and not any(p.startswith(real(CHEZ)) for p in sites)
        finally:
            os.environ["AGENT_CONTEXT_CHEZMOI_SOURCE"] = CHEZ

    def default_path_unchanged(inv):
        saved = os.environ.pop("AGENT_CONTEXT_CHEZMOI_SOURCE")
        try:
            default = real(os.path.expanduser("~/.local/share/chezmoi"))
            return all(real(p).startswith(default) for p in mod._chezmoi_sources())
        finally:
            os.environ["AGENT_CONTEXT_CHEZMOI_SOURCE"] = saved

    def stale_allow(inv):
        
        
        gone = "executable_fixture-gone-launcher"
        inv.allow[gone] = "Phase 8: fixture entry for a file that does not exist"
        try:
            return any(base == gone and "no longer exists" in why
                       for base, why in inv.verify_allow())
        finally:
            inv.allow.pop(gone, None)

    return [
        ("a store script .sh is a site", lambda inv: real(logic_script) in site_set(inv)),
        ("a store hook .sh is a site", lambda inv: real(logic_hook) in site_set(inv)),
        ("a chezmoi bin file with an sh shebang is a site", lambda inv: real(posix_launcher) in site_set(inv)),
        ("a chezmoi bin file with a bash shebang is a site", lambda inv: real(bash_launcher) in site_set(inv)),
        ("a chezmoi bin file with a python shebang is not a site", lambda inv: real(py_bin) not in site_set(inv)),
        ("a store .py script is not a site", lambda inv: real(py_script) not in site_set(inv)),
        ("a new shell script with logic is reported", lambda inv: reported(inv, logic_script)),
        ("a new shell hook with logic is reported", lambda inv: reported(inv, logic_hook)),
        ("a .sh file with no shebang is reported", lambda inv: reported(inv, no_shebang)),
        ("a named POSIX sh launcher passes", lambda inv: not violates(inv, posix_launcher)),
        ("a named launcher with a bash shebang is reported", lambda inv: violates(inv, bash_launcher)),
        ("a named launcher over 25 code lines is reported", lambda inv: violates(inv, long_launcher)),
        ("a named launcher at exactly 25 code lines passes", lambda inv: not violates(inv, edge_launcher)),
        ("comments and blank lines do not count toward the 25", lambda inv: not violates(inv, commented)),
        ("every allowlist reason names its plan phase (an empty list passes)", phase_tagged),
        ("--verify reports an allowlisted file that no longer exists", stale_allow),
        ("a missing chezmoi source leaves the store sites checked, with no crash", chezmoi_absent),
        ("with no override the chezmoi default path is unchanged", default_path_unchanged),
    ]






MANIFEST = os.path.join(STORE, "global", "mcp-servers.json")


def put_manifest(servers):
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump({"$comment": "fixture", "servers": servers}, fh)
    return MANIFEST


def node_cases():
    put_manifest({"fixture": {"command": "/bin/bash", "args": ["run.sh"]}})
    npx_py = write(S, "fixture-npx.py",
                   '#!/usr/bin/env python3\nimport subprocess\nsubprocess.run(["npx", "left-pad"], check=False)\n')
    prose_py = write(S, "fixture-npx-prose.py",
                     '#!/usr/bin/env python3\n"""Mentions npx left-pad@latest and npm install -g left-pad in prose."""\n'
                     '# npx left-pad\nprint("ok")\n')
    npx_ts = write(S, "fixture-ext.ts", 'import { spawn } from "child_process";\nspawn("npx", ["left-pad"]);\n')
    npm_g = write(S, "fixture-npm-global.sh", "#!/bin/sh\nnpm install -g left-pad\n")
    npm_i_g = write(H, "fixture-npm-i-g.sh", "#!/bin/sh\nnpm i -g left-pad\n")
    npm_long = write(B, "executable_fixture-npm-global", "#!/bin/sh\nnpm install --global left-pad\n")
    npm_local = write(S, "fixture-npm-local.sh", "#!/bin/sh\nnpm install left-pad\n")
    hint = write(S, "fixture-hint.sh",
                 '#!/bin/sh\necho "Install: npm install -g @typescript/native-preview" >&2\n')
    self_copy = write(S, "invariant-check.py", mod._read(CHECK))

    def manifest_case(servers, expect):
        def fn(inv):
            put_manifest(servers)
            return violates(inv, MANIFEST) == expect
        return fn

    return [
        ("a store .py script is a site", lambda inv: real(npx_py) in site_set(inv)),
        ("a store .ts script is a site", lambda inv: real(npx_ts) in site_set(inv)),
        ("a store hook is a site", lambda inv: real(npm_i_g) in site_set(inv)),
        ("a chezmoi bin file is a site", lambda inv: real(npm_long) in site_set(inv)),
        ("the MCP manifest is a site", lambda inv: real(MANIFEST) in site_set(inv)),
        ("invariant-check.py itself is not a site", lambda inv: real(self_copy) not in site_set(inv)),
        ("npx in Python code is reported", lambda inv: violates(inv, npx_py)),
        ("npx in TypeScript code is reported", lambda inv: violates(inv, npx_ts)),
        ("npx and npm -g in comments and docstrings pass", lambda inv: not violates(inv, prose_py)),
        ("npm install -g is reported", lambda inv: violates(inv, npm_g)),
        ("npm i -g is reported", lambda inv: violates(inv, npm_i_g)),
        ("npm install --global is reported", lambda inv: violates(inv, npm_long)),
        ("a project-local npm install passes", lambda inv: not violates(inv, npm_local)),
        ("a hint telling a human to npm install -g is reported", lambda inv: violates(inv, hint)),
        ("a manifest server launched through npx is reported",
         manifest_case({"x": {"command": "npx", "args": ["-y", "pkg@1.2.3", "mcp"]}}, True)),
        ("a manifest arg ending in @latest is reported",
         manifest_case({"x": {"command": "pnpm", "args": ["dlx", "pkg@latest"]}}, True)),
        ("npx and @latest inside a manifest $comment pass",
         manifest_case({"x": {"$comment": "was npx pkg@latest", "command": "/bin/bash", "args": ["run.sh"]}}, False)),
        ("a manifest server on a pinned installed bin passes",
         manifest_case({"x": {"command": "/opt/tools/node_modules/.bin/pkg", "args": ["mcp"]}}, False)),
        ("every allowlist reason names its plan phase (an empty list passes)", phase_tagged),
    ]


run_group("[1] shell-is-launcher-only", "shell-is-launcher-only", shell_cases)
run_group("[2] node-tools-pinned", "node-tools-pinned", node_cases)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
