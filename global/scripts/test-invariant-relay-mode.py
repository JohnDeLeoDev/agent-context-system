#!/usr/bin/env python3
'Tests for relay-aware mode in invariant-check.py.\n\nA machine on the network relay has no store checkout: its ~/.agent-context holds only\nthe bundle projection (global/{hooks,scripts,skills,commands,agents,manifests},\nshared-docs), with no .git and no server/. "No store checkout" means the store root has\nno .git entry AND no server/ directory. In that mode every invariant that needs the\ncheckout prints `skipped: needs a store checkout` and does not count as a violation or\naffect the exit status; every other invariant runs as before. In checkout mode nothing\nis skipped. The classification lives in one table next to REGISTRY\n(NEEDS_STORE_CHECKOUT: invariant id -> bool), so a new invariant without an entry fails\nhere.\n\nEach case builds a throwaway store under ~/.cache/tmp from a copy of this store\'s own\nbundle directories, points invariant-check at it through AGENT_CONTEXT_STORE, and runs\nthe real script.'

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")
REAL_GLOBAL = os.path.dirname(HERE)
SKIP_TEXT = "skipped: needs a store checkout"
BUNDLE_DIRS = ("hooks", "scripts", "skills", "commands", "agents", "manifests")

scratch_root = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
os.makedirs(scratch_root, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="invariant-relay-mode-test-", dir=scratch_root)

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


def build_projection(name):
    'A store root holding only the bundle directories, as a relay machine has.'
    root = os.path.join(tmp, name)
    for sub in BUNDLE_DIRS:
        src = os.path.join(REAL_GLOBAL, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(root, "global", sub), symlinks=True)
    os.makedirs(os.path.join(root, "shared-docs"), exist_ok=True)
    return root


def run_check(store):
    env = dict(os.environ)
    env["AGENT_CONTEXT_STORE"] = store
    env["AGENT_CONTEXT_CHEZMOI_SOURCE"] = os.path.join(tmp, "no-chezmoi")
    env["HOME"] = os.path.join(tmp, "home")
    os.makedirs(env["HOME"], exist_ok=True)
    proc = subprocess.run([sys.executable, CHECK], env=env, capture_output=True,
                          text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def violated(out, invariant_id):
    return ("=== %s --" % invariant_id) in out


def skipped(out, invariant_id):
    return any(invariant_id in line and SKIP_TEXT in line for line in out.splitlines())


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


CHECKOUT_ONLY = ("hook-fingerprint-matches", "fleet-row-fields-earn-their-place",
                 "store-entity-body-not-executable")
NON_CHECKOUT = "no-claude-projection-paths"

try:
    print("relay home (no .git, no server/)")
    relay = build_projection("relay")
    code, out = run_check(relay)
    check("clean relay projection exits 0", code == 0,
          "exit %d; %s" % (code, out[:600]))
    check("clean relay projection reports no violation groups", "===" not in out,
          out[:600])

    hooks_dir = os.path.join(relay, "global", "hooks")
    some_hook = sorted(n for n in os.listdir(hooks_dir) if n.endswith(".py"))[0]
    os.chmod(os.path.join(hooks_dir, some_hook), 0o755)
    write(os.path.join(relay, "global", "scripts", "stale-path-fixture.py"),
          "#!/usr/bin/env python3\nPATH = '~/.claude/hooks/guard.py'\n")
    code, out = run_check(relay)
    for ident in CHECKOUT_ONLY:
        check("relay skips %s" % ident, skipped(out, ident), out[:800])
        check("relay does not report %s as a violation" % ident,
              not violated(out, ident))
    check("relay still reports %s as a violation" % NON_CHECKOUT,
          violated(out, NON_CHECKOUT), out[:800])
    check("relay with a real violation exits nonzero", code != 0, "exit %d" % code)

    print("checkout (has .git and server/)")
    checkout = build_projection("checkout")
    os.makedirs(os.path.join(checkout, ".git"))
    os.makedirs(os.path.join(checkout, "server"))
    os.chmod(os.path.join(checkout, "global", "hooks", some_hook), 0o755)
    code, out = run_check(checkout)
    check("checkout skips nothing", SKIP_TEXT not in out, out[:800])
    for ident in CHECKOUT_ONLY:
        check("checkout reports %s as a violation" % ident, violated(out, ident),
              out[:800])
    check("checkout with violations exits nonzero", code != 0, "exit %d" % code)

    print("classification table")
    spec = importlib.util.spec_from_file_location("invariant_check_relay", CHECK)
    if spec is None or spec.loader is None:
        sys.exit("cannot load %s" % CHECK)
    os.environ["AGENT_CONTEXT_STORE"] = relay
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    table = getattr(module, "NEEDS_STORE_CHECKOUT", None)
    check("NEEDS_STORE_CHECKOUT exists next to REGISTRY", isinstance(table, dict))
    if isinstance(table, dict):
        registered = [inv.id for inv in module.REGISTRY]
        missing = [i for i in registered if i not in table]
        stale = [i for i in table if i not in registered]
        check("every registered invariant is classified", not missing, str(missing))
        check("no classification names an unregistered invariant", not stale,
              str(stale))
        check("every classification is a bool",
              all(isinstance(v, bool) for v in table.values()))
        for ident in CHECKOUT_ONLY:
            check("%s is classified as needing a checkout" % ident,
                  table.get(ident) is True)
        check("%s is classified as not needing a checkout" % NON_CHECKOUT,
              table.get(NON_CHECKOUT) is False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

total = passed + len(failures)
print("test-invariant-relay-mode: %d/%d passed" % (passed, total))
sys.exit(1 if failures else 0)
