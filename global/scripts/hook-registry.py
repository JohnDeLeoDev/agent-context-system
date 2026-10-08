#!/usr/bin/env python3
'hook-registry: the hooks Claude Code runs, with dispatched events expanded.\n\nhook-dispatch.json has settings.json\'s hooks shape. An entry {"direct": "<script>"}\nholds the place of a hook that stays registered directly in settings.json (an async\nhook, home-materialize), so the expanded view keeps the original order.\n\nUsage: hook-registry.py [settings.json]    prints the expanded hooks map as JSON'
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

REGISTRY_NAME = "hook-dispatch.json"
DISPATCHER = "hook-dispatch.py"


def registry_path(settings_path=None):
    'The generated hook-dispatch.json in the store root. settings_path is accepted for\n    callers that still pass it and is ignored: the registry no longer sits beside settings.json.\n    $HOOK_DISPATCH_REGISTRY overrides the location, the same variable the dispatcher reads, so\n    a test that syncs a fixture settings file never touches the live registry.'
    return os.environ.get("HOOK_DISPATCH_REGISTRY") or hp.hook_registry_file()


def load_json(path):
    'The parsed file, or None when it is absent or unreadable.'
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def script_name(command):
    'Basename of the script a hook command runs, past any interpreter word.'
    words = (command or "").split()
    while len(words) > 1 and hp.is_interpreter(words[0]):
        words = words[1:]
    return os.path.basename(words[0]) if words else ""


def dispatched_event(command):
    'The event a hook-dispatch.py command serves; None for any other command.'
    words = (command or "").split()
    for at, word in enumerate(words):
        if os.path.basename(word) == DISPATCHER:
            following = words[at + 1] if at + 1 < len(words) else ""
            return "" if following.startswith("-") else following
    return None


def _guards(registry):
    hooks = registry.get("hooks") if isinstance(registry, dict) else None
    return hooks if isinstance(hooks, dict) else {}


def expand(hooks, registry):
    'A settings.json hooks map with each dispatcher entry replaced by its guards.'
    guards = _guards(registry)
    out = {}
    for event, groups in (hooks or {}).items():
        groups = [g for g in groups or [] if isinstance(g, dict)]
        served = {dispatched_event(h.get("command")) or event
                  for g in groups for h in g.get("hooks") or []
                  if isinstance(h, dict) and dispatched_event(h.get("command")) is not None}
        held = {h["direct"] for name in served for g in guards.get(name) or []
                for h in g.get("hooks") or [] if isinstance(h, dict) and "direct" in h}
        direct = {}
        for g in groups:
            for h in g.get("hooks") or []:
                name = script_name(h.get("command")) if isinstance(h, dict) else ""
                if name in held and name not in direct:
                    direct[name] = h
        result, expanded = [], set()
        for g in groups:
            kept = []
            for h in g.get("hooks") or []:
                if not isinstance(h, dict):
                    continue
                serves = dispatched_event(h.get("command"))
                if serves is None:
                    if direct.get(script_name(h.get("command"))) is not h:
                        kept.append(h)
                    continue
                
                
                if (serves or event) in expanded:
                    continue
                expanded.add(serves or event)
                if kept:
                    result.append(dict(g, hooks=kept))
                    kept = []
                for rg in guards.get(serves or event) or []:
                    entries = []
                    for rh in rg.get("hooks") or []:
                        if not isinstance(rh, dict):
                            continue
                        if "direct" in rh:
                            if rh["direct"] in direct:
                                entries.append(direct[rh["direct"]])
                        else:
                            entries.append(rh)
                    if entries:
                        result.append(dict(rg, hooks=entries))
            if kept:
                result.append(dict(g, hooks=kept))
        out[event] = result
    return out


def effective_hooks(settings_path, settings=None):
    'The expanded hooks map for a settings file; {} when it cannot be read.\n\n    Pass `settings` when the caller has already parsed the file, for instance to fail\n    loudly on a malformed one; the registry is still read from beside settings_path.'
    if settings is None:
        settings = load_json(settings_path)
    if not isinstance(settings, dict):
        return {}
    return expand(settings.get("hooks"), load_json(registry_path(settings_path)))


def main(argv):
    if any(arg in ("-h", "--help") for arg in argv[1:]):
        print((__doc__ or "").strip())
        return 0
    path = argv[1] if len(argv) > 1 else hp.settings_file()
    print(json.dumps(effective_hooks(path), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
