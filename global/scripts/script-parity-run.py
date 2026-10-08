#!/usr/bin/env python3
'script-parity-run: does a store script\'s Python port behave like its shell original?\n\nCASES. Every script-port-cases*.py beside this file (or --cases FILE) defines\nCASES = {"<script stem>": [case, ...]}. A case is a dict:\n  name         text shown in the report\n  args         argv after the script path\n  env          extra environment\n  setup        {fixture-relative path: file content}, written before `pre`\n  pre          bash, run in the fixture root before each side\n  stdin        text on stdin (default: none, stdin is closed)\n  cwd          working directory (default {HOME})\n  store        "clone": {STORE} is a remote-less clone of the real store\n  hooks        true: {HOME}/.agent-context/global/hooks holds a copy of the store\'s global/hooks\n  timeout      seconds per side (default 60)\n  normalize    regexes whose matches become <N> in output and effects\n  expect_exit  exit code both sides must return, pinning the branch the case is for\nStrings expand {FIX} (fixture root), {HOME}, {STORE} ({HOME}/.agent-context), {SCRIPTS}\n({HOME}/.agent-context/global/scripts), {BIN} (first on PATH, for stubs of ssh, chezmoi and so on) and\n{RUN}, the command that runs this side\'s copy of the script under test (`bash <x>.sh` or\n`<python> <x>.py`), for a `pre` that runs the script first. A `pre` naming\n`{SCRIPTS}/<stem>.sh` finds nothing on the port side: only the side\'s own copy exists.\n\nTHE TWO SIDES. The shell original is the store\'s working-tree file while it exists, and\nafter the swap the blob just before the commit that removed it (--shell-ref overrides).\nThe port is <scripts-dir>/<stem>.py. Each side gets a fresh fixture at the same path:\n{SCRIPTS} holds a copy of every store script plus that side\'s version of this one, HOME,\nTMPDIR and XDG dirs point inside it, a fixture .gitconfig names a user and signs every\ncommit with a throwaway key kept outside the fixture,\nPATH is {BIN} plus the real PATH without entries under the real home, and nothing else from\nthe caller\'s environment is passed. The original runs under bash, the port under\n--python (default: this interpreter).\n\nCHECKS. Exit code, stdout, stderr, and effects: every file under the fixture the run\ncreated, changed or removed (outside .git, but including .git/hooks and .git/info), and\nfor every git repo in the fixture its status, refs, commit subjects, worktrees and local\nconfig. Output and effects normalize the fixture path, the script\'s own file name\n(<stem>.sh and <stem>.py both read <stem>), timestamps, epoch seconds and commit hashes.\n\nUsage:\n  script-parity-run.py [--script STEM ...] [--cases FILE ...] [--scripts-dir DIR]\n      [--ports-dir DIR] [--store DIR] [--shell-ref REF] [--original-dir REL] [--python PATH]\n      [--shell-only] [--keep] [-v]\n\nPROJECT SCRIPTS. --original-dir projects/<P>/scripts (or hooks) reads the original from that\nstore-relative dir and copies its siblings into {SCRIPTS} beside the global scripts. Stems\nrepeat across projects (wt-finish), so run one project\'s cases file at a time with --cases.\n\nExit: 0 every case matched; 1 a mismatch, a missing port or a failed case setup;\n2 a setup fault (no cases, unknown script, no shell original).'

