'wt_finish_core: the shared landing logic behind every project\'s wt-finish.\n\nA project keeps its own projects/<P>/scripts/wt-finish-core.py. That file declares a\nProject, its Flags and optional hooks, then calls run_project(). This module owns\nwhat the projects share: resolve the worktree, branch and main checkout; --push-only\nresume; the guards (managed worktree, not on the target, worktree clean, main checkout\non the target); dirty main checkout vs the branch\'s paths (a byte-identical path is\nstaged, and in merge mode committed first); commits ahead; remote freshness; gate\nrelevance (gate-relevance.py with the project\'s globs); with Project.rebase_first, the\nrebase before the gates; the gate runner, sequential plus parallel background gates; the\nlanding lock; rebase or merge; under the lock, the gates again on the rebased branch when\nthe target moved; land; push; cleanup; report.\n\nHooks a project module may define, each called with the Ctx:\n  gates(ctx) -> [Gate]       the landing gates, in order\n  prepare_main(ctx)          before the main checkout\'s dirt is read\n  after_combine(ctx, d)      after the rebase (d = worktree) or the merge (d = main)\n  before_land(ctx)           after the rebase, before the target moves\n  after_publish(ctx)         after the push step\n  cleanup(ctx)               replaces wt-sweep when Project.cleanup == "hook"\n\nA project file loads this module by path and checks API. Bump API on any change a\nproject file must follow.\n\nObservations guarded: #120, #171, #172, #326, #394.'

import errno
import io
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp  

API = 1
PROG = "wt-finish"
WARN = "⚠"
CHECK = "✅"
SSH_OPTS = "-o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o ConnectTimeout=8"
STRATEGIES = ("origin", "origin_verify_canonical", "primary_bounded_mirrors", "each_pushurl")




class Push:
    'How the target is published.\n\n    strategy        origin | origin_verify_canonical | primary_bounded_mirrors | each_pushurl\n    mirror_timeout  seconds each mirror gets under primary_bounded_mirrors\n    opt_in_flag     a project flag (e.g. "--push") without which a landing does not push\n    push_message    replaces "pushing <target> to origin..."\n    resume_extra    extra args named in the --push-only resume hint'

    def __init__(self, strategy="origin", mirror_timeout=60, opt_in_flag=None,
                 push_message=None, resume_extra=()):
        if strategy not in STRATEGIES:
            raise ValueError("unknown push strategy %r" % strategy)
        self.strategy = strategy
        self.mirror_timeout = mirror_timeout
        self.opt_in_flag = opt_in_flag
        self.push_message = push_message
        self.resume_extra = tuple(resume_extra)


class Project:
    'rebase_first: rebase onto the target before the gates, so they judge the tree that\n    lands, and under the lock rebase and re-gate only when the target moved again while\n    they ran. Without it the gates build the branch as committed and, whenever the target\n    moved, build it a second time under the\n    lock on the rebased branch: two full builds, one of them holding the lock. The cost is\n    that a gate failing on the newer target leaves the worktree rebased (the pre-rebase\n    HEAD is printed). Takes effect only with landing_lock in rebase mode, where the\n    re-gate keeps "what lands is what passed"; elsewhere the gates run as before.'

    def __init__(self, name, default_mode="rebase", irrelevant_globs=(), push=None,
                 main_status_untracked=True, landing_lock=False, cleanup="sweep",
                 next_step='Next: ExitWorktree(action: "remove") to delete the worktree + branch.',
                 doc="", gate_deadline=600, rebase_first=False):
        if default_mode not in ("rebase", "merge"):
            raise ValueError("default_mode must be rebase or merge")
        if cleanup not in ("sweep", "hook"):
            raise ValueError("cleanup must be sweep or hook")
        self.name = name
        self.default_mode = default_mode
        self.irrelevant_globs = tuple(irrelevant_globs)
        self.push = push or Push()
        self.main_status_untracked = main_status_untracked
        self.landing_lock = landing_lock
        self.cleanup = cleanup
        self.next_step = next_step
        self.doc = doc
        self.gate_deadline = gate_deadline
        self.rebase_first = rebase_first


def opt_key(name):
    return name.lstrip("-").replace("-", "_")


class Flag:
    "A flag. Its opts key is the name without leading dashes, dashes as underscores.\n\n    off       the opts key this flag sets False (default True); without it the flag's\n              own key is set True (default False)\n    alias_of  another flag name (core or project) this one applies\n    note      printed to stderr when the flag is given"

    def __init__(self, name, off=None, alias_of=None, note=None, help=""):
        self.name = name
        self.off = off
        self.alias_of = alias_of
        self.note = note
        self.help = help

    @property
    def key(self):
        return opt_key(self.name)


class Cmd:
    'A command a gate hands back for the core to run: argv, the die message on a\n    nonzero exit, cwd (default the worktree) and env (default inherited).'

    def __init__(self, argv, fail, cwd=None, env=None):
        self.argv = list(argv)
        self.fail = fail
        self.cwd = cwd
        self.env = env


