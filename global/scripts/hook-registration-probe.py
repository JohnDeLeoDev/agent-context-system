#!/usr/bin/env python3
"hook-registration-probe — catch a guard that exists everywhere except where it runs.\n\nThe failure is total (the guard does not exist at runtime) and the evidence\nall points the other way (the file is right there). A guard that is not registered is\nworse than one that was never written, because everyone believes it is enforcing\nsomething.\n\nWHY IT CHECKS settings.json AND NOT THE MANAGED TABLE. Comparing the store's hooks\nagainst MANAGED would only catch hooks nobody wired. Comparing against the settings\nfile that Claude Code actually reads ALSO catches a hook that is in MANAGED and was\ndropped, clobbered by a machine-local edit, or lost to a merge -- the end state is\nwhat matters, and the end state is the only thing worth asserting.\n\nCheap enough to run inline in the SessionStart hook: two directory listings and one\nsmall JSON parse, no subprocesses.\n\nFINGERPRINTS. `global/hook-fingerprints.json` maps each store hook to a sha256 of its\nscript bytes plus the event and matcher home-settings-sync wires it under. A hook whose\nbody or wiring changed without a matching update to that file fails the invariant\n`hook-fingerprint-matches`, so an edit to a guard is always a deliberate, reviewed one.\nOnly `--update-fingerprints` writes the file, and it refuses while the registration\ncheck above has failures: a fingerprint of a broken wiring would bless it.\n\nUsage:  hook-registration-probe.py                     # writes ~/.local/state/agent-context/health/hooks.json\n        hook-registration-probe.py --update-fingerprints   # rewrites global/hook-fingerprints.json\n                                                           # (a server task on the live store)"
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.path.expanduser("~")
STORE_HOOKS = os.path.join(HOME, ".agent-context", "global", "hooks")
SETTINGS = hp.settings_file(HOME)
MANIFEST = os.path.join(HOME, ".agent-context", "global", "hooks-manifest.json")
OUT = os.path.join(hp.state_dir(HOME), "health", "hooks.json")





EXEMPT = {
    "subagent-return-gate.py":
        "invoked by agent definitions, not by a settings.json lifecycle event",
    "opencode-approval.py":
        "invoked by the generated opencode guard plugin around the question tool "
        "(harness-materialize.py); Claude Code has no such tool",
}


def script_word(cmd):
    'The script a hook command runs, past a `bash` or interpreter prefix. A store body\n    is 0644 and so is wired as `bash <path>`, and a .py hook as `<interpreter> <path>`;\n    the first word alone would read as the interpreter.'
    words = (cmd or "").split()
    while len(words) > 1 and hp.is_interpreter(words[0]):
        words = words[1:]
    return words[0] if words else ""


def registered_commands(settings):
    out = set()
    for groups in (settings.get("hooks") or {}).values():
        for g in groups or []:
            for h in g.get("hooks") or []:
                cmd = h.get("command") or ""
                if cmd:
                    out.add(os.path.basename(script_word(cmd)))
    return out


_GLOBAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINGERPRINT_FILE = os.path.join(_GLOBAL, "hook-fingerprints.json")
_STORE_WRITE_TOKEN = "<STORE_WRITE_TOOLS>"
_LAST = {}


def settings_sync():
    'home-settings-sync loaded as a module, once: it owns both what is wired and what\n    this platform leaves out, so the probe reads each from there and keeps no copy.'
    global _SYNC
    if _SYNC is None:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home-settings-sync.py")
        spec = importlib.util.spec_from_file_location("home_settings_sync", path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load " + path)
        _SYNC = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SYNC)
    return _SYNC


_SYNC = None


def skipped_here():
    'Hooks home-settings-sync leaves out on this platform (iterm-tab-status on Windows).\n    They are unwired on purpose, and the reason is recorded where they are left out.'
    try:
        sync = settings_sync()
        return {f for f in getattr(sync, "WINDOWS_SKIPPED", {})
                if not sync.runs_here(os.path.join(sync.H, f))}
    except Exception:
        return set()


def wiring_by_hook():
    '{hook basename: sorted ["Event:matcher", ...]} read from home-settings-sync\'s\n    MANAGED table, the only list of what is wired. The store-write matcher is derived at\n    import time and falls back to a literal set when the derivation fails, so it is\n    hashed as a token: a failed derivation on one machine must not read as drift.'
    sync = settings_sync()
    wired = {}
    for event, matcher, commands in sync.MANAGED:
        m = _STORE_WRITE_TOKEN if matcher == sync.STORE_WRITE_TOOLS else (matcher or "")
        for command, _extra in commands:
            base = sync.command_basename(command)
            if base:
                wired.setdefault(base, set()).add("%s:%s" % (event, m))
    return {k: sorted(v) for k, v in wired.items()}


