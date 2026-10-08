#!/usr/bin/env python3
"Runs the STORE's project-materialize.py (AGENT_CONTEXT_STORE or ~/.agent-context) against a\nfixture HOME, fixture store and fixture git project checkout. Never touches a real checkout.\n\nEach case builds its own fixture under a temp dir (TMPDIR, inside home). A case runs the\nscript once or twice (apply, then apply/--check) and inspects the resulting .agents/ tree\nand process output directly -- there is no parity harness involved here, just the script.\n\nMessage strings asserted here (CHECK_SUMMARY_SUBSTR, CHECK_LINE_SUBSTR, APPLY_LINE_SUBSTR)\nare the contract Stage 2 must implement; they do not exist in the current script, so every\ncase that asserts on them is expected to fail until then."
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
SCRIPT = os.path.join(STORE, "global", "scripts", "project-materialize.py")

STUB = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"


CHECK_LINE_SUBSTR = "pending retirement"
APPLY_LINE_SUBSTR = "retired (no longer in store"
OLD_EXTRA_SUBSTR = "disk-only, not in store"

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


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True, text=True)


def new_fixture(tag):
    "Fixture HOME with a store at HOME/.agent-context (so global-twin lookups resolve) and\n    one project 'proj' with a matching git checkout at HOME/work/proj."
    home = tempfile.mkdtemp(prefix="pm-retire-%s-" % tag)
    store = os.path.join(home, ".agent-context")
    for name in ("lsp-plugin-guard.py", "project-settings-sync.py", "agents-materialize.py"):
        write_file(os.path.join(store, "global", "scripts", name), STUB)
        os.chmod(os.path.join(store, "global", "scripts", name), 0o755)
    write_file(os.path.join(store, "projects", "proj", "project.toml"),
               'uuid = "10000000-0000-0000-0000-000000000001"\n'
               'display_name = "Proj"\n')
    project_dir = os.path.join(home, "work", "proj")
    os.makedirs(project_dir, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=project_dir)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=project_dir)
    return home, store, project_dir


def run_pm(home, args):
    tmp = os.path.join(home, ".cache", "tmp")
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = home
    env["TMPDIR"] = tmp
    env.pop("AGENT_CONTEXT_STORE", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    return subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True, env=env)


def store_script(store, name, content="#!/usr/bin/env bash\necho hi\n"):
    write_file(os.path.join(store, "projects", "proj", "scripts", name), content)


def remove_store_script(store, name):
    os.remove(os.path.join(store, "projects", "proj", "scripts", name))


def agents_path(project_dir, *parts):
    return os.path.join(project_dir, ".agents", *parts)


fixtures = []


def make(tag):
    fx = new_fixture(tag)
    fixtures.append(fx[0])
    return fx


print("project-materialize retirement")


home, store, project = make("basic")
store_script(store, "foo.sh")
r1 = run_pm(home, [project])
check("setup: first apply materializes foo.sh", os.path.isfile(agents_path(project, "scripts", "foo.sh")),
      "rc %d stderr %r" % (r1.returncode, r1.stderr[-300:]))
remove_store_script(store, "foo.sh")
r2 = run_pm(home, [project])
check("1: apply deletes a retired disk file (manifest sha matches, store dropped it)",
      not os.path.isfile(agents_path(project, "scripts", "foo.sh")),
      "still present after second apply, rc %d, stdout %r" % (r2.returncode, r2.stdout[-400:]))
rescued = []
rescue_root = agents_path(project, "tmp", "materialize-rescue")
if os.path.isdir(rescue_root):
    for batch in os.listdir(rescue_root):
        cand = os.path.join(rescue_root, batch, "scripts", "foo.sh")
        if os.path.isfile(cand):
            rescued.append(cand)
check("2: retired file is copied into a materialize-rescue batch before deletion",
      bool(rescued), "no rescued copy found under %s" % rescue_root)


home, store, project = make("edited")
store_script(store, "bar.sh")
run_pm(home, [project])
write_file(agents_path(project, "scripts", "bar.sh"), "#!/usr/bin/env bash\necho EDITED\n")
remove_store_script(store, "bar.sh")
r = run_pm(home, [project])
check("3: an edited disk-only file is kept (fails manifest-sha check)",
      os.path.isfile(agents_path(project, "scripts", "bar.sh")),
      "rc %d, stdout %r" % (r.returncode, r.stdout[-400:]))


home, store, project = make("nomanifest")
write_file(agents_path(project, "scripts", "handplaced.sh"), "#!/usr/bin/env bash\necho manual\n")
r = run_pm(home, [project])
check("3b: a disk file with no manifest entry is kept",
      os.path.isfile(agents_path(project, "scripts", "handplaced.sh")),
      "rc %d, stdout %r" % (r.returncode, r.stdout[-400:]))