class Gate:
    "One landing gate. run(ctx) does the work and calls ctx.die on failure, or returns\n    a Cmd for the core to run. A parallel gate returns a Cmd, or None when it skips\n    itself: the Cmd starts in the background with its output captured and is awaited,\n    output replayed, after every sequential gate, so a sequential failure is reported\n    first and kills it.\n\n    after_rebase: judge only the tree that lands. When the target moved past the\n    branch, the early run skips this gate and _regate runs it on the rebased branch\n    under the landing lock. A gate that compares the branch with a record of the\n    target (a list describing main) otherwise fails a stale branch for files it never\n    touched. Takes effect only with landing_lock in rebase mode; elsewhere the gate\n    runs early as before.\n\n    deadline: seconds this gate's Cmd may run; None takes Project.gate_deadline.\n\n    started: started(proc) -> bool, for a Cmd that first waits in a queue it does not\n    control. The deadline then counts from the first check (one a second) that finds it\n    true, so waiting in line never kills a gate; the work itself still has the deadline.\n    Otherwise a landing can be killed while its build waits for a machine's one\n    xcodebuild slot, before compiling anything."

    def __init__(self, name, run, label, enabled=None, parallel=False,
                 relevance_skippable=True, no_gate_skippable=True, after_rebase=False,
                 deadline=None, started=None, selected_by_default=True, required=False,
                 requires=(), allow_noop=False):
        self.name = name
        self.run = run
        self.label = label
        self.enabled = enabled or (lambda ctx: True)
        self.parallel = parallel
        self.relevance_skippable = relevance_skippable
        self.no_gate_skippable = no_gate_skippable
        self.after_rebase = after_rebase
        self.deadline = deadline
        self.started = started
        self.selected_by_default = selected_by_default
        self.required = required
        self.requires = tuple(requires)
        self.allow_noop = allow_noop


CORE_FLAGS = (
    Flag("--no-gate", off="gate",
         help="skip every gate the project lets --no-gate skip. You normally need no flag:\n"
              "the gates skip themselves when the branch changes no build input\n"
              "(gate-relevance.py), naming the paths."),
    Flag("--merge", help="land with `git merge --no-ff`."),
    Flag("--rebase", help="land linearly: rebase onto the target, then fast-forward."),
    Flag("--push-only", help="resume publishing an already-landed target after the push step\n"
                             "failed. Lands nothing, runs no gates. Requires the main\n"
                             "checkout be on the target, strictly ahead of origin."),
)




class Died(Exception):
    'A die() (msg set) or a bare git failure (msg None: git already wrote to stderr).'

    def __init__(self, msg, code=1):
        super().__init__(msg or "")
        self.msg = msg
        self.code = code


class Terminated(Exception):
    pass


def say(msg=""):
    print("%s: %s" % (PROG, msg) if msg else "")


def warn(msg):
    print("%s %s: %s" % (WARN, PROG, msg))


def die(msg):
    raise Died(msg)


def _flush():
    sys.stdout.flush()
    sys.stderr.flush()


def run(args, cwd=None, env=None, quiet_out=False, quiet_err=False):
    'A command whose exit status the caller reads; stdio inherited unless quieted.'
    _flush()
    return subprocess.run(args, cwd=cwd, env=env,
                          stdout=subprocess.DEVNULL if quiet_out else None,
                          stderr=subprocess.DEVNULL if quiet_err else None)


def capture(args, cwd=None, quiet_err=False):
    '(rc, stdout less trailing newlines). Never raises.'
    _flush()
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL if quiet_err else None, text=True)
    return proc.returncode, proc.stdout.rstrip("\n")


def assign(args, cwd=None):
    'stdout of a command that must succeed; raises Died with its own exit code.'
    rc, out = capture(args, cwd=cwd)
    if rc != 0:
        raise Died(None, rc)
    return out


def extend_ssh():
    "Give git's ssh a keepalive and a connect timeout, once however often called."
    current = os.environ.get("GIT_SSH_COMMAND") or "ssh"
    if "ServerAliveInterval=5" in current:
        return
    os.environ["GIT_SSH_COMMAND"] = "%s %s" % (current, SSH_OPTS)


def _home():
    'The home directory, or "" when none resolves (hp.home() raises then).'
    try:
        return hp.home()
    except RuntimeError:
        return ""


def _store_script(name):
    'A store script\'s path, or "" when no home resolves.'
    try:
        return os.path.join(hp.scripts_dir(), name)
    except RuntimeError:
        return ""


def _use_agent_socket():
    home = _home()
    if not home:
        return
    sock = os.path.join(home, ".ssh", "op-agent.sock")
    try:
        if stat.S_ISSOCK(os.stat(sock).st_mode):
            os.environ["SSH_AUTH_SOCK"] = sock
    except OSError:
        pass


def _nul_list(text):
    return [p for p in text.split("\0") if p]


def _porcelain_paths(raw):
    "Paths from `git status --porcelain -z`, unquoted; a rename's source is skipped."
    out = []
    records = raw.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4:
            continue
        out.append(rec[3:])
        if rec[0] in "RC":
            i += 1
    return out


def symlink_cycles(root):
    'Symlinks under root that loop: one that resolves to itself (ELOOP) or a directory\n    link to a folder that holds it. Walks without following links; a dangling link or a\n    link to elsewhere is not a cycle.'
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = os.path.realpath(dirpath)
        for name in sorted(dirnames + filenames):
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                continue
            try:
                os.stat(path)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    found.append(path)
                continue
            target = os.path.realpath(path)
            if os.path.isdir(path) and (here == target or here.startswith(target.rstrip(os.sep) + os.sep)):
                found.append(path)
    return found


def copy_tree(src, dst):
    'shutil.copytree that never recurses through a symlink. A link that points back\n    at a folder above it makes a link-following copytree recurse until shutil.Error,\n    failing every landing until a human removes it. Links are copied as links, and a looping link is refused first,\n    named, since copying it only moves the loop into the worktree.'
    loops = symlink_cycles(src)
    if loops:
        die("copying %s: %s loop%s back on %s (policy). Remove %s, then land again."
            % (src, ", ".join(loops), "s" if len(loops) == 1 else "",
               "itself or a folder above it" if len(loops) == 1 else "themselves or a folder above them",
               "it" if len(loops) == 1 else "them"))
    shutil.copytree(src, dst, symlinks=True)


