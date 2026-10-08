#!/usr/bin/env python3
'Tests for invariant no-retired-script-reference in invariant-check.py.\n\nEach case builds a throwaway store and chezmoi source, points invariant-check at them\nthrough AGENT_CONTEXT_STORE and AGENT_CONTEXT_CHEZMOI_SOURCE, and asks the registered\nInvariant directly. Fixtures are written only inside a temp dir.'

import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")
IDENT = "no-retired-script-reference"

tmp = tempfile.mkdtemp(prefix="invariant-retired-test-")
STORE = os.path.join(tmp, "store")
CHEZ = os.path.join(tmp, "chezmoi")
G = os.path.join(STORE, "global")
S = os.path.join(G, "scripts")
H = os.path.join(G, "hooks")
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
    return path


def real(p):
    return os.path.realpath(p)


spec = importlib.util.spec_from_file_location("invariant_check", CHECK)
if spec is None or spec.loader is None:
    sys.exit("cannot load %s" % CHECK)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

inv = next((i for i in mod.REGISTRY if i.id == IDENT), None)


def why(path):
    return inv.violated(path, mod._read(path)) if inv is not None else None




write(S, "ported-thing.py", "print('ported')\n")
write(H, "ported-hook.py", "print('ported hook')\n")
write(S, "live-shell.sh", "#!/bin/sh\necho live\n")
write(S, "mid-swap.sh", "#!/bin/sh\necho old\n")
write(S, "mid-swap.py", "print('new')\n")


def case(label, fn):
    if inv is None:
        check(label, False, "%s is not registered in invariant-check.py" % IDENT)
        return
    try:
        ok, detail = fn()
    except Exception as exc:
        check(label, False, "raised %s: %s" % (type(exc).__name__, exc))
        return
    check(label, bool(ok), detail)


def violating(name, text, root=H):
    path = write(root, name, text)
    reason = why(path)
    return bool(reason), "violated() returned %r" % (reason,)


def clean(name, text, root=H):
    path = write(root, name, text)
    reason = why(path)
    return not reason, "violated() returned %r" % (reason,)


print(IDENT)

case("a hook running a ported script by its .sh name violates",
     lambda: violating("c1.py", 'subprocess.run(["bash", HOME + "/.claude/scripts/ported-thing.sh"])\n'))

case("a comment naming a ported script's .sh violates too",
     lambda: violating("c2.py", "# see ported-thing.sh for the old behavior\nx = 1\n"))

case("a script naming a ported HOOK's .sh violates",
     lambda: violating("c3.py", "HOOK = 'ported-hook.sh'\n", root=S))

case("a script still present as .sh does not violate",
     lambda: clean("c4.py", 'subprocess.run(["bash", "live-shell.sh"])\n'))

case("a script mid-swap, .sh and .py both present, does not violate",
     lambda: clean("c5.py", "CMD = 'mid-swap.sh'\n"))

case("an unknown .sh the store never had does not violate (upstream stop-hook.sh)",
     lambda: clean("c6.py", "name = 'stop-hook.sh'\n"))


def retired_list():
    names = set(getattr(mod, "_RETIRED_NO_PORT", ()))
    return ({"file-stat", "global-settings-json"} <= names,
            "_RETIRED_NO_PORT is %r" % (sorted(names),))


case("file-stat and global-settings-json are named as retired without a port", retired_list)

case("naming a script retired without a port violates",
     lambda: violating("c7.sh", '. "$HOME/.claude/scripts/file-stat.sh"\n'))

case("a longer stem that merely ends in a ported name does not violate",
     lambda: clean("c8.py", "x = 'pre-ported-thing.sh'\n"))

case("a .shell or .sha suffix is not a .sh reference",
     lambda: clean("c9.py", "x = 'ported-thing.shell'\ny = 'ported-thing.sha'\n"))


def names_replacement():
    path = write(H, "c10.py", "run('ported-thing.sh')\n")
    reason = why(path) or ""
    return ("ported-thing.sh" in reason and "ported-thing.py" in reason,
            "detail %r should name the .sh and its .py replacement" % reason)


case("the violation names the retired file and its replacement", names_replacement)


def site_coverage():
    want = {
        "server source": write(STORE, "server/src/agent_context/daemon.py", "x = 1\n"),
        "server test": write(STORE, "server/tests/test_x.py", "x = 1\n"),
        "project script": write(STORE, "projects/P/scripts/wt-finish.sh", "#!/bin/sh\n"),
        "template settings": write(STORE, "templates/t/dot-agents/claude/settings.json", "{}\n"),
        "hooks manifest": write(G, "hooks-manifest.json", "{}\n"),
        "script sidecar": write(S, "ported-thing.py.meta.toml", "description = 'x'\n"),
        "hook": write(H, "some-hook.py", "x = 1\n"),
        "chezmoi bin": write(B, "executable_tool", "#!/bin/sh\n"),
    }
    skip = {
        "audit observation": write(G, "audit-observations/0001.json", "{}\n"),
        "doc": write(G, "docs/a.md", "text\n"),
        "worktree copy": write(STORE, ".claude/worktrees/w/global/hooks/h.py", "x = 1\n"),
    }
    sites = {real(p) for p in inv.sites()} if inv is not None else set()
    missing = [k for k, p in want.items() if real(p) not in sites]
    extra = [k for k, p in skip.items() if real(p) in sites]
    return (not missing and not extra, "missing %s, wrongly included %s" % (missing, extra))


case("sites cover hooks, scripts, sidecars, server, projects, templates, manifests and "
     "chezmoi, and skip observations, docs and worktrees", site_coverage)


def reported_by_run():
    path = write(STORE, "projects/P/scripts/wt-sweep-caller.sh",
                 '[ -f "$main/.claude/scripts/ported-thing.sh" ] && bash ported-thing.sh\n')
    found, _ = inv.run() if inv is not None else ([], 0)
    return (any(real(f["path"]) == real(path) for f in found),
            "run() did not report %s" % path)


case("run() reports a violating project script", reported_by_run)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
