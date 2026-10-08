#!/usr/bin/env python3
'Assert the store-owned wiring in a project\'s .agents/claude/settings.json.\n\n    a project\'s settings wire exactly the hooks its own materialized files\n    justify -- wt-seed.sh, wt-sweep.sh and post-edit-format.sh each appear in\n    settings if and only if the project actually has that file.\n\nSo the managed block is COMPUTED from what is on disk. That makes the file\nreconstructible after a wipe (the point of this observation) without inventing a\nuniformity the projects deliberately do not have, and it means adding a\nworktree script to a project wires it automatically instead of silently doing\nnothing.\n\nWHAT IS NOT TOUCHED. `enabledPlugins` belongs to lsp-plugin-guard.py, which runs\nearlier in project-materialize and disables the harness\'s own LSP plugin per\nstack; two writers for one key is how a correct value gets clobbered. Any other\nkey, and any hook this script did not write, is preserved untouched -- the same\n"we wrote it, so we retire it" ownership contract home-settings-sync uses for\nthe home layer, and for the same reason: a machine-local or hand-added entry is\nnot ours to delete.\n\nsettings.local.json is deliberately NOT managed. No project has one, and the\nharness treats it as the per-machine override file -- exactly the role\n~/.claude/settings.local.json plays at the home layer, which home-settings-sync\nalso leaves alone.\n\nPROJECT SCRIPTS ARE MOVING FROM .sh TO .py (Consolidation Phase 4, project scope).\nwt-seed and post-edit-format each get wired from whichever a project has: its .py\nfile when present, else its .sh file, so BOTH ARE WIRED FOR ONE CYCLE -- a session\nstill open on the old .sh layout keeps working, and a landed .py file always wins\nover an older .sh one for that hook. wt-sweep is different: it never has a\nproject-local .py, because wt-sweep.py is a HOST-global script every machine\nalready has at ~/.agent-context/global/scripts/wt-sweep.py. Its wiring stays keyed on\n.agents/scripts/wt-sweep.sh exactly as before -- the shim is kept on disk as the\nmarker, not deleted -- but both hook sites now exec the global script directly\ninstead of the shim.\n\nUsage: project-settings-sync.py <project-dir>\nIdempotent. Writes only on a real change. Never fails a SessionStart.'
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

SETTINGS_REL = os.path.join(".agents", "claude", "settings.json")






