#!/usr/bin/env python3
'hook-test-run.py gave every hook case\'s `pre` setup commands an environment with\nGIT_CONFIG_KEY_n = commit.gpgsign and GIT_CONFIG_VALUE_n = "false" on separate lines, so\nevery fixture commit a hook case made was unsigned, and the invariant\'s pattern (key and\nvalue adjacent) never saw it. The pattern also took only `false`, not git\'s other\nboolean spellings (0, no, off), and no colon form.\n\nCriteria:\n  1. The env-pair shape is flagged: a loop, a dict, and a shell assignment.\n  2. 0 / no / off and `gpgsign: false` are flagged.\n  3. An env pair that turns signing ON, and a false value for an unrelated key beside a\n     signing-on flag, are not.\n  4. hook-test-run.py\'s setup environment makes SIGNED commits, even under a global\n     config that disables signing.\nA separate battery because test-invariant-unsigned-gitconfig.py is locked. Same PASS/FAIL\nstyle and exit code.'
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

STORE = os.path.expanduser("~/.agent-context")
SCRIPTS = os.path.join(STORE, "global", "scripts")
TMP_BASE = os.path.join(os.path.expanduser("~"), ".cache", "tmp")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, filename))
    assert spec is not None and spec.loader is not None, "cannot load " + filename
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inv = load("invariant_check_env_pairs", "invariant-check.py")
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


FLAGGED = {
    "GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n set on separate lines in a loop":
        'for key in ("commit.gpgsign", "tag.gpgsign"):\n'
        '    out["GIT_CONFIG_KEY_%d" % n] = key\n'
        '    out["GIT_CONFIG_VALUE_%d" % n] = "false"\n'
        '    n += 1\n',
    "an env dict pairing commit.gpgsign with false":
        'env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "commit.gpgsign",\n'
        '       "GIT_CONFIG_VALUE_0": "false"}\n',
    "a shell assignment pairing commit.gpgsign with false":
        'os.system("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=commit.gpgsign GIT_CONFIG_VALUE_0=false git commit -m x")\n',
    "-c commit.gpgsign=0":
        'subprocess.run(["git", "-c", "commit.gpgsign=0", "commit", "-m", "x"])\n',
    "-c commit.gpgsign=no":
        'subprocess.run(["git", "-c", "commit.gpgsign=no", "commit", "-m", "x"])\n',
    "-c commit.gpgsign=off":
        'subprocess.run(["git", "-c", "commit.gpgsign=off", "commit", "-m", "x"])\n',
    "a colon form (gpgsign: false)":
        'text = "commit:\\n  gpgsign: false\\n"\n',
}

CLEAN = {
    "an env pair that turns signing on":
        'env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "commit.gpgsign",\n'
        '       "GIT_CONFIG_VALUE_0": "true"}\n',
    "a false value for an unrelated key beside a signing-on flag":
        'env = {"GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false"}\n'
        'subprocess.run(["git", "-c", "commit.gpgsign=true", "commit"], env=env)\n',
    "a word that only starts with off (gpgsign offset)":
        'label = "commit.gpgsign offset"\n',
}

print("invariant no-unsigned-commit-fallback: env pairs and boolean spellings")
for label, src in FLAGGED.items():
    got = inv._has_unsigned_commit_fallback("fixture.py", src)
    check("flags " + label, bool(got), "got %r" % (got,))
for label, src in CLEAN.items():
    got = inv._has_unsigned_commit_fallback("fixture.py", src)
    check("leaves alone " + label, not got, "got %r" % (got,))



print("\n[hook-test-run] a case's setup commands make signed commits")
runner = load("hook_test_run_env_pairs", "hook-test-run.py")
setup_env = getattr(runner, "_signed", None) or getattr(runner, "_unsigned")
os.makedirs(TMP_BASE, exist_ok=True)
work = tempfile.mkdtemp(dir=TMP_BASE, prefix="htr-sign-")
try:
    home = os.path.join(work, "home")
    os.makedirs(home)
    with open(os.path.join(home, ".gitconfig"), "w", encoding="utf-8") as fh:
        fh.write("[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgsign = off\n")
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("GIT_CONFIG_", "GIT_DIR", "GIT_WORK_TREE"))}
    base.update({"HOME": home, "GIT_CONFIG_NOSYSTEM": "1"})
    env = setup_env(base)
    repo = os.path.join(work, "repo")
    script = ("git init -q repo && cd repo && printf 'x\\n' > f && git add f "
              "&& git commit -qm fixture && git cat-file commit HEAD")
    p = subprocess.run(["bash", "-c", script], cwd=work, env=env,
                       capture_output=True, text=True, timeout=60)
    check("the setup commit succeeds", p.returncode == 0, p.stderr.strip()[:200])
    check("the setup commit carries a signature (gpgsig header)",
          "\ngpgsig " in p.stdout, "commit object: %r" % p.stdout[:200])
finally:
    shutil.rmtree(work, ignore_errors=True)

print("\ninvariant-unsigned-env-pairs: %d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
