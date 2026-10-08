#!/usr/bin/env python3
"Print the Python interpreter this host's hooks and harness commands run under.\n\nUsage: agent-python.py        prints an absolute path\n\nFalls back to the running interpreter when the preferred one is absent (pc, a fresh\nhost), so a rendered command always names something that exists. AGENT_CONTEXT_PYTHON\noverrides all of it, for tests and for a host that needs another build.\n\nImporters: home-settings-sync.py and harness-materialize.py render commands with\ninterpreter(); home-settings-sync.py and hook-registration-probe.py read them back with\nis_interpreter()."
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOMEBREW = "/opt/homebrew/bin/python3.14"

LAUNCHER = hp.HOOK_LAUNCHER


def interpreter(platform=None, home=None, env=None, exists=None, executable=None):
    'Absolute path of the interpreter to render into a command on this host.'
    env = os.environ if env is None else env
    override = env.get("AGENT_CONTEXT_PYTHON") or ""
    if override:
        return override
    platform = sys.platform if platform is None else platform
    home = os.path.expanduser("~") if home is None else home
    runnable = exists or (lambda path: os.access(path, os.X_OK))
    if platform == "darwin":
        preferred = HOMEBREW
    elif platform == "win32":
        
        preferred = hp.command_path(os.path.join(home, ".local", "bin", "python3.14.exe"))
    else:
        preferred = os.path.join(home, ".local", "bin", "python3.14")
    if runnable(preferred):
        return preferred
    return executable or sys.executable


is_interpreter = hp.is_interpreter


def main(argv):
    if any(arg in ("-h", "--help") for arg in argv[1:]):
        print((__doc__ or "").strip())
        return 0
    print(interpreter())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