def _interpreter():
    "This host's absolute Python (agent-python.py). A bare python3 resolves on the\n    caller's PATH, which is 3.8 on the Synology nodes (invariant interpreter-is-rendered).\n    The settings file is written per machine, so rendering a host path into it is safe."
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-python.py")
    spec = importlib.util.spec_from_file_location("agent_python", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.interpreter()


MATERIALIZE_CMD = ('bash -c \'%s "$HOME/.agent-context/global/scripts/'
                   'project-materialize.py" "$CLAUDE_PROJECT_DIR" || true\'' % _interpreter())


def _wt_cmd(script):
    return ('bash -c \'f="$CLAUDE_PROJECT_DIR/.agents/scripts/%s"; '
            '[ -f "$f" ] && bash "$f" "$CLAUDE_PROJECT_DIR" || true\'' % script)


def _wt_cmd_py(script):
    return ('bash -c \'f="$CLAUDE_PROJECT_DIR/.agents/scripts/%s"; '
            '[ -f "$f" ] && %s "$f" "$CLAUDE_PROJECT_DIR" || true\'' % (script, _interpreter()))


def _wt_sweep_cmd():
    'wt-sweep runs the HOST-global script directly; no project ever carries wt-sweep.py.'
    return ('bash -c \'%s "$HOME/.agent-context/global/scripts/wt-sweep.py" "$CLAUDE_PROJECT_DIR" || true\''
            % _interpreter())











OWNED_MARKERS = ("global/scripts/project-materialize.", "wt-seed", "wt-sweep",
                 "post-edit-format", "wt-loop-guard")


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def has(project, *rel):
    return os.path.isfile(os.path.join(project, *rel))


def managed_hooks(project):
    "The hook groups this project's own files justify: {event: [group, ...]}."
    session, post = [], []
    session.append({"type": "command", "command": MATERIALIZE_CMD, "timeout": 15})
    if has(project, ".agents", "scripts", "wt-sweep.sh"):
        cmd = _wt_sweep_cmd()
        session.append({"type": "command", "command": cmd, "timeout": 15, "async": True})
        post.append({"matcher": "ExitWorktree", "hooks": [
            {"type": "command", "command": cmd, "timeout": 15}]})
    if has(project, ".agents", "scripts", "wt-seed.py"):
        post.insert(0, {"matcher": "EnterWorktree", "hooks": [
            {"type": "command", "command": _wt_cmd_py("wt-seed.py"), "timeout": 15}]})
    elif has(project, ".agents", "scripts", "wt-seed.sh"):
        post.insert(0, {"matcher": "EnterWorktree", "hooks": [
            {"type": "command", "command": _wt_cmd("wt-seed.sh"), "timeout": 15}]})
    if has(project, ".agents", "hooks", "post-edit-format.py"):
        
        
        
        post.insert(0, {"matcher": "Edit|Write", "hooks": [
            {"type": "command",
             "command": "%s \"$CLAUDE_PROJECT_DIR/.agents/hooks/post-edit-format.py\"" % _interpreter(),
             "timeout": 30}]})
    elif has(project, ".agents", "hooks", "post-edit-format.sh"):
        post.insert(0, {"matcher": "Edit|Write", "hooks": [
            {"type": "command",
             "command": "$CLAUDE_PROJECT_DIR/" + hp.AGENTS_DIRNAME + "/hooks/post-edit-format.sh",
             "timeout": 30}]})
    out = {"SessionStart": [{"hooks": session}]}
    if post:
        out["PostToolUse"] = post
    return out


def is_ours(entry):
    cmd = (entry or {}).get("command", "") or ""
    return any(m in cmd for m in OWNED_MARKERS)


def merge(existing, project):
    'Existing settings with our wiring asserted and everything else preserved.'
    out = dict(existing)
    out.setdefault("worktree", {})
    if isinstance(out["worktree"], dict):
        
        
        
        out["worktree"].setdefault("baseRef", "head")

    hooks = out.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    wanted = managed_hooks(project)

    for event in ("SessionStart", "PostToolUse"):
        groups = [dict(g) for g in (hooks.get(event) or []) if isinstance(g, dict)]
        
        
        survivors = []
        for g in groups:
            kept = [h for h in (g.get("hooks") or []) if not is_ours(h)]
            if kept:
                g["hooks"] = kept
                survivors.append(g)
        ours = wanted.get(event) or []
        if not ours and not survivors:
            hooks.pop(event, None)
            continue
        
        hooks[event] = [dict(g) for g in ours] + survivors
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def main():
    args = sys.argv[1:]
    
    
    
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        print("project-settings-sync: unknown flag %s" % unknown[0], file=sys.stderr)
        return 2
    if len(sys.argv) != 2:
        print("usage: project-settings-sync.py <project-dir>", file=sys.stderr)
        return 2
    project = os.path.abspath(sys.argv[1])
    if not os.path.isdir(os.path.join(project, ".agents")):
        return 0  

    path = os.path.join(project, SETTINGS_REL)
    existing = load(path)
    merged = merge(existing, project)
    if merged == existing and os.path.exists(path):
        return 0

    created = not os.path.exists(path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".pss-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        print("project-settings-sync: cannot write %s: %s" % (path, exc),
              file=sys.stderr)
        return 0
    print("project-settings-sync: settings.json %s"
          % ("CREATED (was missing)" if created else "updated"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                  
        
        
        print("project-settings-sync: skipped (%s)" % exc, file=sys.stderr)
        sys.exit(0)