import argparse
import difflib
import atexit
import glob
import hashlib
import importlib.util
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_HOME = os.path.realpath(os.path.expanduser("~"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
DEFAULT_STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(REAL_HOME, ".agent-context")
ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
EPOCH = re.compile(r"\b1[6-9]\d{8}(?:\d{3})?\b")
SHA = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_SIGNING_KEY = []


def gitconfig():
    'The fixture .gitconfig: a user, and commits signed with one throwaway key file per\n    run. The key lives outside every fixture root, so effect snapshots never see it and\n    both sides sign the same way. No fixture makes an unsigned commit.'
    if not _SIGNING_KEY:
        d = os.path.realpath(tempfile.mkdtemp(prefix="script-parity-key-"))
        atexit.register(shutil.rmtree, d, True)
        key = os.path.join(d, "key")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "parity", "-f", key],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        _SIGNING_KEY.append(key)
    return ("[user]\n\tname = parity\n\temail = parity@example.invalid\n\tsigningkey = %s\n"
            "[gpg]\n\tformat = ssh\n[gpg \"ssh\"]\n\tprogram = %s\n"
            "[commit]\n\tgpgsign = true\n"
            "[init]\n\tdefaultBranch = main\n[safe]\n\tdirectory = *\n"
            % (_SIGNING_KEY[0], shutil.which("ssh-keygen") or "ssh-keygen"))
DIFF_LINES = 40
LOCALE = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"


class SetupFault(Exception):
    pass




def load_cases(paths):
    cases = {}
    for path in paths:
        name = "script_port_cases_" + hashlib.sha1(path.encode()).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise SetupFault("cannot load cases from %s" % path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise SetupFault("cases file %s raised %s: %s" % (path, type(exc).__name__, exc))
        table = getattr(module, "CASES", None)
        if not isinstance(table, dict):
            raise SetupFault("%s defines no CASES dict" % path)
        for stem, items in table.items():
            cases.setdefault(stem, []).extend(items)
    return cases


def git(repo, *args):
    proc = subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SetupFault("git %s in %s failed: %s" % (" ".join(args), repo,
                                                       (proc.stderr or "").strip()[:200]))
    return proc.stdout


def shell_original(store, stem, ref=None, original_dir="global/scripts"):
    rel = "%s/%s.sh" % (original_dir.strip("/"), stem)
    if ref is None:
        live = os.path.join(store, rel)
        if os.path.isfile(live):
            with open(live, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        last = git(store, "rev-list", "-1", "HEAD", "--", rel).strip()
        if not last:
            raise SetupFault("%s: no shell original in the store or its history" % stem)
        
        
        
        try:
            return git(store, "show", "%s:%s" % (last, rel))
        except SetupFault:
            ref = last + "^"
    return git(store, "show", "%s:%s" % (ref, rel))




class Fixture:
    def __init__(self, root):
        self.root = root
        self.home = os.path.join(root, "home")
        self.bin = os.path.join(root, "bin")
        self.tmp = os.path.join(root, "tmp")
        self.scripts = hp.scripts_dir(self.home)
        self.store = os.path.join(self.home, ".agent-context")
        
        
        self.run = ""

    def expand(self, obj):
        if isinstance(obj, str):
            return (obj.replace("{FIX}", self.root).replace("{HOME}", self.home)
                       .replace("{STORE}", self.store).replace("{SCRIPTS}", self.scripts)
                       .replace("{BIN}", self.bin).replace("{RUN}", self.run))
        if isinstance(obj, dict):
            return {self.expand(k): self.expand(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.expand(v) for v in obj]
        return obj


def _copy_dir(src, dst, skip=()):
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        path = os.path.join(src, name)
        if name in skip or name == "__pycache__" or not os.path.isfile(path):
            continue
        shutil.copy2(path, os.path.join(dst, name))


def make_pristine(store, base):
    dest = os.path.join(base, "pristine-store")
    if not os.path.isdir(dest):
        
        
        proc = subprocess.run(["git", "clone", "-q", "--shared", store, dest],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise SetupFault("cannot clone the store: %s" % proc.stderr.strip()[:200])
    return dest


def side_env(fx, case):
    real_prefix = REAL_HOME + os.sep
    keep = [p for p in os.environ.get("PATH", "").split(os.pathsep)
            if p and not os.path.realpath(p).startswith(real_prefix)]
    env = {
        "PATH": os.pathsep.join([fx.bin] + keep),
        "HOME": fx.home, "TMPDIR": fx.tmp,
        "USER": os.environ.get("USER", "parity"), "LOGNAME": os.environ.get("LOGNAME", "parity"),
        "LANG": LOCALE, "LC_ALL": LOCALE, "SHELL": "/bin/sh", "TERM": "dumb",
        "XDG_STATE_HOME": os.path.join(fx.home, ".local", "state"),
        "XDG_CONFIG_HOME": os.path.join(fx.home, ".config"),
        "XDG_CACHE_HOME": os.path.join(fx.home, ".cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(fx.expand(case.get("env") or {}))
    return env


def prepare(fx, opts, stem, ext, body, case, base):
    if os.path.lexists(fx.root):
        shutil.rmtree(fx.root)
    for d in (fx.home, fx.bin, fx.tmp):
        os.makedirs(d)
    with open(os.path.join(fx.home, ".gitconfig"), "w", encoding="utf-8") as fh:
        fh.write(gitconfig())
    _copy_dir(opts.scripts_dir, fx.scripts, skip=(stem + ".sh", stem + ".py"))
    if opts.original_dir != "global/scripts":
        
        
        _copy_dir(os.path.join(opts.store, opts.original_dir), fx.scripts,
                  skip=(stem + ".sh", stem + ".py"))
    target = os.path.join(fx.scripts, "%s.%s" % (stem, ext))
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(target, 0o755)
    fx.run = "%s %s" % (shlex.quote("bash" if ext == "sh" else opts.python), shlex.quote(target))
    if case.get("hooks"):
        _copy_dir(os.path.join(os.path.dirname(opts.scripts_dir), "hooks"),
                  hp.hooks_dir(fx.home))
    env = side_env(fx, case)
    if case.get("store") == "clone":
        pristine = make_pristine(opts.store, base)
        proc = subprocess.run(["git", "clone", "-q", "--shared", pristine, fx.store],
                              capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise SetupFault("store clone failed: %s" % proc.stderr.strip()[:200])
        git(fx.store, "remote", "remove", "origin")
    for rel, content in (case.get("setup") or {}).items():
        dest = os.path.join(fx.root, fx.expand(rel))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(fx.expand(content))
    if case.get("pre"):
        try:
            pre = subprocess.run(["bash", "-c", fx.expand(case["pre"])], cwd=fx.root, env=env,
                                 capture_output=True, encoding="utf-8", errors="replace",
                                 timeout=case.get("timeout", 60))
        except subprocess.TimeoutExpired:
            raise SetupFault("pre timed out on the %s side" % ext)
        if pre.returncode != 0:
            raise SetupFault("pre failed on the %s side: %s"
                             % (ext, (pre.stderr or pre.stdout or "").strip()[:200]))
    return env, target




def _is_bare_repo(dirnames, filenames):
    return ("HEAD" in filenames and "config" in filenames
            and "objects" in dirnames and "refs" in dirnames)


def _walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if _is_bare_repo(dirnames, filenames):
            
            
            
            
            for dp, _dn, fn in os.walk(os.path.join(dirpath, "hooks")):
                for name in fn:
                    yield os.path.join(dp, name)
            dirnames[:] = []
            continue
        if ".git" in dirnames:
            dirnames.remove(".git")
            git_dir = os.path.join(dirpath, ".git")
            for sub in ("hooks", "info"):
                for dp, _dn, fn in os.walk(os.path.join(git_dir, sub)):
                    for name in fn:
                        yield os.path.join(dp, name)
        for name in filenames:
            yield os.path.join(dirpath, name)


def snapshot(root):
    out = {}
    for path in _walk(root):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        out[os.path.relpath(path, root)] = (st.st_size, st.st_mtime_ns)
    return out


def _repos(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if _is_bare_repo(dirnames, filenames):
            found.append(dirpath)
            dirnames[:] = []
            continue
        if ".git" in dirnames or ".git" in filenames:
            found.append(dirpath)
        if ".git" in dirnames:
            dirnames.remove(".git")
    return found


def normalize(text, fx, stem, extra):
    for root in {os.path.realpath(fx.root), fx.root}:
        text = text.replace(root, "<FIX>")
    
    
    text = re.sub(r"\b(?:bash|sh|python3)\s+(\S*" + re.escape(stem) + r"\.(?:sh|py)\b)",
                  r"\1", text)
    text = re.sub(re.escape(stem) + r"\.(?:sh|py)\b", stem, text)
    for pattern in extra:
        text = re.sub(pattern, "<N>", text)
    text = ISO.sub("<TS>", text)
    
    
    text = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "<TIME>", text)
    
    
    text = re.sub(r"\(\+\d+s\)", "(+<N>s)", text)
    
    
    text = re.sub(r"\b\d{8}-\d{6}\b", "<TS>", text)
    text = EPOCH.sub("<EPOCH>", text)
    return SHA.sub("<SHA>", text)


def effects(fx, before, env, stem, extra):
    out = {}
    seen = set()
    for path in _walk(fx.root):
        rel = os.path.relpath(path, fx.root)
        seen.add(rel)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if before.get(rel) == (st.st_size, st.st_mtime_ns):
            continue
        try:
            if os.path.islink(path):
                data = "link:" + os.readlink(path)
            else:
                with open(path, "rb") as fh:
                    data = fh.read().decode("utf-8", "replace")
        except OSError as exc:
            data = "unreadable:%s" % exc.errno
        key = "file " + normalize(rel, fx, stem, extra)
        out[key] = "mode %o sha1 %s" % (st.st_mode & 0o777, hashlib.sha1(
            normalize(data, fx, stem, extra).encode()).hexdigest())
    for rel in before:
        if rel not in seen:
            out["file " + normalize(rel, fx, stem, extra)] = "gone"
    for repo in _repos(fx.root):
        label = normalize(os.path.relpath(repo, fx.root), fx, stem, extra)
        for what, args in (("status", ["status", "--porcelain=v1", "-uall"]),
                           ("refs", ["for-each-ref", "--format=%(refname)"]),
                           ("subjects", ["log", "--all", "--format=%s"]),
                           ("worktrees", ["worktree", "list", "--porcelain"]),
                           ("config", ["config", "--local", "--list"])):
            proc = subprocess.run(["git", "-C", repo] + args, capture_output=True, env=env,
                                  encoding="utf-8", errors="replace")
            text = proc.stdout + proc.stderr
            if what in ("subjects", "refs"):
                
                
                
                text = "\n".join(sorted(text.splitlines()))
            out["git %s %s" % (label, what)] = normalize(text, fx, stem, extra)
    return out




def _killpg(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def run_side(opts, fx, stem, ext, body, case, base):
    env, target = prepare(fx, opts, stem, ext, body, case, base)
    before = snapshot(fx.root)
    argv = (["bash"] if ext == "sh" else [opts.python]) + [target] + fx.expand(case.get("args") or [])
    cwd = fx.expand(case.get("cwd") or "{HOME}")
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd, env=env, encoding="utf-8",
                                errors="replace", start_new_session=True)
    except OSError as exc:
        raise SetupFault("cannot start the %s side: %s" % (ext, exc))
    try:
        stdout, stderr = proc.communicate(input=fx.expand(case.get("stdin")),
                                          timeout=case.get("timeout", 60))
        code = proc.returncode
    except subprocess.TimeoutExpired:
        _killpg(proc)
        stdout, stderr = proc.communicate()
        code = "timeout"
    _killpg(proc)
    extra = case.get("normalize") or []
    return {"exit": code,
            "stdout": normalize(stdout or "", fx, stem, extra),
            "stderr": normalize(stderr or "", fx, stem, extra),
            "effects": effects(fx, before, env, stem, extra)}


def _diff(a, b):
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "shell", "port",
                                      lineterm="", n=1))
    if len(lines) > DIFF_LINES:
        lines = lines[:DIFF_LINES] + ["... %d more diff lines" % (len(lines) - DIFF_LINES)]
    return "\n      ".join(lines)


def compare(shell, port):
    problems = []
    if shell["exit"] != port["exit"]:
        problems.append("exit: shell %s, port %s" % (shell["exit"], port["exit"]))
    for stream in ("stdout", "stderr"):
        if shell[stream] != port[stream]:
            problems.append("%s differs:\n      %s" % (stream, _diff(shell[stream], port[stream])))
    for key in sorted(set(shell["effects"]) | set(port["effects"])):
        a = shell["effects"].get(key)
        b = port["effects"].get(key)
        if a == b:
            continue
        if a is None or b is None or key.startswith("file "):
            problems.append("effect %s: shell %s, port %s" % (key, a, b))
        else:
            problems.append("effect %s differs:\n      %s" % (key, _diff(a, b)))
    return problems


def _preview(result):
    lines = ["exit %s" % result["exit"]]
    for stream in ("stdout", "stderr"):
        text = result[stream].strip().splitlines()
        if text:
            lines.append("%s: %s" % (stream, " | ".join(text[:5])))
    changed = [k for k in result["effects"] if k.startswith("file ")]
    if changed:
        lines.append("files: %s" % ", ".join(sorted(changed)[:10]))
    return "; ".join(lines)


def run(opts):
    paths = opts.cases or sorted(glob.glob(os.path.join(opts.scripts_dir, "script-port-cases*.py")))
    if not paths:
        raise SetupFault("no script-port-cases*.py in %s" % opts.scripts_dir)
    cases = load_cases(paths)
    stems = opts.script or sorted(cases)
    unknown = [s for s in stems if s not in cases]
    if unknown:
        raise SetupFault("no cases for: %s" % ", ".join(unknown))
    if not any(cases[s] for s in stems):
        raise SetupFault("the selected scripts have no cases")
    base = os.path.realpath(tempfile.mkdtemp(prefix="script-parity-"))
    total = failures = 0
    try:
        for stem in stems:
            shell_body = shell_original(opts.store, stem, opts.shell_ref, opts.original_dir)
            port_path = os.path.join(opts.ports_dir, stem + ".py")
            port_body = None
            if os.path.isfile(port_path):
                with open(port_path, encoding="utf-8", errors="replace") as fh:
                    port_body = fh.read()
            items = cases[stem]
            for i, case in enumerate(items, 1):
                total += 1
                label = "%s [%d/%d] %s" % (stem, i, len(items), case.get("name", "?"))
                fx = Fixture(os.path.join(base, "fx"))
                if port_body is None and not opts.shell_only:
                    failures += 1
                    print("script-parity-run: %s FAIL: port missing at %s" % (label, port_path))
                    continue
                try:
                    shell = run_side(opts, fx, stem, "sh", shell_body, case, base)
                    port = None if opts.shell_only else run_side(opts, fx, stem, "py", port_body,
                                                                 case, base)
                except SetupFault as exc:
                    failures += 1
                    print("script-parity-run: %s FAIL: %s" % (label, exc))
                    continue
                problems = [] if port is None else compare(shell, port)
                expect = case.get("expect_exit")
                if expect is not None:
                    for name, result in (("shell", shell), ("port", port)):
                        if result is not None and result["exit"] != expect:
                            problems.append("%s exit %s, case expects %s"
                                            % (name, result["exit"], expect))
                if problems:
                    failures += 1
                    print("script-parity-run: %s FAIL" % label)
                    for problem in problems:
                        print("    " + problem)
                    if opts.verbose:
                        print("    shell: " + _preview(shell))
                elif opts.shell_only:
                    print("script-parity-run: %s shell-only ok: %s" % (label, _preview(shell)))
                else:
                    print("script-parity-run: %s ok" % label)
                    if opts.verbose:
                        print("    " + _preview(shell))
    finally:
        if opts.keep:
            print("script-parity-run: fixture kept at %s" % base)
        else:
            shutil.rmtree(base, ignore_errors=True)
    print("\n  %d case(s), %d failure(s)" % (total, failures))
    return 1 if failures else 0


def parse_args(argv):
    p = argparse.ArgumentParser(prog="script-parity-run.py",
                                description="Compare store shell scripts with their Python ports.")
    p.add_argument("--script", action="append", help="script stem to run (repeatable)")
    p.add_argument("--cases", action="append", help="cases file (repeatable)")
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--scripts-dir", default=None,
                   help="where ports and cases live (default <store>/global/scripts)")
    p.add_argument("--ports-dir", default=None,
                   help="where <stem>.py ports are read from (default --scripts-dir), so a port "
                        "in scratch runs beside the store's real sibling scripts")
    p.add_argument("--shell-ref", default=None, help="git ref to read shell originals from")
    p.add_argument("--original-dir", default="global/scripts",
                   help="store-relative directory holding the shell original, e.g. "
                        "projects/example-app/scripts; its siblings join {SCRIPTS}")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--shell-only", action="store_true",
                   help="run only the shell original, to check that cases reach their branch")
    p.add_argument("--keep", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    opts = p.parse_args(argv)
    opts.store = os.path.realpath(os.path.expanduser(opts.store))
    opts.scripts_dir = os.path.realpath(opts.scripts_dir or os.path.join(opts.store, "global", "scripts"))
    opts.ports_dir = os.path.realpath(opts.ports_dir) if opts.ports_dir else opts.scripts_dir
    opts.cases = [os.path.realpath(c) for c in opts.cases] if opts.cases else None
    return opts


def main(argv):
    opts = parse_args(argv)
    try:
        return run(opts)
    except SetupFault as exc:
        print("script-parity-run: setup fault: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
