#!/usr/bin/env python3
'home-materialize.py - project the store\'s neutral global/ content into Claude\nCode\'s home config dir (~/.claude). Home-scope analog of agents-materialize.\n\nThe store (~/.agent-context/global) is the up-to-date canonical. ~/.claude holds\nonly *generated projections* of our authored content plus Claude\'s own runtime\ndata (credentials, sessions, projects/transcripts, plugins, cache, ...) - the\nruntime is never touched. Other agents (copilot/pi/opencode) consume the store\nvia the config harness-materialize.py writes.\n\nProjection rules:\n  hooks, scripts   - raw executables: copied verbatim (minus store .meta sidecars).\n  skills, commands - store-format content entities: copied, then the leading\n                     store-frontmatter block (the one carrying `uuid:`) is stripped\n                     from each .md so the harness sees its native frontmatter.\n                     A SKILL.md gets a minimal name+description block re-emitted\n                     (every harness addresses a skill by those two fields).\n  docs             - not projected. Docs are read over MCP with get_doc; a slash\n                     command hands the agent a get_doc pointer. The old\n                     <store>/shared-docs copy is removed (retire_shared_docs).\n  settings.json    - not projected as a file (it is per-machine and holds\n                     machine-local hooks). Instead home-settings-sync.py merges\n                     the store-managed guard/materialization hooks into it.\n\nDRIFT GUARD: before overwriting, any projection file that is newer than its\nstore source and differs is reported (and notified, throttled): a hand-edited\nprojection means someone changed the copy and not the store. The overwrite still happens - the store wins - but it\nis always reported.\n\nDevice-agnostic (no absolute paths beyond $HOME), idempotent, safe to re-run.\n\nCONCURRENCY NOTE: this script runs as a SessionStart hook alongside sibling\nSessionStart hooks that live under ~/.claude/hooks - the very dir it rebuilds.\nAny approach that momentarily removes dst - a naive rmtree+copy, or even a\ntemp-build + swap - leaves the directory absent for a window during which the\nharness, racing to exec those sibling hooks, hits "No such file or directory"\n(or "Permission denied" before +x is restored). project() therefore updates\neach projection IN PLACE with rsync (per-file temp+rename, --delete to prune):\ndst always exists, and only changed files are ever rewritten.\n\nUsage: home-materialize.py [--force] [--session-start]\n  --force          bypass the steady-state skip (a write made mid-turn)\n  --session-start  forwarded to preflight-crash-watch, which counts session starts\nFresh machine: git clone <remote> ~/.agent-context && python3 ~/.agent-context/global/scripts/home-materialize.py\n\nObservations guarded: #27, #227, #246, #276, #309, #437, #440, #442.'
import glob
import io
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
from frontmatter_strip import strip_frontmatter_tree, strip_store_frontmatter  
import tree_copy







if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(line_buffering=True)

DEPS_CHECK_MAX_AGE = 12 * 3600
DEPS_CHECK_RETRY = 3600


