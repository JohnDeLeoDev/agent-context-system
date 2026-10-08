'The one place harness directory paths are built.\n\nScripts and hooks import this instead of writing os.path.join(HOME, ".claude", "hooks")\nthemselves. Every accessor returns the path the old literal built, so a migration changes\nno behavior, and moving a directory later is an edit here rather than in 57 files.\ncheck-harness-paths.py finds the sites that still build a path by hand.\n\nImport from a script beside this file:\n\n    import harness_paths\n\nImport from a hook in ../hooks:\n\n    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))\n    import harness_paths\n\nEvery home-scoped accessor takes an optional home=. Without it the accessor follows $HOME\nat call time, so a test that points HOME at a fake home needs no other wiring. Paths are\nnormalized with abspath: a trailing or doubled slash goes, a relative home becomes\nabsolute, and a symlinked home keeps the spelling it was given.\n\nRuns on Python 3.8, the system Python on the Synology nodes. Standard library only.'

import hashlib
import os
import re
import subprocess
import time

CLAUDE_DIRNAME = ".claude"
AGENTS_DIRNAME = ".agents"
STORE_DIRNAME = ".agent-context"
CLAUDE_JSON_NAME = ".claude.json"
HARNESS_DIRNAMES = (AGENTS_DIRNAME, CLAUDE_DIRNAME)




_INTERPRETER = re.compile(r"^(?:(?:ba)?sh|python(?:\d+(?:\.\d+)?)?)(?:\.exe)?$", re.I)


HOOK_LAUNCHER = "hook-client"


def is_interpreter(word):
    "True when a hook command's word is an interpreter or the hook launcher, not the\n    script itself. The one rule; hook-registry, hook-dispatch, agent-python and the\n    registration probe all read commands with it."
    name = os.path.basename(word)
    return name == HOOK_LAUNCHER or bool(_INTERPRETER.match(name))


def command_path(path):
    '`path` as it goes into a command string a harness hands to a shell. On Windows,\n    forward slashes: Claude Code there may run a hook command through Git Bash, cmd or\n    PowerShell, and only a forward-slash path means the same file to all three (Bash reads a\n    backslash as an escape). Windows Python opens either form. Unchanged elsewhere.'
    return path.replace("\\", "/") if os.name == "nt" else path


def is_link(path):
    'A symlink, or on Windows a directory junction: the two ways a projection or a relay\n    `current` points at a directory it does not hold. os.path.isjunction is 3.12+; before\n    it there were no junctions to ask about.'
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda p: False)(path)


def _resolve(home):
    if home is None:
        home = os.environ.get("HOME") or os.path.expanduser("~")
    if not home or home == "~":
        raise RuntimeError("cannot resolve the home directory: home is empty and HOME is unset")
    return os.path.abspath(home)


def home(home=None):
    'The home directory: home= if given, else $HOME, else the user database.'
    return _resolve(home)


def claude_home(home=None):
    return os.path.join(_resolve(home), CLAUDE_DIRNAME)


def store_root(home=None):
    return os.path.join(_resolve(home), STORE_DIRNAME)


def hooks_dir(home=None):
    'Store hooks run in place; nothing is projected into ~/.claude/hooks any more.'
    return os.path.join(store_root(home), "global", "hooks")


def scripts_dir(home=None):
    'Store scripts run in place; nothing is projected into ~/.claude/scripts any more.'
    return os.path.join(store_root(home), "global", "scripts")


def docs_dir(home=None):
    'Per-machine docs projection (frontmatter stripped, project docs namespaced).'
    return os.path.join(store_root(home), "shared-docs")


def hook_registry_file(home=None):
    'The generated hook-dispatch.json registry, a git-ignored per-machine file in the store root.'
    return os.path.join(store_root(home), "hook-dispatch.json")


def state_dir(home=None):
    return os.path.join(_resolve(home), ".local", "state", "agent-context")


def cache_dir(home=None):
    'Rebuildable per-machine data: deleting it costs time, never state.'
    return os.path.join(_resolve(home), ".cache", "agent-context")





DAEMON_COMPONENTS = frozenset({"agent-context sync", "agent-context daemon", "invariants"})


def is_relay_host(home=None):
    'True when this machine reaches the store only through the relay: it has a relay release\n    installed (policy) and no store checkout, the only place a daemon runs. Both are required, so\n    a daemon host with a damaged checkout is never mistaken for a relay host.'
    relay = os.path.join(_resolve(home), ".local", "share", "agent-context", "relay", "current")
    return os.path.exists(relay) and not os.path.exists(os.path.join(store_root(home), ".git"))


def stale_daemon_verdict(rec, home=None):
    'A health record from a daemon this machine no longer runs.'
    return rec.get("component") in DAEMON_COMPONENTS and is_relay_host(home)


def skills_dir(home=None):
    return os.path.join(claude_home(home), "skills")


def commands_dir(home=None):
    return os.path.join(claude_home(home), "commands")


def settings_file(home=None):
    return os.path.join(claude_home(home), "settings.json")


def claude_json(home=None):
    return os.path.join(_resolve(home), CLAUDE_JSON_NAME)


def projects_dir(home=None):
    "Claude Code's own transcript store, ~/.claude/projects (harness runtime data)."
    return os.path.join(claude_home(home), "projects")


def worktree_marks():
    'Path fragments that mark a file as living inside an agent worktree.'
    return tuple(os.sep + os.path.join(scope, "worktrees") + os.sep
                 for scope in (AGENTS_DIRNAME, CLAUDE_DIRNAME))


def project_claude_dir(repo):
    return os.path.join(os.path.abspath(repo), CLAUDE_DIRNAME)


def project_agents_dir(repo):
    return os.path.join(os.path.abspath(repo), AGENTS_DIRNAME)


def relay_install_tool(home=None):
    'The relay installer to run when a mandatory directory is missing locally.\n    RELAY_INSTALL_TOOL overrides it for a hermetic test.'
    return (os.environ.get("RELAY_INSTALL_TOOL")
            or os.path.join(_resolve(home), ".local", "bin", "agent-context-relay-install"))


def ensure_local_checkout(target_dir, home=None, retry_after=300):
    "True if target_dir exists. On a relay-only machine (audit #427) a hook's mandatory\n    directory never materializes on its own: this triggers the relay installer once, at\n    most every retry_after seconds per target_dir, then rechecks. Every installer error\n    (missing binary, non-zero exit, timeout) is swallowed: a hook calling this must still\n    fall through to its own fail-open path when the directory stays missing.\n    RELAY_INSTALL_RETRY_SECS overrides retry_after for a hermetic test."
    if os.path.isdir(target_dir):
        return True
    retry_after = float(os.environ.get("RELAY_INSTALL_RETRY_SECS", retry_after))
    stamp_dir = os.path.join(state_dir(home), "relay-install-attempts")
    key = hashlib.sha256(os.path.abspath(target_dir).encode("utf-8", "surrogateescape")).hexdigest()
    stamp = os.path.join(stamp_dir, key)
    now = time.time()
    try:
        last = os.path.getmtime(stamp)
    except OSError:
        last = None
    if last is not None and now - last < retry_after:
        return os.path.isdir(target_dir)
    try:
        os.makedirs(stamp_dir, exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(str(now))
    except OSError:
        pass
    try:
        subprocess.run([relay_install_tool(home)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.isdir(target_dir)