def fingerprint(body, wiring):
    'sha256 over the script bytes and its wiring. `wiring` is the sorted list from\n    wiring_by_hook, empty for a hook that is deliberately unwired.'
    h = hashlib.sha256()
    h.update(body)
    h.update(b"\0" + "\n".join(wiring).encode("utf-8"))
    return h.hexdigest()


def compute_fingerprints(hooks_dir, wired):
    out = {}
    for f in sorted(os.listdir(hooks_dir)):
        if not f.endswith((".sh", ".py")):
            continue
        with open(os.path.join(hooks_dir, f), "rb") as fh:
            out[f] = fingerprint(fh.read(), wired.get(f, []))
    return out


def load_fingerprints(path=FINGERPRINT_FILE):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def update_fingerprints():
    'Rewrite the manifest. Refuses, and writes nothing, while the registration check\n    has failures.'
    main()
    failures = _LAST.get("report", {}).get("failures", [])
    if failures or not _LAST:
        for msg in failures or ["the registration check did not run"]:
            print("refusing to fingerprint: %s" % msg, file=sys.stderr)
        return 1
    fresh = compute_fingerprints(os.path.join(_GLOBAL, "hooks"), wiring_by_hook())
    tmp = FINGERPRINT_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(fresh, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, FINGERPRINT_FILE)
    print("wrote %d fingerprints to %s" % (len(fresh), FINGERPRINT_FILE))
    return 0


def main():
    report = {"ts": int(time.time()), "component": "hook registration",
              "ok": True, "failures": []}

    try:
        with open(SETTINGS) as fh:
            settings = json.load(fh)
    except (OSError, ValueError) as exc:
        report["ok"] = False
        report["failures"].append(
            "cannot read %s (%s) -- whether ANY guard is registered is unknown."
            % (SETTINGS, exc))
        return write(report)

    
    
    
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hook_registry", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-registry.py"))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load hook-registry.py")
    hook_registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook_registry)
    settings = dict(settings, hooks=hook_registry.effective_hooks(SETTINGS, settings))
    live = registered_commands(settings)

    
    
    try:
        with open(MANIFEST) as fh:
            manifest = json.load(fh)
        manifest_hooks = {os.path.basename(h.get("script", ""))
                          for h in (manifest.get("hooks") or {}).values()
                          if isinstance(h, dict) and h.get("script")}
    except (OSError, ValueError):
        manifest_hooks = set()

    try:
        on_disk = [f for f in sorted(os.listdir(STORE_HOOKS))
                   if f.endswith((".sh", ".py")) and not f.endswith(".meta.toml")]
    except OSError as exc:
        report["ok"] = False
        report["failures"].append("cannot list %s (%s)." % (STORE_HOOKS, exc))
        return write(report)

    skipped = skipped_here()
    orphans = [f for f in on_disk if f not in live and f not in EXEMPT and f not in skipped]
    if orphans:
        report["ok"] = False
        for f in orphans:
            where = (" It IS declared in hooks-manifest.json for other harnesses, "
                     "which makes the gap easy to miss." if f in manifest_hooks else "")
            report["failures"].append(
                "%s exists in the store and is in global/hooks, but is "
                "registered in NO settings.json lifecycle event -- it has never run "
                "and never will.%s Wire it into MANAGED in home-settings-sync.py, or "
                "add it to this probe's EXEMPT map with a reason." % (f, where))

    
    
    
    disk_set = set(on_disk)
    hookdir = hp.hooks_dir(HOME)
    for groups in (settings.get("hooks") or {}).values():
        for g in groups or []:
            for h in g.get("hooks") or []:
                cmd = script_word(h.get("command") or "")
                if not cmd or not cmd.startswith(hookdir):
                    continue
                base = os.path.basename(cmd)
                if not os.path.exists(cmd) and base not in disk_set:
                    report["ok"] = False
                    report["failures"].append(
                        "settings.json registers %s but that file does not exist -- "
                        "the harness will skip it silently and the guard is absent."
                        % cmd)

    return write(report)


def write(report):
    _LAST["report"] = report
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if report["ok"]:
        try:
            os.remove(OUT)
        except OSError:
            pass
        return 0
    tmp = OUT + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(report, fh, indent=2)
    os.replace(tmp, OUT)
    return 0


if __name__ == "__main__":
    if "--update-fingerprints" not in sys.argv[1:]:
        sys.exit(main())
    
    
    if os.environ.get("AGENT_CONTEXT_SERVER_TASK") == "1":
        sys.exit(update_fingerprints())
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import store_task
    store_task.main_or_forward("hook-registration-probe", update_fingerprints,
                               store=os.path.dirname(_GLOBAL))