home, store, project = make("vendored")
write_file(agents_path(project, "claude", "skills", "myskill", "SKILL.md"), "# my skill\n")
write_file(agents_path(project, "claude", "skills", "myskill", "references", "doc.txt"), "ref\n")
r = run_pm(home, [project])
check("4a: a vendored skill's SKILL.md is left alone",
      os.path.isfile(agents_path(project, "claude", "skills", "myskill", "SKILL.md")),
      "rc %d, stdout %r" % (r.returncode, r.stdout[-400:]))
check("4a: a vendored skill's references/ file is left alone",
      os.path.isfile(agents_path(project, "claude", "skills", "myskill", "references", "doc.txt")))


home, store, project = make("shadow")
write_file(os.path.join(store, "global", "scripts", "twin.sh"), "#!/usr/bin/env bash\necho twin\n")
write_file(agents_path(project, "scripts", "twin.sh"), "#!/usr/bin/env bash\necho twin\n")
r = run_pm(home, [project])
check("4b: a file shadowing a global store entity is left alone",
      os.path.isfile(agents_path(project, "scripts", "twin.sh")),
      "rc %d, stdout %r" % (r.returncode, r.stdout[-400:]))


home, store, project = make("partial")
write_file(agents_path(project, "parity", "old.txt"), "old parity note\n")
r = run_pm(home, [project])
check("4c: a file under a partial subtree (parity/) is left alone",
      os.path.isfile(agents_path(project, "parity", "old.txt")),
      "rc %d, stdout %r" % (r.returncode, r.stdout[-400:]))


home, store, project = make("dotfiles")
store_script(store, "keep.sh")
run_pm(home, [project])
write_file(agents_path(project, "scripts", ".DS_Store"), "junk")
r = run_pm(home, ["--check", project])
check("4d: .DS_Store never drives a pending-retirement line",
      ".DS_Store" not in r.stdout and ".DS_Store" not in r.stderr,
      "stdout %r stderr %r" % (r.stdout[-400:], r.stderr[-400:]))


home, store, project = make("check")
store_script(store, "gone.sh")
run_pm(home, [project])
remove_store_script(store, "gone.sh")
r = run_pm(home, ["--check", project])
check("5: --check exits 3 while a retirement is pending", r.returncode == 3,
      "rc %d" % r.returncode)
check("5: --check reports the pending retirement in the new style, not the old disk-only style",
      CHECK_LINE_SUBSTR in r.stdout and "gone.sh" in r.stdout and OLD_EXTRA_SUBSTR + "): .agents/scripts/gone.sh" not in r.stdout,
      "stdout %r" % r.stdout[-600:])
check("5: --check deletes nothing", os.path.isfile(agents_path(project, "scripts", "gone.sh")),
      "gone.sh missing after --check")
r_apply = run_pm(home, [project])
check("5: apply after --check actually retires it", not os.path.isfile(agents_path(project, "scripts", "gone.sh")))
r_after = run_pm(home, ["--check", project])
check("5: --check exits 0 once the retirement has been applied and nothing else drifts",
      r_after.returncode == 0, "rc %d, stdout %r" % (r_after.returncode, r_after.stdout[-400:]))




home, store, project = make("kept")
store_script(store, "stays.sh")
run_pm(home, [project])
sha_after_first = sha256_of(agents_path(project, "scripts", "stays.sh"))
r = run_pm(home, [project])
check("6a: a path still projected by the store survives repeated applies, unchanged",
      os.path.isfile(agents_path(project, "scripts", "stays.sh")) and
      sha256_of(agents_path(project, "scripts", "stays.sh")) == sha_after_first,
      "rc %d, stdout %r" % (r.returncode, r.stdout[-400:]))


home, store, project = make("nomanifestfile")
write_file(agents_path(project, "scripts", "preexisting.sh"), "#!/usr/bin/env bash\necho pre\n")
check("6b: setup has no manifest file yet", not os.path.isfile(agents_path(project, "tmp", ".materialize-manifest")))
r = run_pm(home, [project])
check("6b: with no manifest, nothing under owned subtrees is deleted",
      os.path.isfile(agents_path(project, "scripts", "preexisting.sh")),
      "rc %d, stdout %r" % (r.returncode, r.stdout[-400:]))


home, store, project = make("noop2")
store_script(store, "temp.sh")
run_pm(home, [project])
remove_store_script(store, "temp.sh")
run_pm(home, [project])
batches_after_1 = set()
rescue_root = agents_path(project, "tmp", "materialize-rescue")
if os.path.isdir(rescue_root):
    batches_after_1 = set(os.listdir(rescue_root))
r3 = run_pm(home, [project])
batches_after_2 = set(os.listdir(rescue_root)) if os.path.isdir(rescue_root) else set()
check("second apply after a retirement stays deleted, not resurrected",
      not os.path.isfile(agents_path(project, "scripts", "temp.sh")))
check("second apply after a retirement creates no new rescue batch",
      batches_after_2 == batches_after_1,
      "batches grew: %r -> %r" % (batches_after_1, batches_after_2))

for h in fixtures:
    shutil.rmtree(h, ignore_errors=True)

print("\nproject-materialize-retire: %d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