def spawn_deps_check(state_dir=None, script=None, now=None, popen=subprocess.Popen):
    "Start deps-check.py detached when deps.json is absent or older than 12 hours.\n\n    Fire and forget: the child gets its own session, no stdin, and its output goes to\n    deps-check.log under the state dir. This never waits for it and never raises, so a slow,\n    missing or crashing checker cannot change this script's output, exit status or duration.\n    The report shows at the next session start, through the health record deps-check writes.\n    Returns True when a checker was started."
    try:
        state_dir = state_dir or hp.state_dir()
        script = script or os.path.join(os.path.dirname(os.path.abspath(__file__)), "deps-check.py")
        if not os.path.isfile(script):
            return False
        t = time.time() if now is None else now
        try:
            if t - os.path.getmtime(os.path.join(state_dir, "deps.json")) < DEPS_CHECK_MAX_AGE:
                return False
        except OSError:
            pass
        
        
        try:
            if t - os.path.getmtime(os.path.join(state_dir, "deps-check.log")) < DEPS_CHECK_RETRY:
                return False
        except OSError:
            pass
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "deps-check.log"), "w", encoding="utf-8") as log:
            popen([sys.executable, script], stdin=subprocess.DEVNULL, stdout=log,
                  stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        return True
    except Exception:
        return False
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(line_buffering=True)

HOME = os.environ.get("HOME") or os.path.expanduser("~")
ROOT = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(HOME, ".agent-context")
STORE = os.path.join(ROOT, "global")
CL = hp.claude_home(HOME)

RSYNC_EXCLUDES = tree_copy.rsync_excludes()






WINDOWS = os.name == "nt"


def stage_copy(src, dst):
    'Copy a store directory into the stage; a failed copy stops the script, as run_checked\n    stops it on a failed rsync.'
    ok, err = tree_copy.copy_tree(src, dst)
    if not ok:
        print("home-materialize: copying %s failed: %s" % (src, err), file=sys.stderr)
        sys.exit(1)


def _point_at(dst, gen, parent, tag):
    'Point dst at the generation directory gen. A relative symlink renamed over dst on\n    POSIX, so dst always resolves to one complete generation. Windows cannot rename over a\n    directory, so there the old junction goes first and a reader can find dst missing for\n    that moment; the next session start projects again.'
    tmp_link = os.path.join(parent, "%s%s-link-%d" % (GEN_PREFIX, tag, os.getpid()))
    if os.path.lexists(tmp_link):
        if WINDOWS and hp.is_link(tmp_link):
            os.rmdir(tmp_link)
        else:
            os.remove(tmp_link)
    if not WINDOWS:
        os.symlink(os.path.basename(gen), tmp_link)
        os.replace(tmp_link, dst)  
        return
    import _winapi
    _winapi.CreateJunction(os.path.abspath(gen), tmp_link)
    if hp.is_link(dst):
        os.rmdir(dst)  
    os.rename(tmp_link, dst)


def run_checked(argv):
    "Run an external program the way bash's `set -e` would: on a nonzero\n    exit, stop the whole script immediately with that same exit code."
    proc = subprocess.run(argv)
    if proc.returncode != 0:
        sys.exit(proc.returncode)


def prune_older_than(root, days, mindepth):
    'Delete entries under root older than `days`, at `mindepth`\n    path components below it (mirrors find -mindepth N -maxdepth N -mtime +D\n    -exec rm -rf {} +). Errors are swallowed, matching `2>/dev/null || true`.'
    cutoff = time.time() - days * 86400
    try:
        levels = [root]
        for _ in range(mindepth):
            nxt = []
            for d in levels:
                try:
                    nxt.extend(os.path.join(d, name) for name in os.listdir(d))
                except OSError:
                    pass
            levels = nxt
        for entry in levels:
            try:
                if os.lstat(entry).st_mtime < cutoff:
                    if os.path.isdir(entry) and not hp.is_link(entry):
                        import shutil
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        os.remove(entry)
            except OSError:
                pass
    except OSError:
        pass


def run_state_migration():
    'Move the legacy Claude-side state dir into the neutral state dir (state-migrate.py).\n\n    Returns report lines. A failed migration is reported and never stops the projection.'
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state-migrate.py")
    try:
        spec = importlib.util.spec_from_file_location("state_migrate", path)
        if spec is None or spec.loader is None:
            return ["state-migrate FAILED: cannot load %s" % path]
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.migrate(HOME)
    except Exception as e:
        return ["state-migrate FAILED: %s" % e]






FOREIGN_DIRS = {"skills": ("synced",)}

GEN_PREFIX = ".home-mat-gen-"


def source_entry_count(src):
    'Top-level entries in a store source dir that a projection of it must hold: what\n    scrub() and the RSYNC_EXCLUDES leave, so the count compares like with like.'
    try:
        names = os.listdir(src)
    except OSError:
        return 0
    return sum(1 for n in names
               if n not in (".DS_Store", "__pycache__")
               and not n.endswith(".meta.toml") and not n.endswith(".meta.json"))


def swap_projection_dir(dst, build_dir, keep=(), expected=0):
    "Atomically point dst at build_dir.\n\n    Multiple SessionStart hooks can start within seconds of each other and each\n    run home-materialize.py concurrently. An rsync --delete in place is not atomic:\n    two runs racing against the same dst can interleave their delete and copy\n    phases, and a sibling hook reading ~/.claude/agents mid-race can see it empty.\n    A symlink swap has no such window: dst always resolves to a complete\n    generation, never a partial one, whichever run's swap lands last.\n\n    `keep` names subdirectories under dst that this script does not own (e.g. the\n    Claude Desktop skill bucket at skills/synced) and must carry forward into the\n    new generation, never lose."
    parent = os.path.dirname(dst)
    
    
    lock_fd = os.open(parent, os.O_RDONLY) if not WINDOWS else os.open(
        os.path.join(parent, ".home-mat.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            if WINDOWS:
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)  
                        break
                    except OSError:
                        continue
            else:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass

        tag = os.path.basename(dst)

        
        
        live_dir = dst
        if hp.is_link(dst):
            target = os.readlink(dst)
            live_dir = target if os.path.isabs(target) else os.path.join(parent, target)

        
        
        
        
        
        
        try:
            build_count = len(os.listdir(build_dir))
        except OSError:
            build_count = 0
        try:
            live_count = len(os.listdir(live_dir))
        except OSError:
            live_count = 0
        if build_count == 0 and live_count > 0:
            print("home-materialize: %s build came up empty while %d entries are live — "
                  "treating as a transient source failure and keeping the current generation"
                  % (tag, live_count))
            return False
        if build_count < expected:
            msg = ("home-materialize: %s build holds %d of the %d entries the store has — "
                   "refusing the swap and keeping the current generation (%d entries live)"
                   % (tag, build_count, expected, live_count))
            print(msg)
            print(msg, file=sys.stderr)
            return False

        gen = os.path.join(parent, "%s%s-%d-%d" % (GEN_PREFIX, tag, int(time.time_ns()), os.getpid()))
        if os.path.lexists(gen):
            shutil.rmtree(gen, ignore_errors=True)
        shutil.move(build_dir, gen)
        for name in keep:
            src_keep = os.path.join(dst, name)
            if os.path.isdir(src_keep) and not hp.is_link(src_keep):
                shutil.copytree(src_keep, os.path.join(gen, name), dirs_exist_ok=True)

        previous = None
        if hp.is_link(dst):
            target = os.readlink(dst)
            previous = target if os.path.isabs(target) else os.path.join(parent, target)
        elif os.path.lexists(dst):
            
            
            previous = os.path.join(parent, "%s%s-adopted-%d" % (GEN_PREFIX, tag, os.getpid()))
            os.rename(dst, previous)

        _point_at(dst, gen, parent, tag)

        keep_paths = {os.path.abspath(gen)}
        if previous:
            keep_paths.add(os.path.abspath(previous))
        
        
        
        
        own_prefix = "%s%s-" % (GEN_PREFIX, tag)
        for name in os.listdir(parent):
            p = os.path.join(parent, name)
            if (name.startswith(own_prefix) and os.path.isdir(p) and not hp.is_link(p)
                    and os.path.abspath(p) not in keep_paths):
                shutil.rmtree(p, ignore_errors=True)
        return True
    finally:
        os.close(lock_fd)  


def scrub(path, keep=()):
    'Drop store-only artifacts from a freshly-copied projection.'
    for dp, dirnames, filenames in os.walk(path):
        if dp == path:
            dirnames[:] = [d for d in dirnames if d not in keep]
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")
            import shutil
            shutil.rmtree(os.path.join(dp, "__pycache__"), ignore_errors=True)
        for name in filenames:
            if name.endswith(".meta.toml") or name.endswith(".meta.json") or name == ".DS_Store":
                try:
                    os.remove(os.path.join(dp, name))
                except OSError:
                    pass


def check_drift(src, dst, drift):
    'Collect hand-edited projection files: dst is newer than its store src\n    and the contents differ.'
    if not os.path.isdir(dst):
        return
    for dp, dirnames, filenames in os.walk(dst):
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")
        for name in filenames:
            if name == ".DS_Store" or name.endswith(".pyc"):
                continue
            f = os.path.join(dp, name)
            rel = os.path.relpath(f, dst)
            s = os.path.join(src, rel)
            if not os.path.isfile(s):
                continue
            try:
                f_mtime = os.path.getmtime(f)
                s_mtime = os.path.getmtime(s)
            except OSError:
                continue
            if f_mtime <= s_mtime:
                continue
            try:
                with open(f, "rb") as fh1, open(s, "rb") as fh2:
                    if fh1.read() == fh2.read():
                        continue
            except OSError:
                continue
            drift.append(" %s/%s" % (os.path.basename(dst), rel))


def project(src, dst, drift, exec_bits=False):
    'Update dst in place from src (never remove dst - see CONCURRENCY NOTE).'
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    check_drift(src, dst, drift)
    
    
    
    
    run_checked(["rsync", "-rlt", "--delete"] + RSYNC_EXCLUDES + [src + "/", dst + "/"])
    scrub(dst)
    if exec_bits:
        for dp, _dn, filenames in os.walk(dst):
            for name in filenames:
                p = os.path.join(dp, name)
                try:
                    os.chmod(p, os.stat(p).st_mode | 0o111)
                except OSError:
                    pass


def any_newer_than(paths, ref_mtime):
    for p in paths:
        if os.path.isfile(p):
            try:
                if os.path.getmtime(p) > ref_mtime:
                    return True
            except OSError:
                pass
            continue
        if not os.path.isdir(p):
            continue
        for dp, _dn, filenames in os.walk(p):
            for name in filenames:
                fp = os.path.join(dp, name)
                try:
                    if os.path.getmtime(fp) > ref_mtime:
                        return True
                except OSError:
                    continue
    return False


def hr(*args):
    'hr(component, "--ok") | hr(component, "--fail", "<detail>")'
    path = os.path.join(STORE, "scripts", "health-record.py")
    if not os.path.isfile(path):
        return
    try:
        subprocess.run([sys.executable, path] + list(args), stderr=subprocess.DEVNULL)
    except OSError:
        pass




LEGACY_WIRING = re.compile(r"(?:~|\$HOME|\$\{HOME\}|%h|" + re.escape(HOME) + r")/"
                           + re.escape(hp.CLAUDE_DIRNAME) + r"/(hooks|scripts|docs|hook-dispatch\.json)")


def legacy_wiring_files():
    'Config files that may still name the retired ~/.claude hooks and scripts directories.'
    xcode = os.path.join(HOME, "Library", "Developer", "Xcode", "CodingAssistant", "ClaudeAgentConfig")
    return [hp.settings_file(HOME),
            os.path.join(xcode, "settings.json"),
            os.path.join(HOME, ".codex", "hooks.json"),
            os.path.join(HOME, ".pi", "agent", "extensions", "agent-context-hooks.ts"),
            os.path.join(HOME, ".config", "opencode", "plugins", "agent-context-guards.js"),
            os.path.join(hp.claude_home(HOME), "settings.local.json")]


def retire_claude_projections():
    'Remove ~/.claude/{hooks,scripts,docs} and the old hook-dispatch.json.\n\n    Hooks and scripts run in place from the store and docs are read over MCP, so these\n    copies are dead weight. They stay while any harness config still names them: a session\n    that loaded its wiring before the switch keeps calling the old paths, and deleting them\n    then would turn every guard into a silent no-op. Returns report lines, [] when there\n    is nothing to do.'
    import shutil
    targets = [os.path.join(CL, sub) for sub in ("hooks", "scripts", "docs")]
    targets.append(os.path.join(CL, "hook-dispatch.json"))
    present = [t for t in targets if os.path.lexists(t)]
    if not present:
        return []
    for path in legacy_wiring_files():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                if LEGACY_WIRING.search(fh.read()):
                    return ["retire-claude-projections: kept %s, still named by %s"
                            % (", ".join(os.path.basename(t) for t in present), path)]
        except FileNotFoundError:
            continue
        except OSError as e:
            
            return ["retire-claude-projections: kept the copies, cannot read %s: %s" % (path, e)]
    notes = []
    for path in present:
        try:
            if os.path.isdir(path) and not hp.is_link(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            notes.append("retire-claude-projections: removed %s" % path)
        except OSError as e:
            notes.append("retire-claude-projections FAILED on %s: %s" % (path, e))
    return notes


def retire_shared_docs():
    'Remove <store>/shared-docs, the docs copy this script used to build. Docs are read\n    over MCP (get_doc), and a slash command hands the agent a get_doc pointer, so nothing\n    needs them on disk. The tree is generated and gitignored: nothing else writes it.\n    Returns report lines, [] when there is nothing to do.'
    import shutil
    dst = hp.docs_dir(HOME)
    if not os.path.lexists(dst):
        return []
    try:
        if os.path.isdir(dst) and not hp.is_link(dst):
            shutil.rmtree(dst)
        else:
            os.remove(dst)
    except OSError as e:
        return ["retire-shared-docs FAILED on %s: %s" % (dst, e)]
    return ["retire-shared-docs: removed %s" % dst]


def project_claude_md():
    "Write ~/.claude/CLAUDE.md from the store's AGENTS.md, as an exact mirror.\n\n    Claude Code reads CLAUDE.md at user scope and does not read AGENTS.md there, so the\n    neutral file is projected. Nothing else is kept, a <wikis> block included:\n    no tool generates it and its target does not exist, so the next\n    run removes it. No AGENTS.md means no write. Returns report lines, [] when nothing\n    changed."
    src = os.path.join(ROOT, "AGENTS.md")
    dst = os.path.join(CL, "CLAUDE.md")
    try:
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    want = text.rstrip("\n") + "\n"
    try:
        with open(dst, encoding="utf-8") as fh:
            if fh.read() == want:
                return []
    except OSError:
        pass
    os.makedirs(CL, exist_ok=True)
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(want)
    os.replace(tmp, dst)
    return ["project-claude-md: wrote %s from AGENTS.md" % dst]


KNOWN_FLAGS = ("--force", "--session-start")


def main(argv):
    
    
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    unknown = [a for a in argv if a not in KNOWN_FLAGS]
    if unknown:
        print("home-materialize: unknown flag(s): %s" % " ".join(unknown), file=sys.stderr)
        print("home-materialize: this script WRITES to ~/.claude, so an unknown flag is refused.",
              file=sys.stderr)
        return 2
    if not os.path.isdir(STORE):
        print("home-materialize: no store at %s" % STORE, file=sys.stderr)
        return 1

    for line in run_state_migration():
        print(line)

    
    
    for line in project_claude_md():
        print(line)

    drift = []

    
    
    
    
    
    
    
    stamp = os.path.join(CL, ".home-materialize.stamp")

    
    
    
    cache_tmp = os.path.join(HOME, ".cache", "tmp")
    os.makedirs(cache_tmp, exist_ok=True)
    prune_older_than(cache_tmp, 2, 1)

    
    
    
    claude_tmp = os.path.join(HOME, ".cache", "claude-tmp")
    os.makedirs(claude_tmp, exist_ok=True)
    os.chmod(claude_tmp, 0o700)
    prune_older_than(claude_tmp, 7, 2)

    proc = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    head_now = proc.stdout.strip() if proc.returncode == 0 else ""
    skip_projection = False

    
    
    
    
    
    
    
    force = len(argv) > 0 and argv[0] == "--force"
    if not force and head_now and os.path.isfile(stamp):
        try:
            with open(stamp, encoding="utf-8") as fh:
                stamp_content = fh.read()
        except OSError:
            stamp_content = ""
        if stamp_content == head_now:
            ref_mtime = os.path.getmtime(stamp)
            fresh_src_paths = [STORE] + sorted(glob.glob(os.path.join(ROOT, "projects", "*", "commands")))
            fresh_dst_paths = [os.path.join(CL, sub) for sub in ("skills", "commands", "agents")]
            if not any_newer_than(fresh_src_paths, ref_mtime) and not any_newer_than(fresh_dst_paths, ref_mtime):
                skip_projection = True

    if skip_projection:
        print("home-materialize: projection current (store HEAD %s, nothing newer) — skipped"
              % head_now[:8])
    else:
        
        
        
        
        
        
        
        
        import tempfile
        tmpdir_env = os.environ.get("TMPDIR") or tempfile.gettempdir()
        stage = tempfile.mkdtemp(prefix="home-mat.", dir=tmpdir_env)
        
        
        expected = {sub: source_entry_count(os.path.join(STORE, sub))
                    for sub in ("skills", "commands", "agents")}
        refused = []
        try:
            if os.path.isdir(os.path.join(STORE, "skills")):
                stage_copy(os.path.join(STORE, "skills"), os.path.join(stage, "skills"))
            if os.path.isdir(os.path.join(STORE, "commands")):
                stage_copy(os.path.join(STORE, "commands"), os.path.join(stage, "commands"))
            
            
            
            
            if os.path.isdir(os.path.join(STORE, "agents")):
                stage_copy(os.path.join(STORE, "agents"), os.path.join(stage, "agents"))
            
            strip_frontmatter_tree([os.path.join(stage, sub)
                                    for sub in ("skills", "commands", "agents")])

            
            
            os.makedirs(CL, exist_ok=True)
            for sub in ("skills", "commands", "agents"):
                stage_sub = os.path.join(stage, sub)
                if not os.path.isdir(stage_sub):
                    continue
                dst_sub = os.path.join(CL, sub)
                check_drift(stage_sub, dst_sub, drift)
                keep = FOREIGN_DIRS.get(sub, ())
                scrub(stage_sub, keep)
                if not swap_projection_dir(dst_sub, stage_sub, keep=keep, expected=expected[sub]):
                    refused.append(sub)
        finally:
            import shutil
            shutil.rmtree(stage, ignore_errors=True)

        
        for line in retire_shared_docs():
            print(line)

        
        
        if drift:
            drift_text = "".join(drift)
            print("home-materialize: drift: these ~/.claude projections were hand-edited "
                  "(newer than their store source) and have been overwritten from the store. "
                  "Port wanted changes into the store:%s" % drift_text, file=sys.stderr)
            dstamp = os.path.join(CL, ".projection-drift-alert.stamp")
            dnow = int(time.time())
            dlast = 0
            if os.path.isfile(dstamp):
                try:
                    with open(dstamp, encoding="utf-8") as fh:
                        dlast = int(fh.read().strip() or 0)
                except (OSError, ValueError):
                    dlast = 0
            notify = os.path.join(HOME, ".local", "bin", "notify")
            if (dnow - dlast) >= 86400 and os.access(notify, os.X_OK):
                try:
                    with open(dstamp, "w", encoding="utf-8") as fh:
                        fh.write("%d\n" % dnow)
                except OSError:
                    pass
                try:
                    hostproc = subprocess.run(["hostname", "-s"], capture_output=True, text=True)
                    hostname = hostproc.stdout.strip() if hostproc.returncode == 0 else "?"
                except OSError:
                    hostname = "?"
                try:
                    subprocess.run([notify, "-t", "agent-context projection drift on %s" % hostname,
                                   "Hand-edited ~/.claude projection(s) overwritten from the store:"
                                   "%s — port the edits into the store if they matter." % drift_text],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass

        
        
        if head_now and not refused:
            try:
                with open(stamp, "w", encoding="utf-8") as fh:
                    fh.write(head_now)
            except OSError:
                pass

    
    
    
    
    
    
    
    
    pin = os.path.join(HOME, ".agent-context", "global", "scripts", "agents-pin.py")
    if os.path.isfile(pin) and os.path.isdir(os.path.join(HOME, ".pi", "agent")):
        rc = subprocess.run([sys.executable, pin, "pi", os.path.join(CL, "agents"),
                            os.path.join(HOME, ".pi", "agent", "agents")]).returncode
        if rc == 0:
            hr("pi-agents-pin", "--ok")
        else:
            print("home-materialize: pi-agents-pin failed (pi workers stay on tier aliases)",
                  file=sys.stderr)
            hr("pi-agents-pin", "--fail",
              "agents-pin.py pi failed, so ~/.pi/agent/agents was not re-derived against pi's "
              "live model catalog. Every pi worker then falls back to the SESSION model instead "
              "of its own tier — a subagent quietly running on the lead's tier, which is the "
              "single failure the pin exists to prevent and is invisible from inside a pi session.")

    
    
    
    

    
    
    
    
    for line in retire_claude_projections():
        print(line)

    
    
    
    client_build = os.path.join(STORE, "scripts", "hook-client-build.py")
    if os.path.isfile(client_build):
        rc = subprocess.run([sys.executable, client_build, "--quiet"]).returncode
        if rc == 0:
            hr("hook-client-build", "--ok")
        else:
            hr("hook-client-build", "--fail",
               "hook-client-build.py exited %d, so this host's hook events start Python on "
               "every call. Exit 3 means no rustc: chezmoi's install-packages provides "
               "`rust`. Run hook-client-build.py by hand to see the error." % rc)

    
    
    settings_sync = os.path.join(STORE, "scripts", "home-settings-sync.py")
    if os.path.isfile(settings_sync):
        rc = subprocess.run([sys.executable, settings_sync]).returncode
        if rc == 0:
            hr("settings-sync", "--ok")
        else:
            print("home-materialize: settings sync skipped (non-fatal)", file=sys.stderr)
            hr("settings-sync", "--fail",
              "home-settings-sync.py failed, so ~/.claude/settings.json was not reconciled with "
              "the store. Any hook, permission or env value the store owns may be stale or "
              "missing on this machine — including the guards (worktree, deploy, git-write, "
              "secret-read).")

    
    
    
    
    
    harness_materialize = os.path.join(STORE, "scripts", "harness-materialize.py")
    if os.path.isfile(harness_materialize):
        rc = subprocess.run([sys.executable, harness_materialize]).returncode
        if rc == 0:
            hr("harness-materialize", "--ok")
        else:
            print("home-materialize: harness-materialize skipped (non-fatal)", file=sys.stderr)
            hr("harness-materialize", "--fail",
              "harness-materialize.py failed, so the other harnesses (opencode, Copilot CLI, "
              "antigravity, pi, Xcode) were not reconciled with the store. Their MCP servers, "
              "global instruction symlinks and skills mirror may be stale — and unlike Claude, "
              "none of them will say so at startup.")

    
    
    
    
    orphan_probe = os.path.join(STORE, "scripts", "store-orphan-probe.py")
    if os.path.isfile(orphan_probe):
        subprocess.run([sys.executable, orphan_probe, "--quiet"])

    
    
    
    
    
    
    
    
    cstamp = os.path.join(CL, ".mirror-converge.stamp")
    if not os.path.isfile(cstamp) or (time.time() - os.path.getmtime(cstamp)) > 360 * 60:
        converge = os.path.join(HOME, ".local", "bin", "git-mirror-converge")
        if os.access(converge, os.X_OK):
            rc = subprocess.run([converge, "--quick"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).returncode
            if rc == 0:
                hr("mirror-converge", "--ok")
            else:
                print("home-materialize: git-mirror-converge skipped (non-fatal)", file=sys.stderr)
                hr("mirror-converge", "--fail",
                  "git-mirror-converge --quick failed, so this machine's repo remotes, auth and "
                  "commit signing are not converged. A push may reach fewer mirrors than expected "
                  "(s1/s2/ls/GitHub) or fail signing — and a missing mirror leg is invisible "
                  "until the day it is needed.")
        try:
            with open(cstamp, "w", encoding="utf-8"):
                pass
        except OSError:
            pass
        converged = "mirrors converged"
    else:
        converged = "mirror converge skipped (<6h)"

    
    
    
    
    
    
    
    
    
    
    crash_watch = os.path.join(STORE, "scripts", "preflight-crash-watch.py")
    if os.path.isfile(crash_watch):
        
        
        
        
        
        
        
        ss_args = ["--session-start"] if "--session-start" in argv else []
        
        
        
        
        
        rc = subprocess.run([sys.executable, crash_watch] + ss_args).returncode
        if rc == 0:
            hr("crash-watch", "--ok")
        else:
            hr("crash-watch", "--fail",
              "preflight-crash-watch.py exited non-zero, so the watchdog on preflight-core-health "
              "did not run. Core systems fail open, which means a degraded LSP, store or daemon "
              "looks identical to a working one; this probe is what makes that visible, and while "
              "it is down nothing is checking the checker.")

    
    
    spawn_deps_check()

    
    
    
    
    print("home-materialize: ~/.claude {skills,commands,agents} projected "
         "from store; settings hooks merged; %s" % converged, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