class Ctx:
    say = staticmethod(say)
    warn = staticmethod(warn)
    die = staticmethod(die)
    run = staticmethod(run)
    capture = staticmethod(capture)
    assign = staticmethod(assign)
    extend_ssh = staticmethod(extend_ssh)

    def __init__(self, project, module, opts, store):
        self.project = project
        self.module = module
        self.opts = opts
        self.mode = project.default_mode
        self.worktree = ""
        self.branch = ""
        self.main = ""
        self.target = "main"
        self.changed_paths = []
        self.ahead = 0
        self.behind = 0
        self.main_before = ""
        self.gated_base = ""
        self.deferred = []
        self.failed_gate = ""
        self.staged = []
        self.rebased = False
        self.pre_rebase = ""
        self.pushed = False
        self.home = _home()
        self.store = store

    def hook(self, name):
        if isinstance(self.module, dict):
            fn = self.module.get(name)
        else:
            fn = getattr(self.module, name, None)
        return fn if callable(fn) else None

    @staticmethod
    def git(repo, *args):
        return ["git", "-C", repo] + list(args)




def _help_text(project, flags):
    lines = [project.doc.strip("\n"), "", "Flags:"]
    for f in CORE_FLAGS + tuple(flags):
        head = "  %-12s " % f.name
        body = f.help or ("alias of %s." % f.alias_of if f.alias_of else "")
        parts = body.split("\n")
        lines.append((head + parts[0]).rstrip())
        lines += [" " * len(head) + p for p in parts[1:]]
    lines.append("  -h, --help   print this and exit.")
    lines.append("  --check=NAME run one named gate; repeat for each required check, or use --check=all.")
    return "\n".join(lines)


def _parse(argv, project, flags):
    '(opts, None) to go on, or (None, exit code).'
    if any(a in ("-h", "--help") for a in argv):
        print(_help_text(project, flags))
        return None, 0
    by_name = {f.name: f for f in CORE_FLAGS + tuple(flags)}
    opts = {"gate": True, "merge": False, "rebase": False, "push_only": False}
    for f in flags:
        if f.alias_of is None:
            if f.off:
                opts.setdefault(f.off, True)
            else:
                opts.setdefault(f.key, False)
    selected = []
    for arg in argv:
        if arg.startswith("--check="):
            name = arg.partition("=")[2]
            if not name:
                print("%s: --check needs a gate name" % PROG, file=sys.stderr)
                return None, 2
            selected.append(name)
            continue
        f = by_name.get(arg)
        if f is None:
            print("%s: unknown flag '%s'" % (PROG, arg), file=sys.stderr)
            return None, 2
        if f.note:
            print(f.note, file=sys.stderr)
        seen = set()
        while f.alias_of and f.name not in seen and f.alias_of in by_name:
            seen.add(f.name)
            f = by_name[f.alias_of]
        if f.off:
            opts[f.off] = False
        else:
            opts[f.key] = True
    opts["checks"] = tuple(selected)
    return opts, None


def _resolve(ctx):
    if run(["git", "rev-parse", "--is-inside-work-tree"], quiet_out=True, quiet_err=True).returncode != 0:
        die("not inside a git repository")
    ctx.worktree = assign(["git", "rev-parse", "--show-toplevel"])
    ctx.branch = assign(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    
    for line in assign(["git", "worktree", "list", "--porcelain"]).split("\n"):
        if line.startswith("worktree "):
            ctx.main = line[len("worktree "):]
            break
    if ctx.opts.get("merge"):
        ctx.mode = "merge"
    if ctx.opts.get("rebase"):
        ctx.mode = "rebase"


def _resume_hint(ctx):
    extra = "".join(" " + a for a in ctx.project.push.resume_extra)
    return 'resume with: bash "%s/.agents/scripts/wt-finish.sh" --push-only%s' % (ctx.main, extra)


def _push_origin(ctx):
    if run(Ctx.git(ctx.main, "push", "origin", ctx.target)).returncode != 0:
        die("push failed (the landing is committed locally), %s" % _resume_hint(ctx))


def _push_origin_verify_canonical(ctx):
    
    
    t = ctx.target
    if run(Ctx.git(ctx.main, "push", "origin", t)).returncode == 0:
        return
    local_head = assign(Ctx.git(ctx.main, "rev-parse", t))
    url = assign(Ctx.git(ctx.main, "remote", "get-url", "origin"))
    rc, out = capture(Ctx.git(ctx.main, "ls-remote", url, "refs/heads/%s" % t), quiet_err=True)
    if rc != 0:
        die("push failed and could not reach %s to check whether GitHub already has %s "
            "(offline?); %s" % (url, t, _resume_hint(ctx)))
    words = out.split()
    remote_head = words[0] if words else ""
    if remote_head and remote_head == local_head:
        warn("a mirror push failed/timed out, but GitHub is up to date (%s@%s), continuing; "
             "mirrors self-heal on the next push." % (t, local_head[:8]))
    else:
        die("push failed and GitHub is not up to date (the landing is committed locally on %s, "
            "retry the push); %s" % (t, _resume_hint(ctx)))


def _push_urls(ctx):
    rc, out = capture(Ctx.git(ctx.main, "remote", "get-url", "--push", "--all", "origin"))
    return [u for u in out.split("\n") if u] if rc == 0 else []


def _push_bounded(ctx, url, limit):
    _flush()
    proc = subprocess.Popen(Ctx.git(ctx.main, "push", url, ctx.target),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait()
        return 124


def _push_primary_bounded_mirrors(ctx):
    urls = _push_urls(ctx)
    if not urls:
        die("origin has no push url")
    primary, mirrors = urls[0], urls[1:]
    if run(Ctx.git(ctx.main, "push", primary, ctx.target)).returncode != 0:
        die("push to %s failed (the landing is committed locally), %s" % (primary, _resume_hint(ctx)))
    limit = ctx.project.push.mirror_timeout
    lagging = []
    for url in mirrors:
        say("mirroring to %s..." % url)
        if _push_bounded(ctx, url, limit) != 0:
            lagging.append(url)
    if lagging:
        warn("these mirrors did not accept the push within %ds and are now BEHIND:" % limit)
        for url in lagging:
            print("  %s" % url)
        print("  The landing itself succeeded, %s has the commit and CI is running." % primary)
        print("  They catch up on the next push; a mirror that stays behind is a NAS to look at,")
        print("  not a landing to retry. Never respond by dropping a mirror leg.")


def _push_each_pushurl(ctx):
    
    first = assign(Ctx.git(ctx.main, "remote", "get-url", "origin"))
    
    
    
    took, missed = [], []
    _flush()
    legs = [(url, subprocess.Popen(Ctx.git(ctx.main, "push", url, ctx.target),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
            for url in _push_urls(ctx)]
    for url, proc in legs:
        words, _ = proc.communicate()
        sys.stdout.write(words or "")
        _flush()
        if proc.returncode == 0:
            took.append(url)
        else:
            missed.append(url)
            warn("%s did not take the push" % url)
    if not took:
        die("no remote took the push (the landing is committed locally on %s, retry when a "
            "mirror is reachable), %s" % (ctx.target, _resume_hint(ctx)))
    if missed:
        warn("%s is on %s but not on %s" % (ctx.target, " ".join(took), " ".join(missed)))
        if first in missed:
            warn("that includes the CANONICAL remote, anything fetching from it will not see "
                 "this landing yet.")


_PUSHERS = {
    "origin": _push_origin,
    "origin_verify_canonical": _push_origin_verify_canonical,
    "primary_bounded_mirrors": _push_primary_bounded_mirrors,
    "each_pushurl": _push_each_pushurl,
}


def push_target(ctx):
    _use_agent_socket()
    push = ctx.project.push
    if push.push_message:
        say(push.push_message)
    elif push.strategy == "primary_bounded_mirrors":
        urls = _push_urls(ctx)
        say("pushing %s to %s..." % (ctx.target, urls[0] if urls else "origin"))
    else:
        say("pushing %s to origin..." % ctx.target)
    extend_ssh()
    _PUSHERS[push.strategy](ctx)
    ctx.pushed = True


def _push_only(ctx):
    
    
    
    t, m = ctx.target, ctx.main
    opt_in = ctx.project.push.opt_in_flag
    if opt_in and not ctx.opts.get(opt_key(opt_in)):
        die("--push-only publishes %s; pass %s too to confirm" % (t, opt_in))
    main_branch = assign(Ctx.git(m, "symbolic-ref", "--short", "HEAD"))
    if main_branch != t:
        die("main checkout is on '%s', not '%s', switch it first" % (main_branch, t))
    extend_ssh()
    if run(Ctx.git(m, "fetch", "--quiet", "origin", t), quiet_err=True).returncode != 0:
        warn("could not fetch origin/%s (offline?), comparing against the last-known origin/%s" % (t, t))
    if run(Ctx.git(m, "rev-parse", "-q", "--verify", "origin/%s" % t), quiet_out=True).returncode != 0:
        die("no origin/%s ref cached, fetch never succeeded; connect and retry" % t)
    ahead = int(assign(Ctx.git(m, "rev-list", "--count", "origin/%s..%s" % (t, t))))
    behind = int(assign(Ctx.git(m, "rev-list", "--count", "%s..origin/%s" % (t, t))))
    if behind > 0 and ahead > 0:
        die("local %s and origin/%s have DIVERGED, reconcile before pushing" % (t, t))
    if behind > 0:
        die("local %s is behind origin/%s (%d commit(s)), fetch and reconcile before pushing"
            % (t, t, behind))
    if ahead == 0:
        die("local %s is even with origin/%s, nothing to push" % (t, t))
    rc1, short_origin = capture(Ctx.git(m, "rev-parse", "--short", "origin/%s" % t))
    rc2, short_target = capture(Ctx.git(m, "rev-parse", "--short", t))
    say("pushing %d commit(s) (%s..%s) to origin" % (
        ahead, short_origin if rc1 == 0 else "", short_target if rc2 == 0 else ""))
    push_target(ctx)
    print()
    print("%s %s: %s pushed." % (CHECK, PROG, t))
    return 0


def _guards(ctx):
    t = ctx.target
    prefixes = [os.path.join(ctx.main, d, "worktrees") + os.sep for d in hp.HARNESS_DIRNAMES]
    if not any(ctx.worktree.startswith(p) for p in prefixes):
        die("not inside a managed worktree (%s), run this from the worktree" % ctx.worktree)
    if ctx.branch == t:
        die("already on %s; nothing to land" % t)
    _rc, status = capture(Ctx.git(ctx.worktree, "status", "--porcelain"))
    if status.strip():
        die("worktree has uncommitted changes, commit them first")
    main_branch = assign(Ctx.git(ctx.main, "symbolic-ref", "--short", "HEAD"))
    if main_branch != t:
        die("main checkout is on '%s', not '%s', switch it first" % (main_branch, t))


def _main_dirt(ctx):
    
    
    
    
    
    fn = ctx.hook("prepare_main")
    if fn:
        fn(ctx)
    args = Ctx.git(ctx.main, "status", "--porcelain", "-z")
    if not ctx.project.main_status_untracked:
        args.append("--untracked-files=no")
    dirty = _porcelain_paths(assign(args))
    if not dirty:
        return
    branch_paths = set(_nul_list(assign(Ctx.git(
        ctx.worktree, "diff", "--name-only", "-z", "%s...%s" % (ctx.target, ctx.branch)))))
    conflicting, staged = [], []
    for p in sorted(set(dirty) & branch_paths):
        rc, want = capture(Ctx.git(ctx.worktree, "rev-parse", "--quiet", "--verify",
                                   "%s:%s" % (ctx.branch, p)), quiet_err=True)
        want = want if rc == 0 else ""
        have = ""
        full = os.path.join(ctx.main, p)
        if os.path.isfile(full):
            rc, have = capture(Ctx.git(ctx.main, "hash-object", "--", full), quiet_err=True)
            have = have if rc == 0 else ""
        if want and want == have:
            if run(Ctx.git(ctx.main, "add", "--", p)).returncode != 0:
                die("could not stage %s in the main checkout" % p)
            say("%s is dirty in the main checkout but identical to the branch's copy, staged" % p)
            staged.append(p)
        else:
            conflicting.append(p)
    if conflicting:
        die("main checkout has uncommitted changes to paths this branch also changes, "
            "resolve these first:" + "".join("\n  " + p for p in conflicting))
    ctx.staged = staged
    remaining = [p for p in dirty if p not in staged]
    if remaining:
        say("main checkout has unrelated uncommitted changes, left untouched:")
        for p in remaining:
            print("    " + p)


def _freshness(ctx):
    
    
    t, m = ctx.target, ctx.main
    extend_ssh()
    if run(Ctx.git(m, "fetch", "--quiet", "origin", t), quiet_out=True, quiet_err=True).returncode != 0:
        warn("could not fetch origin/%s (offline?), landing onto local %s as-is; the push may "
             "be rejected if the remote moved" % (t, t))
        return
    remote = "origin/%s" % t
    if run(Ctx.git(m, "merge-base", "--is-ancestor", remote, t), quiet_err=True).returncode == 0:
        return
    if run(Ctx.git(m, "merge-base", "--is-ancestor", t, remote), quiet_err=True).returncode != 0:
        die("local %s and %s have DIVERGED, reconcile the main checkout before landing "
            "(nothing was changed)" % (t, remote))
    say("local %s is behind %s, fast-forwarding it first" % (t, remote))
    if run(Ctx.git(m, "merge", "--ff-only", "--quiet", remote)).returncode != 0:
        die("could not fast-forward %s onto %s (a dirty path in the way?), reconcile the main "
            "checkout by hand" % (t, remote))


def _relevance_skips(ctx):
    "True when gate-relevance.py says no changed path can reach a build.\n    The judgment reads GATE_RELEVANCE, else the store's gate-relevance.py; the globs\n    are the project's own additions to its safe list."
    tool = os.environ.get("GATE_RELEVANCE") or _store_script("gate-relevance.py")
    if not tool or not os.path.isfile(tool):
        return False
    rc, out = capture([sys.executable, tool, ctx.worktree, ctx.target, ctx.branch]
                      + list(ctx.project.irrelevant_globs))
    if rc != 0:
        return False
    say(out)
    return True


_KILL_GRACE = 5


def _kill_group(proc):
    "End a gate and every process it started. Each gate runs in its own session, so\n    the group id is its pid; SIGKILL follows the grace even when the leader has exited,\n    since a test runner's workers outlive it."
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        if sig == signal.SIGTERM:
            try:
                proc.wait(timeout=_KILL_GRACE)
            except subprocess.TimeoutExpired:
                pass
    proc.wait()


def _deadline(ctx, g):
    return g.deadline if g.deadline is not None else ctx.project.gate_deadline


def _wait_gate(proc, until):
    "The gate's exit code, or None when it ran past `until` (a monotonic time; None\n    waits for ever)."
    try:
        return proc.wait(timeout=None if until is None else max(0.0, until - time.monotonic()))
    except subprocess.TimeoutExpired:
        return None




_QUEUE_CAP = 7200


def _wait_started(proc, limit, started, label):
    '_wait_gate for a command that first waits in a queue: the `limit` seconds count\n    from the first check that finds started(proc) true, not from launch. A command still\n    queued after _QUEUE_CAP is killed and the landing dies saying so.'
    queued = time.monotonic()
    began = None
    while True:
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if began is None and started(proc):
            began = now
        if began is not None and now - began >= limit:
            return None
        if began is None and now - queued >= _QUEUE_CAP:
            _kill_group(proc)
            die("the %s waited %ds in its queue without starting and was killed. Something "
                "holds that queue that long; look at it before running wt-finish again."
                % (label, _QUEUE_CAP))


def _wait_for(g, proc, limit, until):
    
    
    if limit is not None and g.started is not None:
        return _wait_started(proc, limit, g.started, g.label)
    return _wait_gate(proc, until)





_TEST_NAME = r'("[^"]*"|\S+)'
_TEST_STARTED = (re.compile(r"^Test Case '(.+)' started\."),
                 re.compile(r"^[^A-Za-z]*Test %s started\.$" % _TEST_NAME))
_TEST_ENDED = (re.compile(r"^Test Case '(.+)' (?:passed|failed|skipped)\b"),
               re.compile(r"^[^A-Za-z]*Test %s (?:with \d+ test cases? )?"
                          r"(?:passed|failed|skipped)\b" % _TEST_NAME))


def running_tests(output):
    "The tests `output` shows started and never finished, in the order they started.\n    Counted per name: swift-testing prints unqualified names and runs suites in parallel,\n    so two suites' `basic()` can both start and only one end."
    running = {}
    for line in output.splitlines():
        line = line.strip()
        for pattern in _TEST_STARTED:
            m = pattern.match(line)
            if m and m.group(1) != "run":
                running[m.group(1)] = running.get(m.group(1), 0) + 1
        for pattern in _TEST_ENDED:
            m = pattern.match(line)
            if m and m.group(1) in running:
                running[m.group(1)] -= 1
                if not running[m.group(1)]:
                    del running[m.group(1)]
    return list(running)


def _overran_message(label, limit, output):
    msg = "the %s ran past its %ds deadline and was killed with every process it started." % (
        label, limit)
    running = running_tests(output)
    if running:
        msg += " Still running when it was killed:%s\nRun those on their own" % "".join(
            "\n  " + t for t in running)
    else:
        msg += (" Its output above ends where it stopped; the last tests named there are the "
                "ones that hung. Run them on their own")
    return msg + ("; if it legitimately needs longer, raise gate_deadline in the project's "
                  "wt-finish-core.py.")


def _overran(ctx, g, limit, proc, output=""):
    
    
    _kill_group(proc)
    ctx.failed_gate = g.name
    die(_overran_message(g.label, limit, output))


def run_deadline(args, deadline, label, cwd=None, env=None, tail=40, started=None):
    'run deadline.'
    _flush()
    with tempfile.TemporaryFile() as fh:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            if deadline is not None and started is not None:
                rc = _wait_started(proc, deadline, started, label)
            else:
                rc = _wait_gate(proc, None if deadline is None else time.monotonic() + deadline)
        finally:
            if proc.poll() is None:
                _kill_group(proc)
        fh.seek(0)
        output = fh.read().decode("utf-8", errors="replace")
    if rc is None:
        for line in output.splitlines()[-tail:]:
            print(line)
        die(_overran_message(label, deadline, output))
    return rc, output


def _gates(ctx, only=None, stale=0):
    'only: run just the gates with these names. stale: how many commits the target\n    is ahead of the branch; when nonzero, after_rebase gates are deferred to _regate.'
    fn = ctx.hook("gates")
    gates = list(fn(ctx)) if fn else []
    names = {g.name for g in gates}
    requested = set(ctx.opts.get("checks") or ())
    unknown = requested - names - {"all", "none"}
    if unknown:
        die("unknown gate(s): %s" % ", ".join(sorted(unknown)))
    if "none" in requested and len(requested) != 1:
        die("--check=none cannot be combined with other checks")
    if "all" in requested and len(requested) != 1:
        die("--check=all cannot be combined with named checks")
    by_name = {g.name: g for g in gates}
    if requested == {"none"}:
        if not _relevance_skips(ctx):
            die("--check=none is allowed only when gate-relevance finds no build input")
        selected = {g.name for g in gates if g.required}
    elif "all" in requested:
        selected = set(names)
    elif requested:
        selected = requested | {g.name for g in gates if g.required}
    else:
        selected = {g.name for g in gates if g.required or g.selected_by_default}
    explicit = requested - {"all", "none"}
    if requested and requested != {"none"} and not ctx.opts.get("gate"):
        die("--check cannot be combined with --no-gate")
    strict = (set(names) if requested == {"all"} else
              (explicit | {g.name for g in gates if g.required}))
    if not ctx.opts.get("gate"):
        skipped = [g.label for g in gates if g.name in selected and g.no_gate_skippable
                   and not g.required]
        if skipped:
            say("--no-gate set, skipping %s" % ", ".join(skipped))
        selected = {g.name for g in gates if g.name in selected and
                    (not g.no_gate_skippable or g.required)}
    if any(g.relevance_skippable for g in gates) and _relevance_skips(ctx):
        selected = {g.name for g in gates if g.name in selected and
                    (not g.relevance_skippable or g.required or g.name in explicit)}
    if only is not None:
        selected = {g.name for g in gates if g.name in selected and
                    (g.required or g.name in only)}

    ordered = []
    visiting = set()
    def include(name):
        if name in visiting:
            die("cyclic gate dependency at %s" % name)
        visiting.add(name)
        for dependency in by_name[name].requires:
            if dependency not in by_name:
                die("gate %s requires unknown gate %s" % (name, dependency))
            selected.add(dependency)
            if name in strict:
                strict.add(dependency)
            include(dependency)
        visiting.remove(name)
        gate = by_name[name]
        if gate not in ordered:
            ordered.append(gate)
    for gate in gates:
        if gate.name in selected:
            include(gate.name)
    gates = ordered
    if stale and ctx.project.landing_lock and ctx.mode == "rebase":
        for g in gates:
            if g.after_rebase:
                say("%s gained %d commit(s) since this branch was cut, so the %s waits to "
                    "judge the rebased branch, the tree that lands" % (ctx.target, stale, g.label))
                ctx.deferred.append(g.name)
        gates = [g for g in gates if g.name not in ctx.deferred]
    background = []  
    try:
        for g in gates:
            if not g.enabled(ctx):
                if g.name in strict and not g.allow_noop:
                    die("selected gate %s did not run; remove its incompatible skip flag" % g.name)
                say("skipping %s" % g.label)
                continue
            ctx.failed_gate = g.name
            result = g.run(ctx)
            if result is None and g.name in strict and not g.allow_noop:
                die("selected gate %s did not run; remove its incompatible skip flag" % g.name)
            limit = _deadline(ctx, g)
            if g.parallel:
                if result is None:
                    continue
                if not isinstance(result, Cmd):
                    raise TypeError("parallel gate %s must return a Cmd" % g.name)
                fd, log = tempfile.mkstemp(prefix="wt-finish-%s-" % g.name)
                _flush()
                with os.fdopen(fd, "wb") as fh:
                    proc = subprocess.Popen(result.argv, cwd=result.cwd or ctx.worktree,
                                            env=result.env, stdout=fh, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                background.append((g, result, proc, log, limit,
                                   None if limit is None else time.monotonic() + limit))
            elif isinstance(result, Cmd):
                _flush()
                proc = subprocess.Popen(result.argv, cwd=result.cwd or ctx.worktree,
                                        env=result.env, start_new_session=True)
                try:
                    rc = _wait_for(g, proc, limit,
                                   None if limit is None else time.monotonic() + limit)
                    if rc is None:
                        _overran(ctx, g, limit, proc)
                finally:
                    if proc.poll() is None:
                        _kill_group(proc)
                if rc != 0:
                    die(result.fail)
            ctx.failed_gate = ""
        
        ctx.failed_gate = ""
        while background:
            g, cmd, proc, log, limit, until = background[0]
            say("waiting on the %s..." % g.label)
            rc = _wait_for(g, proc, limit, until)
            if rc is None:
                _kill_group(proc)
            output = ""
            try:
                with open(log, "r", errors="replace") as fh:
                    output = fh.read()
            except OSError:
                pass
            sys.stdout.write(output)
            background.pop(0)
            os.remove(log)
            if rc is None:
                _overran(ctx, g, limit, proc, output)
            if rc != 0:
                ctx.failed_gate = g.name
                die(cmd.fail)
        ctx.failed_gate = ""
    finally:
        for _g, _cmd, proc, log, _limit, _until in background:
            if proc.poll() is None:
                _kill_group(proc)
            try:
                os.remove(log)
            except OSError:
                pass


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError):
        return False
    return True


class _Lock:
    'One landing at a time holds the window from the rebase through the push. mkdir is\n    the lock (atomic everywhere, macOS has no flock); the pid inside lets a lock left by\n    a killed landing be broken, not waited out.'

    def __init__(self, path):
        self.path = path
        self.held = False

    
    
    
    WAIT = 7200
    NOTE_EVERY = 180

    def take(self):
        
        
        line = self.path + ".waiting"
        os.makedirs(line, exist_ok=True)
        mine = os.path.join(line, str(os.getpid()))
        open(mine, "w").close()
        try:
            self._take(line)
        finally:
            try:
                os.remove(mine)
            except OSError:
                pass

    def _note(self, line, holder, waited):
        others = 0
        for name in os.listdir(line):
            if name == str(os.getpid()):
                continue
            if _pid_alive(name):
                others += 1
            else:
                try:
                    os.remove(os.path.join(line, name))
                except OSError:
                    pass
        try:
            held = "%ds" % (time.time() - os.stat(self.path).st_mtime)
        except OSError:
            held = "an unknown time"
        say("still waiting for the landing lock after %ds: pid %s has held it for %s, and %d "
            "other landing(s) wait too" % (waited, holder or "unknown", held, others))

    def _take(self, line):
        waited = 0
        while True:
            try:
                os.mkdir(self.path)
                break
            except FileExistsError:
                pass
            holder = ""
            try:
                with open(os.path.join(self.path, "pid"), encoding="utf-8") as fh:
                    holder = fh.read().strip()
            except OSError:
                pass
            if holder and not _pid_alive(holder):
                say("breaking a landing lock left behind by dead pid %s" % holder)
                shutil.rmtree(self.path, ignore_errors=True)
                continue
            if waited == 0:
                say("another landing holds the lock (pid %s), waiting for it" % (holder or "unknown"))
            elif waited % self.NOTE_EVERY == 0:
                self._note(line, holder, waited)
            time.sleep(2)
            waited += 2
            if waited >= self.WAIT:
                die("waited %d minutes for the landing lock at %s, look at what holds it before "
                    "removing it" % (self.WAIT // 60, self.path))
        with open(os.path.join(self.path, "pid"), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        self.held = True
        if waited:
            say("took the landing lock after %ds" % waited)

    def release(self):
        if self.held:
            shutil.rmtree(self.path, ignore_errors=True)
            self.held = False


def _rebase(ctx):
    t, b = ctx.target, ctx.branch
    if ctx.behind == 0:
        say("branch is already on top of %s, no rebase needed" % t)
        return
    pre = ctx.pre_rebase = assign(Ctx.git(ctx.worktree, "rev-parse", "HEAD"))
    say("rebasing %s onto %s (%d commit(s) behind)" % (b, t, ctx.behind))
    say("pre-rebase HEAD is %s (recover with: git reset --hard %s)" % (pre, pre))
    if run(Ctx.git(ctx.worktree, "rebase", t)).returncode != 0:
        run(Ctx.git(ctx.worktree, "rebase", "--abort"), quiet_out=True, quiet_err=True)
        die("rebase conflict replaying %s onto %s, resolve manually, or re-run with --merge to "
            "land as a merge commit instead" % (b, t))
    ctx.rebased = True


_UNSEEN_SHOWN = 20


def _regate(ctx):
    
    
    
    
    
    
    
    if not ctx.project.landing_lock:
        return
    t = ctx.target
    if not ctx.rebased:
        say("%s has not moved since the gate built this branch, not building it again" % t)
        
        
        if ctx.deferred:
            _gates(ctx, only=ctx.deferred)
        return
    unseen = assign(Ctx.git(ctx.worktree, "log", "--oneline", "--no-decorate",
                            "%s..%s" % (ctx.gated_base, ctx.main_before))).splitlines()
    say("%s gained %d commit(s) the gate did not build, building the rebased branch again..."
        % (t, len(unseen)))
    try:
        _gates(ctx)
    except Died as e:
        if ctx.failed_gate in ctx.deferred:
            
            
            raise Died("%s\nrefusing to land: the rebased branch, the tree that would land, "
                       "fails this gate. It waited for the rebase because %s had moved; its "
                       "output above says what it found.\n"
                       "The worktree is already rebased onto %s: fix it there, commit, and "
                       "run wt-finish again." % (e.msg or "gate failed", t, t), e.code)
        shown = "".join("\n  " + c for c in unseen[:_UNSEEN_SHOWN])
        if len(unseen) > _UNSEEN_SHOWN:
            shown += "\n  ... and %d more" % (len(unseen) - _UNSEEN_SHOWN)
        raise Died("%s\nrefusing to land: the branch passes its gate on its own base but "
                   "fails on top of %s. The first gate did not build these commits on %s; "
                   "look for the conflict there first:%s\nThe worktree is already rebased "
                   "onto %s: fix it there, commit, and run wt-finish again."
                   % (e.msg or "gate failed", t, t, shown, t), e.code)


def _land(ctx):
    t, b = ctx.target, ctx.branch
    if ctx.mode == "merge":
        
        if ctx.staged:
            msg = "Sync %d byte-identical path(s) before landing" % len(ctx.staged)
            if run(Ctx.git(ctx.main, "commit", "-q", "-m", msg, "--") + ctx.staged).returncode != 0:
                die("could not commit the byte-identical path(s) in the main checkout")
        if ctx.opts.get("merge"):
            say("--merge set, merging into %s..." % t)
        else:
            say("merging %s into %s..." % (b, t))
        if run(Ctx.git(ctx.main, "merge", "--no-ff", b, "-m", "Merge %s into %s" % (b, t))).returncode != 0:
            run(Ctx.git(ctx.main, "merge", "--abort"), quiet_out=True, quiet_err=True)
            die("merge conflict landing %s onto %s, resolve manually" % (b, t))
        fn = ctx.hook("after_combine")
        if fn:
            fn(ctx, ctx.main)
        return
    
    say("fast-forwarding %s onto %s..." % (t, b))
    if run(Ctx.git(ctx.main, "merge", "--ff-only", b)).returncode != 0:
        die("%s moved since the rebase, nothing was changed; re-run wt-finish to replay onto "
            "the new %s" % (t, t))


def _publish(ctx):
    opt_in = ctx.project.push.opt_in_flag
    if opt_in and not ctx.opts.get(opt_key(opt_in)):
        print()
        say("landed locally, not pushed (pass %s to push)." % opt_in)
        return
    push_target(ctx)


def park_seat(ctx):
    "For a project's cleanup hook. True when the landed worktree is a seat\n    (wt_seats.py): it stays, because its checkout and its build output are what make the\n    next task's build compile only what changed. It is left on no branch at main, the\n    commit it just landed, so no file in it changes. False for any other worktree, which\n    the hook removes."
    wt = ctx.worktree
    name = os.path.basename(os.path.normpath(wt))
    if not name.startswith("seat-"):
        return False
    if ctx.run(["git", "-C", wt, "switch", "-q", "--detach", "main"], quiet_err=True).returncode == 0:
        ctx.say("left seat %s at main with its build, for the next task" % name)
    else:
        ctx.warn("could not put seat %s back on main: look at 'git -C %s status'" % (name, wt))
    return True


def _cleanup(ctx):
    if ctx.project.cleanup == "hook":
        fn = ctx.hook("cleanup")
        if fn:
            fn(ctx)
        return
    
    sweep = _store_script("wt-sweep.py")
    if sweep and os.path.isfile(sweep):
        run([sys.executable, sweep, ctx.main])


def _report(ctx):
    t, b = ctx.target, ctx.branch
    tail = " and pushed" if ctx.pushed else ", not pushed"
    print()
    if ctx.mode == "merge":
        print("%s %s: %s merged into %s%s." % (CHECK, PROG, b, t, tail))
    else:
        print("%s %s: %s rebased onto %s, fast-forwarded%s." % (CHECK, PROG, b, t, tail))
    if ctx.project.next_step:
        print("   " + ctx.project.next_step)


def _land_all(ctx):
    if ctx.opts.get("push_only"):
        return _push_only(ctx)
    _guards(ctx)
    _main_dirt(ctx)
    t, b = ctx.target, ctx.branch
    ctx.ahead = int(assign(Ctx.git(ctx.worktree, "rev-list", "--count", "%s..%s" % (t, b))))
    if ctx.ahead == 0:
        die("%s has no commits beyond %s, nothing to land" % (b, t))
    say("landing %s (%d commit(s)) onto %s" % (b, ctx.ahead, t))
    _freshness(ctx)
    early = ctx.project.rebase_first and ctx.project.landing_lock and ctx.mode == "rebase"
    if early:
        
        
        ctx.behind = int(assign(Ctx.git(ctx.worktree, "rev-list", "--count", "%s..%s" % (b, t))))
        _rebase(ctx)
        early_pre, ctx.rebased = ctx.pre_rebase, False
    ctx.changed_paths = _nul_list(assign(Ctx.git(
        ctx.worktree, "diff", "--name-only", "-z", "%s...%s" % (t, b))))
    
    
    
    
    ctx.gated_base = assign(Ctx.git(ctx.worktree, "merge-base", t, b))
    try:
        _gates(ctx, stale=int(assign(Ctx.git(ctx.worktree, "rev-list", "--count", "%s..%s" % (b, t)))))
    except Died as e:
        
        if not (early and early_pre) or e.msg is None:
            raise
        raise Died("%s\nThe worktree was rebased onto %s before the gates ran; the branch as "
                   "committed is %s (git reset --hard %s restores it)."
                   % (e.msg or "gate failed", t, early_pre, early_pre), e.code)
    lock = None
    if ctx.project.landing_lock:
        lock = _Lock(os.path.join(ctx.main, ".git", "wt-finish-landing.lock"))
    try:
        if lock:
            lock.take()
        ctx.main_before = assign(Ctx.git(ctx.main, "rev-parse", t))
        ctx.behind = int(assign(Ctx.git(ctx.worktree, "rev-list", "--count", "%s..%s" % (b, t))))
        if ctx.mode == "rebase":
            _rebase(ctx)
            _regate(ctx)
            fn = ctx.hook("after_combine")
            if fn:
                fn(ctx, ctx.worktree)
        fn = ctx.hook("before_land")
        if fn:
            fn(ctx)
        _land(ctx)
        _publish(ctx)
    finally:
        if lock:
            lock.release()
    fn = ctx.hook("after_publish")
    if fn:
        fn(ctx)
    _cleanup(ctx)
    _report(ctx)
    return 0


def _on_sigterm(signum, frame):
    raise Terminated()


def run_project(module, project, flags=(), argv=None):
    "Entry point for a project's wt-finish-core.py. Returns the exit code. module is\n    the project module or its globals(), where the optional hooks are looked up."
    
    
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(line_buffering=True)
    opts, rc = _parse(sys.argv[1:] if argv is None else argv, project, flags)
    if opts is None:
        return rc
    store = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ctx = Ctx(project, module, opts, store)
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        _resolve(ctx)
        return _land_all(ctx)
    except Died as e:
        if e.msg is not None:
            print("%s: %s" % (PROG, e.msg), file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130
    except Terminated:
        return 143
