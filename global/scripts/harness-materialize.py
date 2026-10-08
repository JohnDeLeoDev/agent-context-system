#!/usr/bin/env python3
"Materialize canonical agent-context config into every installed coding harness.\n\nGives opencode, Copilot CLI, antigravity, and pi the same core capabilities as\nClaude Code — reading everything from the single source of truth (~/.agent-context):\n\nNon-destructive (preserves unmanaged keys/servers/dirs), idempotent, $HOME-portable.\nA harness is only touched if it's installed (its config dir exists).\n\nRun standalone (`python3 harness-materialize.py`) or from home-materialize.py at\nClaude SessionStart.\n\nObservations guarded: #219, #256, #261."
import json
import os
import starter_profile
import re
import hashlib
import select
import shlex
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
from frontmatter_strip import strip_frontmatter_tree
import tree_copy

HOME = os.path.expanduser("~")
STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(HOME, ".agent-context")
MANIFEST = os.path.join(STORE, "global", "mcp-servers.json")
HOOK_MANIFEST = os.path.join(STORE, "global", "hooks-manifest.json")
ORCA_MARK = "/.orca/"  
AGENTS_MD = os.path.join(STORE, "AGENTS.md")
SHARED_SKILLS = os.path.join(STORE, "shared-skills")
STORE_SKILLS = os.path.join(STORE, "global", "skills")
STORE_COMMANDS = os.path.join(STORE, "global", "commands")
SHARED_COMMANDS = os.path.join(STORE, "shared-commands")
XCODE_CA = os.path.join(HOME, "Library", "Developer", "Xcode", "CodingAssistant")
XCODE_CLAUDE = os.path.join(XCODE_CA, "ClaudeAgentConfig")

DEPRECATED = {"agent-db"}  


def agent_python():
    "The interpreter a rendered harness command names, from agent-python.py: Homebrew or\n    uv 3.14, never a bare `python3` whose meaning depends on the caller's PATH."
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-python.py")
    spec = importlib.util.spec_from_file_location("agent_python", path)
    if spec is None or spec.loader is None:
        return sys.executable
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.interpreter()


def hook_client(home=None):
    "This home's current hook-client build (policy), from hook-client-build.py's\n    installed(), or None. A dispatcher or guard launched through it runs in the warm hook\n    server; without it the command starts Python itself."
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-client-build.py")
    spec = importlib.util.spec_from_file_location("hook_client_build", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.installed(home)


GUARD_DISPATCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-dispatch.py")


def guard_command(script):
    "The command that runs one store guard for a harness that lists guards one by one:\n    through hook-client and the dispatcher's --guard mode when this host has a build, so\n    the guard runs warm and a JSON deny comes back as exit 2 with its reason, else the\n    guard itself under this host's interpreter."
    client = hook_client()
    if client:
        return shlex.join([client, agent_python(), GUARD_DISPATCHER, "PreToolUse", "--guard",
                           script])
    return f"{agent_python()} {script}"












FINDINGS = []
HEALTH_RECORD = os.path.join(hp.scripts_dir(HOME), "health-record.py")


STAMP = os.path.join(hp.state_dir(HOME), "health", "materialize-stamp.json")


def progress(msg):
    'One short line per harness/project step: this runs from a\n    SessionStart hook, so it stays silent by default and only prints on a real\n    TTY or with HARNESS_MATERIALIZE_PROGRESS=1, so session-start output does not\n    grow for the normal case.'
    if sys.stderr.isatty() or os.environ.get("HARNESS_MATERIALIZE_PROGRESS") == "1":
        print("harness-materialize: " + msg, file=sys.stderr, flush=True)


def finding(text):
    'Record a projection problem for preflight, as well as printing it.\n\n    Deliberately not called for a self-repair that succeeded. A replaced broken\n    symlink is said on the row; recording it as a health failure\n    would put a permanent finding in the banner for something already fixed, and a\n    banner that cries wolf is the one people stop reading.'
    FINDINGS.append(text)


def record_health():
    "Publish this run's findings, or clear a previous run's.\n\n    Clearing on success is not optional: without it the first bad run haunts every\n    later one until someone deletes the state file by hand, and a stale failure is its\n    own silent lie."
    if not os.path.exists(HEALTH_RECORD):
        return
    args = ([HEALTH_RECORD, "harness-materialize", "--fail",
             "; ".join(FINDINGS)] if FINDINGS
            else [HEALTH_RECORD, "harness-materialize", "--ok"])
    try:
        subprocess.run([sys.executable] + args, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        
        pass


def write_stamp():
    'Record when this machine last projected, and from which store commit.\n\n    Written unconditionally, including on a run that produced findings: the question\n    this answers is "when did projection last RUN here", which is separate from\n    "did it find anything". The fleet reader needs both and they fail differently.'
    stamp = {"at": int(time.time()), "commit": store_head(), "findings": len(FINDINGS)}
    try:
        os.makedirs(os.path.dirname(STAMP), exist_ok=True)
        tmp = STAMP + ".tmp"
        with open(tmp, "w") as f:
            json.dump(stamp, f)
        os.replace(tmp, STAMP)
    except OSError:
        
        pass


def store_head():
    'The store commit this projection was made from, or None.\n\n    Names which content was projected, not just when. A machine that projected an\n    hour ago from a commit fifty behind is a different problem from one that has not\n    projected in a week, and the timestamp alone cannot tell them apart.'
    try:
        r = subprocess.run(["git", "-C", STORE, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None



def load_json(path, default):
    try:
        with open(path) as f:
            txt = f.read().strip()
        value = json.loads(txt) if txt else default
        return starter_profile.manifest(value) if os.path.basename(path) == 'hooks-manifest.json' else value
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, ValueError):
        return default


def write_text_atomic(path, text):
    'Replace `path` with `text` so a reader never sees a partial file.\n\n    Every file this script projects is read or executed by another process that\n    can start at any moment: pi loads the extensions on session start, opencode\n    loads the guard plugin before every tool call, both read the agent\n    definitions when they spawn a worker. `open(path, "w")` truncates the file\n    before the first byte lands, so a session starting in that window loads an\n    empty or half-written module: a hook file caught mid-write makes every Bash\n    call fail with "unexpected EOF while looking for matching `\'`". Temp file in the\n    same directory\n    (os.replace is atomic only within a filesystem), removed if the write dies so\n    nothing is left for a later `ls` or loader to trip over.'
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".hm-tmp-" + os.path.basename(path))
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, data):
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")


_PYTHON = []


def relay_command():
    "The agent-context relay this machine should launch: the standalone install\n    (~/.local/bin/agent-context, from agent-context-relay-install) when it is there and\n    executable, else the clone's venv script. Looked up at each call, so a machine that gains\n    the install switches on the next render.\n\n    On Windows the installer makes no ~/.local/bin link (an unelevated session cannot), so\n    the command is the release's own launcher through the `current` junction."
    if os.name == "nt":
        return hp.command_path(os.path.join(HOME, ".local", "share", "agent-context", "relay",
                                            "current", "Scripts", "agent-context.exe"))
    installed = os.path.join(HOME, ".local", "bin", "agent-context")
    if os.path.isfile(installed) and os.access(installed, os.X_OK):
        return installed
    return os.path.join(HOME, ".agent-context", "server", ".venv", "bin", "agent-context")


def subst(v):
    "Expand {HOME}, {PYTHON} and {RELAY} in a command string or an argv list.\n\n    {RELAY} is relay_command(): the agent-context server entry, which lives in different\n    places on a machine with a clone and on one without.\n\n    The command needs this as much as the args do: a server launched from a\n    per-user path (a venv console script) cannot be written absolute here --\n    the manifest is git-synced to machines whose home directories differ.\n    {PYTHON} is agent_python(), so a store script served over MCP (`lspd.py --mcp`)\n    never names a bare python3 whose meaning depends on the harness's PATH\n    (invariant interpreter-is-rendered)."
    def one(s):
        s = s.replace("{HOME}", HOME)
        if "{RELAY}" in s:
            s = s.replace("{RELAY}", relay_command())
        if "{PYTHON}" in s:
            if not _PYTHON:
                _PYTHON.append(agent_python())
            s = s.replace("{PYTHON}", _PYTHON[0])
        return s

    if isinstance(v, str):
        return one(v)
    return [one(a) for a in v]


def ensure_symlink(link, target, notes):
    'Point `link` at `target`. Back up a real (non-symlink) file first.'
    if not os.path.isdir(os.path.dirname(link)):
        return
    try:
        if os.path.islink(link):
            if os.readlink(link) == target:
                return
            os.unlink(link)
        elif os.path.exists(link):
            shutil.move(link, link + ".pre-harness-materialize.bak")
        os.symlink(target, link)
        notes.append(f"link {os.path.relpath(link, HOME)}")
    except OSError as e:
        notes.append(f"link-FAIL {os.path.relpath(link, HOME)}: {e}")



def wanted(manifest, harness):
    return {n: s for n, s in manifest["servers"].items()
            if harness in s.get("harnesses", [])}


def scoped_paths(spec):
    "Absolute paths for a server's optional `projects` list ($HOME-relative),\n    keeping only the ones that exist on this machine."
    out = []
    for rel in spec.get("projects") or []:
        p = os.path.join(HOME, rel)
        if os.path.isdir(p):
            out.append(p)
    return out


def render_claude(spec, existing):
    if spec["transport"] == "stdio":
        e = dict(existing) if isinstance(existing, dict) else {}
        e["command"] = subst(spec["command"]); e["args"] = subst(spec["args"]); e["disabled"] = False
        if spec.get("env"):
            e["env"] = dict(spec["env"])
        e.pop("type", None); e.pop("url", None)
        return e  
    e = dict(existing) if isinstance(existing, dict) else {}
    e["type"] = "http"; e["url"] = spec["url"]
    if "oauthCallbackPort" in spec:
        e.setdefault("oauth", {})["callbackPort"] = spec["oauthCallbackPort"]
    elif isinstance(e.get("oauth"), dict):
        
        
        
        e["oauth"].pop("callbackPort", None)
        if not e["oauth"]:
            e.pop("oauth")
    e.pop("command", None); e.pop("args", None)
    return e


def render_copilot(spec, existing):
    if spec["transport"] == "stdio":
        return {"command": subst(spec["command"]), "args": subst(spec["args"])}
    return {"type": "http", "url": spec["url"]}


def render_antigravity(spec, existing):
    if spec["transport"] == "stdio":
        return {"command": subst(spec["command"]), "args": subst(spec["args"])}
    return None  


MCP_TARGETS = {
    "claude":      (hp.claude_json(HOME), "mcpServers", render_claude,
                    lambda: os.path.exists(hp.claude_json(HOME))),








    "copilot":     (os.path.join(HOME, ".copilot", "mcp-config.json"), "mcpServers", render_copilot,
                    lambda: os.path.isdir(os.path.join(HOME, ".copilot"))),
    "claude-desktop": (os.path.join(HOME, "Library", "Application Support", "Claude", "claude_desktop_config.json"),
                    "mcpServers", render_copilot,
                    lambda: os.path.isdir(os.path.join(HOME, "Library", "Application Support", "Claude"))),
    "antigravity": (os.path.join(HOME, ".gemini", "config", "mcp_config.json"), "mcpServers", render_antigravity,
                    lambda: os.path.isdir(os.path.join(HOME, ".gemini", "config"))),
}















OWNED_STATE = os.path.join(hp.state_dir(HOME), "harness-materialize-owned.json")


def _load_owned():
    d = load_json(OWNED_STATE, {})
    return d if isinstance(d, dict) else {}


def _save_owned(owned):
    try:
        os.makedirs(os.path.dirname(OWNED_STATE), exist_ok=True)
        write_json(OWNED_STATE, owned)
    except OSError:
        pass          


def materialize_mcp(manifest, report):
    owned = _load_owned()
    for hname, (path, key, render, guard) in MCP_TARGETS.items():
        if not guard():
            continue
        cfg = load_json(path, {})
        if not isinstance(cfg, dict):
            cfg = {}
        servers = cfg.get(key)
        if not isinstance(servers, dict):
            servers = {}
        added, updated, pruned = [], [], []
        for dep in list(servers):
            if dep in DEPRECATED:
                del servers[dep]; pruned.append(dep)
        scoped = []
        now_owned: dict[str, set] = {}
        for name, spec in wanted(manifest, hname).items():
            entry = render(spec, servers.get(name))
            if entry is None:
                continue
            if spec.get("projects"):
                
                
                
                if name in servers:
                    del servers[name]; pruned.append(name)
                if hname != "claude":
                    continue
                for proj in scoped_paths(spec):
                    slot = cfg.setdefault("projects", {}).setdefault(proj, {})
                    pservers = slot.get("mcpServers")
                    if not isinstance(pservers, dict):
                        pservers = {}
                    if pservers.get(name) != entry:
                        pservers[name] = entry
                        scoped.append(f"{name}@{os.path.relpath(proj, HOME)}")
                    slot["mcpServers"] = pservers
                    now_owned.setdefault(proj, set()).add(name)
                continue
            if name not in servers:
                added.append(name)
            elif servers[name] != entry:
                updated.append(name)
            servers[name] = entry

        
        
        
        
        prev = owned.get(hname) or {}
        for proj, names in prev.items():
            keep = now_owned.get(proj, set())
            gone = [n for n in names if n not in keep]
            if not gone:
                continue
            slot = (cfg.get("projects") or {}).get(proj)
            pservers = slot.get("mcpServers") if isinstance(slot, dict) else None
            if not isinstance(pservers, dict):
                continue
            for n in gone:
                if n in pservers:
                    del pservers[n]
                    pruned.append(f"{n}@{os.path.relpath(proj, HOME)}")
        owned[hname] = {p: sorted(n) for p, n in now_owned.items() if n}

        cfg[key] = servers
        write_json(path, cfg)
        parts = []
        if added:   parts.append("add " + ",".join(added))
        if updated: parts.append("upd " + ",".join(updated))
        if pruned:  parts.append("prune " + ",".join(pruned))
        if scoped:  parts.append("scope " + ",".join(scoped))
        report.setdefault(hname, []).append("mcp: " + ("; ".join(parts) if parts else "in sync"))
    _save_owned(owned)


def render_opencode(spec, existing):
    "opencode's own MCP shape. `command` is one argv list, not command+args."
    if spec["transport"] == "stdio":
        e = dict(existing) if isinstance(existing, dict) else {}
        e["type"] = "local"
        args = subst(spec["args"] or [])
        e["command"] = [subst(spec["command"])] + (args if isinstance(args, list) else [args])
        e["enabled"] = True
        if spec.get("env"):
            e["environment"] = dict(spec["env"])
        e.pop("url", None)
        return e
    e = dict(existing) if isinstance(existing, dict) else {}
    e["type"] = "remote"; e["url"] = spec["url"]; e["enabled"] = True
    e.pop("command", None); e.pop("environment", None)
    return e


def git_exclude(proj, name, notes):
    'Keep a generated file out of the repo via .git/info/exclude, never .gitignore.'
    info = os.path.join(proj, ".git", "info")
    if not os.path.isdir(info):
        return  
    path = os.path.join(info, "exclude")
    try:
        body = open(path).read() if os.path.exists(path) else ""
        if any(line.strip() == name for line in body.splitlines()):
            return
        
        
        with open(path, "a") as f:
            if body and not body.endswith("\n"):
                f.write("\n")
            f.write(name + "\n")
        notes.append(f"exclude {name}@{os.path.relpath(proj, HOME)}")
    except OSError as e:
        notes.append(f"exclude-FAIL {os.path.relpath(proj, HOME)}: {e}")


def materialize_opencode_projects(manifest, report):
    "Project-scoped MCP servers for opencode, written into each repo's own\n    `opencode.json` rather than the global one.\n\n    Why this exists at all: opencode reads `~/.config/opencode/opencode.json` in\n    every directory, so a scoped server cannot go there — that is the whole point\n    of the manifest's `projects` key. But unlike Claude, opencode has no\n    `projects.<path>` section in its global config; its per-directory config is a\n    file in the repo. So the scoped servers land there, and `.git/info/exclude`\n    keeps them out of the user's repo.\n\n    Why it is a separate function from materialize_mcp(): that loop is keyed on\n    MCP_TARGETS, and opencode is deliberately absent from it because the global\n    opencode.json is chezmoi-owned end to end (see the comment there). Nothing\n    here touches that file — only per-repo ones, which chezmoi does not manage."
    if not os.path.isdir(os.path.join(HOME, ".config", "opencode")):
        return
    
    
    want = {}   
    for name, spec in wanted(manifest, "opencode").items():
        if not spec.get("projects"):
            continue  
        for proj in scoped_paths(spec):
            want.setdefault(proj, {})[name] = spec
    
    
    for spec in manifest["servers"].values():
        for proj in scoped_paths(spec):
            if os.path.exists(os.path.join(proj, "opencode.json")):
                want.setdefault(proj, {})

    managed = set(manifest["servers"])
    touched, excluded = [], []
    for proj, specs in want.items():
        path = os.path.join(proj, "opencode.json")
        cfg = load_json(path, {})
        if not isinstance(cfg, dict):
            cfg = {}
        servers = cfg.get("mcp")
        if not isinstance(servers, dict):
            servers = {}
        for name, spec in specs.items():
            entry = render_opencode(spec, servers.get(name))
            if servers.get(name) != entry:
                servers[name] = entry
                touched.append(f"{name}@{os.path.relpath(proj, HOME)}")
        
        
        
        for name in list(servers):
            if name in managed and name not in specs:
                del servers[name]
                touched.append(f"-{name}@{os.path.relpath(proj, HOME)}")
        if servers:
            cfg.setdefault("$schema", "https://opencode.ai/config.json")
            cfg["mcp"] = servers
            write_json(path, cfg)
            git_exclude(proj, "opencode.json", excluded)
        elif os.path.exists(path) and set(cfg) <= {"$schema", "mcp"}:
            
            
            os.remove(path)
            touched.append(f"-file@{os.path.relpath(proj, HOME)}")
        else:
            cfg["mcp"] = servers
            write_json(path, cfg)
    parts = []
    if touched:   parts.append("scope " + ",".join(touched))
    if excluded:  parts.append("; ".join(excluded))
    report.setdefault("opencode", []).append(
        "mcp(project): " + ("; ".join(parts) if parts else "in sync"))



def materialize_instructions(report):
    targets = {
        "opencode":    os.path.join(HOME, ".config", "opencode", "AGENTS.md"),
        "antigravity": os.path.join(HOME, ".gemini", "config", "AGENTS.md"),
        "copilot":     os.path.join(HOME, ".copilot", "copilot-instructions.md"),
        "codex":       os.path.join(HOME, ".codex", "AGENTS.md"),
    }
    for hname, link in targets.items():
        notes = []
        ensure_symlink(link, AGENTS_MD, notes)
        if notes:
            report.setdefault(hname, []).extend(notes)



def project_stripped(src, dst):
    "Project a store content directory into dst with the store frontmatter stripped.\n\n    The strip runs in a staging copy that keeps each file's mtime, so a second run\n    changes nothing, and a file removed from the store is pruned from dst.\n    Returns (ok, error text)."
    import tempfile
    stage = tempfile.mkdtemp(prefix="harness-mat.")
    try:
        ok, err = tree_copy.copy_tree(src, stage)
        if not ok:
            return False, err
        strip_frontmatter_tree([stage])
        if os.path.islink(dst):
            os.unlink(dst)  
        os.makedirs(dst, exist_ok=True)
        return tree_copy.copy_tree(stage, dst, delete=True, excludes=())
    except OSError as e:
        return False, str(e)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def has_entries(path):
    'True when the directory holds anything besides .DS_Store.'
    return os.path.isdir(path) and any(n != ".DS_Store" for n in os.listdir(path))


def mirror_shared_skills(report):
    "Project the store's global/skills into the neutral shared-skills/.\n\n    The store is the only source: nothing here reads ~/.claude/skills."
    if not has_entries(STORE_SKILLS):
        report.setdefault("_shared", []).append("skills: store global/skills is empty or missing")
        finding("the shared-skills mirror is not being written: the store has no "
                "global/skills, so every non-Claude harness is serving whatever "
                "skill set it already had")
        return False
    progress("skills: mirroring shared-skills...")
    t0 = time.time()
    ok, err = project_stripped(STORE_SKILLS, SHARED_SKILLS)
    progress("skills: mirror ok=%s in %ds" % (ok, time.time() - t0))
    if ok:
        report.setdefault("_shared", []).append(
            f"skills: mirrored {len(os.listdir(SHARED_SKILLS))} skill(s) -> shared-skills")
        return True
    report.setdefault("_shared", []).append(f"skills: rsync FAILED: {err}")
    finding(f"the shared-skills mirror is not being written: rsync failed "
            f"({err[:200]}), so every non-Claude harness is serving "
            f"whatever skill set it already had")
    return False


def set_skill_dir_json(path, key, value_list, report, hname):
    cfg = load_json(path, {})
    if not isinstance(cfg, dict):
        cfg = {}
    if cfg.get(key) != value_list:
        cfg[key] = value_list
        write_json(path, cfg)
        report.setdefault(hname, []).append(f"skills: set {key}")


def materialize_skills(report):
    if not mirror_shared_skills(report):
        return
    
    for hname, link in {
        "opencode":    os.path.join(HOME, ".config", "opencode", "skills"),
        "antigravity": os.path.join(HOME, ".gemini", "config", "skills"),
    }.items():
        notes = []
        ensure_symlink(link, SHARED_SKILLS, notes)
        if notes:
            report.setdefault(hname, []).extend(notes)
    
    cp = os.path.join(HOME, ".copilot", "settings.json")
    if os.path.isdir(os.path.dirname(cp)):
        set_skill_dir_json(cp, "skillDirectories", [SHARED_SKILLS, hp.CLAUDE_DIRNAME + "/skills"], report, "copilot")



















OC_CMD_KEYS = ("description", "agent", "model", "subtask")
CMD_MANIFEST = ".agent-context-commands.json"


def split_frontmatter(text):
    "Return (dict-of-scalar-keys, body). Non-frontmatter input -> ({}, text).\n\n    Deliberately not YAML: these files are generated with one `key: value` per\n    line and pulling in PyYAML would make a SessionStart hook depend on a package\n    that is not in every machine's system python."
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 3)
    if end == -1:
        return {}, text
    fm = {}
    for line in text[4:end].splitlines():
        k, sep, v = line.partition(":")
        if sep and not k.startswith((" ", "\t", "#")):
            fm[k.strip()] = v.strip()
    return fm, text[end + 5:]


def render_opencode_command(text):
    "Re-emit a Claude/store command as one opencode understands, or None to skip.\n\n    opencode requires a description (it is what the `/` picker shows) but Claude\n    does not: the store's `init-project` reaches .claude/commands with no\n    frontmatter at all, because Claude just reads the H1. Dropping such a command\n    would reintroduce the bug this whole function exists to fix, one command at a\n    time and silently, so the heading is used as the description instead."
    fm, body = split_frontmatter(text)
    desc = fm.get("description", "").strip().strip('"').strip("'")
    if not desc:
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("# "):
                desc = line[2:].strip()
                break
            if line and not line.startswith(("```", "---")):
                desc = line
                break
    if not desc:
        return None
    desc = desc[:1024]
    
    
    
    
    out = ["---", "description: " + json.dumps(desc, ensure_ascii=False)]
    for k in OC_CMD_KEYS[1:]:
        v = fm.get(k)
        
        
        if v and (k != "model" or "/" in v):
            out.append(f"{k}: {v}")
    out += ["---", ""]
    return "\n".join(out) + body.lstrip("\n")


def project_commands(src, dst, label, report, hname="opencode"):
    "Copy .md commands src -> dst, pruning ours and never clobbering anyone else's.\n\n    Ownership is a manifest of the names we wrote, not a heuristic. Without it the\n    prune pass would have to guess, and the global destination is shared with the\n    caveman plugin's commands -- deleting or overwriting one of those because it\n    happens to sit in our output directory is exactly the failure to avoid."
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    mpath = os.path.join(dst, CMD_MANIFEST)
    owned = load_json(mpath, [])
    if not isinstance(owned, list):
        owned = []
    wrote, skipped, pruned = [], [], []
    now = []
    for fn in sorted(os.listdir(src)):
        if not fn.endswith(".md"):
            continue
        target = os.path.join(dst, fn)
        if os.path.exists(target) and fn not in owned:
            skipped.append(fn)   
            continue
        try:
            with open(os.path.join(src, fn)) as f:
                rendered = render_opencode_command(f.read())
        except OSError as e:
            skipped.append(f"{fn}({e.strerror})")
            continue
        if rendered is None:
            continue
        now.append(fn)
        if load_text(target) != rendered:
            write_text_atomic(target, rendered)
            wrote.append(fn)
    for fn in owned:
        if fn not in now and os.path.exists(os.path.join(dst, fn)):
            os.remove(os.path.join(dst, fn))
            pruned.append(fn)
    if now != owned:
        write_json(mpath, now)
    parts = []
    if wrote:   parts.append("upd " + ",".join(wrote))
    if pruned:  parts.append("prune " + ",".join(pruned))
    if skipped: parts.append("skip(not ours) " + ",".join(skipped))
    report.setdefault(hname, []).append(
        f"commands({label}): " + ("; ".join(parts) if parts else f"in sync ({len(now)})"))


def load_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def materialize_commands(report):
    "Global store commands -> ~/.config/opencode/commands.\n\n    Sourced from the store's own global/commands, not from the ~/.claude/commands\n    projection, so opencode does not inherit a second-hand copy that is only as\n    fresh as the last Claude materialize. The per-repo half is not here: this\n    script has no way to enumerate this machine's checkouts (the file store\n    resolves projects by an in-repo .agents/project-id marker, not a path\n    registry), so it runs from agents-materialize.py, which already holds the\n    repo root, via `--commands SRC DST`."
    dst = os.path.join(HOME, ".config", "opencode", "commands")
    if not os.path.isdir(os.path.dirname(dst)):
        return
    project_commands(os.path.join(STORE, "global", "commands"), dst, "global", report)



CODEX_OWNED = ".agent-context-owned.json"


def _codex_description(fm, body, fallback):
    desc = fm.get("description", "").strip().strip('"').strip("'")
    if not desc:
        desc = next((line.strip().lstrip("# ") for line in body.splitlines()
                     if line.strip() and not line.startswith("---")), fallback)
    return desc[:1024]


def _codex_native_source(text, kind):
    'Remove store metadata while retaining native discovery and agent fields.'
    fm, body = split_frontmatter(text)
    if "uuid" not in fm:
        return text
    body = body.lstrip("\n")
    if body.startswith("---\n"):
        return body
    fields = {
        "skill": ("name", "description", "allowed_tools"),
        "agent": ("name", "description", "tools", "model", "effort", "permission_mode"),
        "command": ("description",),
    }[kind]
    lines = ["---"]
    for key in fields:
        if key in fm:
            native = {"allowed_tools": "allowed-tools",
                      "permission_mode": "permissionMode"}.get(key, key)
            lines.append((native or key) + ": " + fm[key])
    lines += ["---", ""]
    return "\n".join(lines) + body


def _codex_skill(name, source):
    fm, body = split_frontmatter(source)
    desc = _codex_description(fm, body, name)
    return ("---\nname: " + name + "\ndescription: " +
            json.dumps(desc, ensure_ascii=False) + "\n---\n\n" + body.lstrip("\n"))


def _codex_agent(name, source):
    fm, body = split_frontmatter(source)
    desc = _codex_description(fm, body, name)
    tier = fm.get("model", "").strip().strip('"').strip("'").lower()
    models = {"opus": "gpt-5.6-sol", "sonnet": "gpt-5.6-terra",
              "haiku": "gpt-5.6-luna"}
    instructions = ("This agent is projected from the agent-context store. "
                    "Follow its task-specific instructions below. Tool names in the "
                    "source may be Claude names; use Codex equivalents.\n\n" + body.lstrip("\n"))
    model_line = ("model = " + json.dumps(models[tier]) + "\n") if tier in models else ""
    sandbox_line = ('sandbox_mode = "read-only"\n'
                    if name in ("worker-explore", "worker-review") else "")
    return ("name = " + json.dumps(name) + "\n" +
            "description = " + json.dumps(desc, ensure_ascii=False) + "\n" +
            model_line + sandbox_line +
            "developer_instructions = " + json.dumps(instructions, ensure_ascii=False) + "\n")


def _sync_codex_files(dst, desired, report, label):
    'Owned-file projection; never replace or prune a foreign file.'
    os.makedirs(dst, exist_ok=True)
    manifest = os.path.join(dst, CODEX_OWNED)
    owned = load_json(manifest, {})
    if not isinstance(owned, dict):
        owned = {}
    owned = {rel: digest for rel, digest in owned.items()
             if _codex_safe_relative(rel) and isinstance(digest, str)}
    next_owned = {}
    changed, skipped, retired = [], [], []
    for rel, content in sorted(desired.items()):
        if not _codex_safe_relative(rel):
            raise ValueError("unsafe Codex projection path: " + str(rel))
        target = os.path.join(dst, rel)
        if not _codex_target_safe(dst, target):
            raise ValueError("Codex projection escapes destination: " + rel)
        content = content.encode() if isinstance(content, str) else content
        current = _codex_read_bytes(target)
        if current is not None and rel not in owned:
            skipped.append(rel)
            continue
        if rel in owned and current is not None:
            digest = hashlib.sha256(current).hexdigest()
            if digest != owned[rel] and current != content:
                skipped.append(rel + "(modified)")
                continue
        if current != content:
            _codex_write_bytes(target, content)
            changed.append(rel)
        next_owned[rel] = hashlib.sha256(content).hexdigest()
    for rel, digest in owned.items():
        if rel in next_owned:
            continue
        target = os.path.join(dst, rel)
        if not _codex_target_safe(dst, target):
            continue
        current = _codex_read_bytes(target)
        if current is not None and hashlib.sha256(current).hexdigest() == digest:
            os.remove(target)
            retired.append(rel)
            parent = os.path.dirname(target)
            if parent != dst and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
    if owned != next_owned:
        write_json(manifest, next_owned)
    report.setdefault("codex", []).append(
        "%s: %d active; %d updated; %d retired; %d skipped" %
        (label, len(next_owned), len(changed), len(retired), len(skipped)))


def _codex_safe_relative(path):
    return (isinstance(path, str) and bool(path) and not os.path.isabs(path)
            and all(part not in ("", ".", "..") for part in path.split(os.sep)))


def _codex_target_safe(dst, target):
    base = os.path.realpath(dst)
    parent = os.path.realpath(os.path.dirname(target))
    return os.path.commonpath((base, parent)) == base


def _codex_read_bytes(path):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except OSError:
        return None


def _codex_write_bytes(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), ".hm-tmp-" + os.path.basename(path))
    try:
        with open(tmp, "wb") as stream:
            stream.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def materialize_codex_content(home, store, project, report):
    'Project global or per-repo Claude content into Codex-native locations.'
    if project is not None:
        materialize_codex_content(home, store, None, report)
    shared = os.path.join(store, "shared-skills")
    if project is None:
        src_skills = shared if os.path.isdir(shared) and os.listdir(shared) else \
            os.path.join(store, "global", "skills")
        src_commands = os.path.join(store, "global", "commands")
        src_agents = os.path.join(store, "global", "agents")
        skill_dst = os.path.join(home, ".agents", "skills")
        prompts_dst = os.path.join(home, ".codex", "prompts")
        agents_dst = os.path.join(home, ".codex", "agents")
    else:
        src_skills = os.path.join(project, ".agents", "claude", "skills")
        src_commands = os.path.join(project, ".agents", "claude", "commands")
        src_agents = os.path.join(project, ".agents", "claude", "agents")
        skill_dst = os.path.join(project, ".agents", "skills")
        prompts_dst = None  
        agents_dst = os.path.join(project, ".codex", "agents")
    desired_skills = {}
    if os.path.isdir(src_skills):
        for entry in sorted(os.listdir(src_skills)):
            source = os.path.join(src_skills, entry)
            if os.path.isfile(os.path.join(source, "SKILL.md")):
                for path, _, files in os.walk(source):
                    for fn in files:
                        rel = os.path.relpath(os.path.join(path, fn), src_skills)
                        body = _codex_read_bytes(os.path.join(path, fn))
                        if body is not None:
                            if fn == "SKILL.md" and project is None and src_skills != shared:
                                body = _codex_native_source(body.decode(), "skill").encode()
                            desired_skills[rel] = body
    desired_prompts = {}
    if os.path.isdir(src_commands):
        for fn in sorted(os.listdir(src_commands)):
            if not fn.endswith(".md"):
                continue
            body = load_text(os.path.join(src_commands, fn))
            if body is None:
                continue
            if project is None:
                body = _codex_native_source(body, "command")
            name = fn[:-3]
            if name + "/SKILL.md" not in desired_skills:
                desired_skills[name + "/SKILL.md"] = _codex_skill(name, body)
            desired_prompts[fn] = body
    desired_agents = {}
    if os.path.isdir(src_agents):
        for fn in sorted(os.listdir(src_agents)):
            if fn.endswith(".md"):
                body = load_text(os.path.join(src_agents, fn))
                if body is not None:
                    if project is None:
                        body = _codex_native_source(body, "agent")
                    desired_agents[fn[:-3] + ".toml"] = _codex_agent(fn[:-3], body)
    if project is not None and os.path.islink(skill_dst):
        claude_projection = os.path.realpath(os.path.join(hp.project_claude_dir(project), "skills"))
        if os.path.realpath(skill_dst) != claude_projection:
            raise RuntimeError("Codex skill path is a foreign symlink: " + skill_dst)
        backup = skill_dst + ".pre-codex-parity-link"
        suffix = 2
        while os.path.lexists(backup):
            backup = skill_dst + ".pre-codex-parity-link-" + str(suffix)
            suffix += 1
        os.rename(skill_dst, backup)
        os.makedirs(skill_dst)
        report.setdefault("codex", []).append("skills: preserved legacy link at " + backup)
    _sync_codex_files(skill_dst, desired_skills, report, "skills+commands")
    if prompts_dst is not None:
        _sync_codex_files(prompts_dst, desired_prompts, report, "prompts")
    _sync_codex_files(agents_dst, desired_agents, report, "agents")
    if project is not None:
        project_settings = load_json(os.path.join(project, ".agents", "claude",
                                                  "settings.json"), {})
        hooks = project_settings.get("hooks", {}) if isinstance(project_settings, dict) else {}
        rendered, unsupported = render_codex_hooks(
            hooks, os.path.join(store, "global", "scripts", "codex-hook-adapter.py"),
            agent_python(), hook_client())
        hook_file = os.path.join(project, ".codex", "hooks.json")
        marker = os.path.join(project, ".codex", ".agent-context-hook-owned")
        current = load_text(hook_file)
        owned_hash = load_text(marker)
        current_hash = hashlib.sha256(current.encode()).hexdigest() if current else None
        if hooks or owned_hash:
            if current is None or current_hash == owned_hash:
                if rendered["hooks"]:
                    body = json.dumps(rendered, indent=2) + "\n"
                    if body != current:
                        write_text_atomic(hook_file, body)
                    write_text_atomic(marker, hashlib.sha256(body.encode()).hexdigest())
                elif current is not None:
                    os.remove(hook_file)
                    os.remove(marker)
            else:
                report.setdefault("codex", []).append("hooks(project): skipped foreign/modified file")
        claude_only = [e for e in unsupported if e in CLAUDE_ONLY_CODEX_EVENTS]
        unsupported = [e for e in unsupported if e not in CLAUDE_ONLY_CODEX_EVENTS]
        if claude_only:
            report.setdefault("codex", []).append(
                "hooks(project): claude-only events " + ", ".join(claude_only))
        if unsupported:
            report.setdefault("codex", []).append(
                "hooks(project): unsupported events " + ", ".join(unsupported))


CODEX_BLOCK_BEGIN = "# BEGIN agent-context managed MCP\n"
CODEX_BLOCK_END = "# END agent-context managed MCP\n"


def _codex_mcp_config(path, entries):
    current = load_text(path) or ""
    start = current.find(CODEX_BLOCK_BEGIN)
    end = current.find(CODEX_BLOCK_END, start) if start >= 0 else -1
    if start >= 0 and end >= 0:
        unmanaged = current[:start] + current[end + len(CODEX_BLOCK_END):]
    else:
        unmanaged = current
    blocks = []
    for name, spec in sorted(entries.items()):
        header = "[mcp_servers." + name + "]"
        if header in unmanaged:
            continue  
        parts = [header]
        if spec.get("transport") == "stdio":
            parts.append("command = " + json.dumps(subst(spec["command"])))
            parts.append("args = " + json.dumps(subst(spec.get("args", []))))
            if spec.get("env"):
                parts.append("[mcp_servers." + name + ".env]")
                for key, value in sorted(spec["env"].items()):
                    parts.append(key + " = " + json.dumps(subst(value)))
        elif spec.get("transport") == "http":
            parts.append("url = " + json.dumps(spec["url"]))
        else:
            continue
        blocks.append("\n".join(parts))
    managed = (CODEX_BLOCK_BEGIN + "\n\n".join(blocks) + "\n" +
               CODEX_BLOCK_END) if blocks else ""
    updated = unmanaged.rstrip() + ("\n\n" + managed if managed else "\n")
    if updated != current:
        write_text_atomic(path, updated)
    return len(blocks)


def materialize_codex_mcp(home, manifest, report):
    'Global and scoped Codex MCP, without widening Claude-only servers.'
    if not os.path.isdir(os.path.join(home, ".codex")):
        return
    global_entries, scoped = {}, {}
    for name, spec in wanted(manifest, "codex").items():
        projects = spec.get("projects") or []
        if projects:
            for rel in projects:
                root = os.path.join(home, rel)
                if os.path.isdir(root):
                    scoped.setdefault(root, {})[name] = spec
        else:
            global_entries[name] = spec
    count = _codex_mcp_config(os.path.join(home, ".codex", "config.toml"), global_entries)
    state = os.path.join(home, ".codex", "agent-context-projects.json")
    previous = load_json(state, [])
    previous = previous if isinstance(previous, list) else []
    for root in sorted(set(previous) | set(scoped)):
        if os.path.isdir(root) and os.path.commonpath((home, root)) == home:
            config = os.path.join(root, ".codex", "config.toml")
            if os.path.exists(config) or root in scoped:
                _codex_mcp_config(config, scoped.get(root, {}))
                notes = []
                git_exclude(root, ".codex/", notes)
                if notes:
                    report.setdefault("codex", []).extend(notes)
    if previous != sorted(scoped):
        write_json(state, sorted(scoped))
    report.setdefault("codex", []).append("mcp: %d global; %d project(s)" %
                                        (count, len(scoped)))



AGENTS_PIN = os.path.join(STORE, "global", "scripts", "agents-pin.py")
STORE_AGENTS = os.path.join(STORE, "global", "agents")


def materialize_agents(report):
    'Invoked by STORE path rather than through ~/.agent-context/global/scripts, for the same\n    reason preflight-crash-watch is: a projector that depends on its own\n    projection being current cannot be the thing that repairs a stale one.\n\n    Kept in a separate script rather than inlined here because the translation is\n    the whole job -- tool names, and a tier alias that must resolve to a provider\n    opencode can actually authenticate -- and agents-pin.py does the same job for\n    pi. Two harnesses, two spellings, one script.'
    if not os.path.isdir(os.path.join(HOME, ".config", "opencode")):
        return
    if not (os.path.exists(AGENTS_PIN) and os.path.isdir(STORE_AGENTS)):
        return
    progress("agents: pinning opencode agents...")
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, AGENTS_PIN, "opencode", STORE_AGENTS],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        progress("agents: FAILED in %ds" % (time.time() - t0))
        report.setdefault("opencode", []).append(f"agents: FAILED: {exc}")
        finding(f"opencode has no agent definitions projected ({exc}), so the worker-* "
                f"names the ralph-* skills mandate cannot be spawned there")
        return
    progress("agents: done rc=%d in %ds" % (r.returncode, time.time() - t0))
    line = (r.stdout or r.stderr or "").strip()
    line = line.replace("opencode-agents-pin: ", "")
    report.setdefault("opencode", []).append("agents: " + (line or "in sync"))












PI_EXTENSIONS = ("ralph-loop.ts", "agent-context-mcp.ts")
PI_EXT_DIR = os.path.join(HOME, ".pi", "agent", "extensions")
PI_EXT_MANIFEST = ".agent-context-extensions.json"


def materialize_pi_extensions(report):
    "Copy the store's pi extensions into ~/.pi/agent/extensions.\n\n    Manifest-owned, like commands and agents: only names a previous run wrote are\n    retired, and a hand-placed extension of the same name is left alone rather\n    than overwritten. A named file the store does not (yet) hold is simply\n    skipped -- this must never fail a SessionStart over a missing optional file."
    if not os.path.isdir(os.path.dirname(PI_EXT_DIR)):
        return  
    src_dir = os.path.join(STORE, "global", "scripts")
    manifest_path = os.path.join(PI_EXT_DIR, PI_EXT_MANIFEST)
    owned = load_json(manifest_path, [])
    if not isinstance(owned, list):
        owned = []
    generated = [n for n in owned if n in PI_GENERATED]
    owned = [n for n in owned if n not in PI_GENERATED]

    wrote, foreign, missing, relinked, failed = [], [], [], [], []
    for name in PI_EXTENSIONS:
        src = os.path.join(src_dir, name)
        body = load_text(src)
        if body is None:
            missing.append(name)
            continue
        dst = os.path.join(PI_EXT_DIR, name)
        
        
        
        
        
        
        if os.path.islink(dst) and not os.path.exists(dst):
            try:
                os.remove(dst)
                relinked.append(name)
            except OSError:
                pass
        elif os.path.exists(dst) and name not in owned and load_text(dst) != body:
            foreign.append(name)
            continue
        os.makedirs(PI_EXT_DIR, exist_ok=True)
        
        
        try:
            if load_text(dst) != body:
                write_text_atomic(dst, body)   
            wrote.append(name)
        except OSError as e:
            failed.append(f"{name} ({type(e).__name__})")

    pruned = []
    for stale in sorted(set(owned) - set(wrote)):
        try:
            os.remove(os.path.join(PI_EXT_DIR, stale))
            pruned.append(stale)
        except OSError:
            pass
    if wrote or owned:
        try:
            os.makedirs(PI_EXT_DIR, exist_ok=True)
            write_json(manifest_path, sorted(wrote + generated))
        except OSError:
            pass

    parts = []
    if wrote:    parts.append(f"{len(wrote)} extension(s)")
    if pruned:   parts.append("pruned " + ",".join(pruned))
    if relinked: parts.append("REPLACED broken symlink: " + ",".join(relinked))
    if failed:   parts.append("FAILED to write: " + ",".join(failed))
    if foreign:  parts.append("LEFT ALONE (not ours): " + ",".join(foreign))
    
    
    
    
    if failed:
        finding("pi extensions could not be written (%s) -- pi on this machine is "
                "running whatever it already had" % ",".join(failed))
    if foreign:
        finding("pi extension(s) %s differ from the store and were LEFT ALONE because "
                "this projector did not place them; pi here is pinned on that copy "
                "until someone adopts or removes it" % ",".join(foreign))
    
    if missing:  parts.append("NOT IN STORE: " + ",".join(missing))
    if missing:
        finding("pi extension(s) %s are named for projection but the store does not "
                "hold them, so no machine gets them" % ",".join(missing))
    if parts:
        report.setdefault("pi", []).append("extensions: " + "; ".join(parts))


PI_HOOKS_EXT = "agent-context-hooks.ts"


PI_GENERATED = (PI_HOOKS_EXT,)

PI_HOOKS_TEMPLATE = r'''// agent-context hooks for pi. GENERATED by harness-materialize.py. Do not edit.
//
// pi-native wiring for the store's hooks: each pi lifecycle event runs the store's
// hook dispatcher once (`<run> <script> <ClaudeEvent>`, behind hook-client when this host
// has a build so the call runs in the warm hook server; Claude-shaped payload on
// stdin), the same way Claude does, so every guard has one body and one
// registry. A refusal (exit 2, permissionDecision deny, decision block) blocks; any
// other dispatcher outcome (crash, timeout, missing script) allows, the same fail-open
// the dispatcher documents. Slash commands come from the store's commands directory.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const DISPATCHERS = __DISPATCHERS__;
const COMMANDS_DIR = __COMMANDS_DIR__;
const STOP_BLOCK_CAP = 3;

const CLAUDE_NAMES = { bash: "Bash", read: "Read", write: "Write", edit: "Edit", grep: "Grep", find: "Glob", ls: "LS" };

function absolute(value, cwd) {
  if (typeof value !== "string" || value.length === 0) return value;
  const expanded = value === "~" || value.startsWith("~/") ? join(homedir(), value.slice(1)) : value;
  return resolve(cwd, expanded);
}

// pi tool input -> Claude tool_input for the file and shell tools; others pass through.
function claudeInput(tool, input, cwd) {
  const raw = input && typeof input === "object" ? input : {};
  if (tool === "write") return { file_path: absolute(raw.path, cwd), content: raw.content };
  if (tool === "read") return { file_path: absolute(raw.path, cwd), offset: raw.offset, limit: raw.limit };
  if (tool === "edit") {
    const edits = Array.isArray(raw.edits) ? raw.edits : [];
    const first = edits[0] ?? {};
    return { file_path: absolute(raw.path, cwd), old_string: first.oldText ?? "", new_string: first.newText ?? "", replace_all: false, ...(edits.length > 1 ? { edits } : {}) };
  }
  if (tool === "bash") return { command: raw.command, ...(typeof raw.timeout === "number" ? { timeout: raw.timeout * 1000 } : {}) };
  return raw;
}

const NONE = { block: false, reason: "", context: [], message: "" };

function dispatch(event, payload, ctx) {
  const d = DISPATCHERS[event];
  if (!d || !existsSync(d.script)) return NONE;
  const sessionId = ctx.sessionManager?.getSessionId?.() ?? "pi";
  const launcher = d.launcher && existsSync(d.launcher) ? d.launcher : null;
  const r = spawnSync(launcher ?? d.run, launcher ? [d.run, d.script, event] : [d.script, event], {
    input: JSON.stringify({ hook_event_name: event, cwd: ctx.cwd, session_id: sessionId, ...payload }),
    encoding: "utf8",
    timeout: d.timeout * 1000,
    maxBuffer: 256 * 1024 * 1024,
    cwd: existsSync(ctx.cwd) ? ctx.cwd : undefined,
    env: process.env,
  });
  if (r.error || r.status === null) return NONE;
  if (r.status !== 0 && r.status !== 2) return NONE;
  const parsed = parseJsonOutput(r.stdout || "");
  const out = parsed?.hookSpecificOutput ?? {};
  const context = typeof out.additionalContext === "string" && out.additionalContext ? [out.additionalContext] : [];
  const message = typeof parsed?.systemMessage === "string" ? parsed.systemMessage : "";
  const jsonReason = out.permissionDecisionReason || parsed?.reason || "";
  if (r.status === 2) return { block: true, reason: (r.stderr || "").trim() || jsonReason || "blocked by hook", context, message };
  if (out.permissionDecision === "deny") return { block: true, reason: jsonReason || "blocked by hook", context, message };
  if (parsed?.decision === "block") return { block: true, reason: jsonReason || "blocked by hook", context, message };
  if (out.permissionDecision === "ask") return { block: false, ask: true, reason: jsonReason || "A hook asks you to confirm this call.", context, message };
  // PostToolUse only: a guard's replacement for the tool result (compress-tool-output).
  return { block: false, reason: "", context, message, output: out.updatedToolOutput };
}

// The dispatcher prints one JSON object; tolerate text lines before it.
function parseJsonOutput(stdout) {
  const text = stdout.trim();
  for (const start of [0, text.indexOf("\n{") + 1]) {
    if (start < 0 || (start === 0 && !text.startsWith("{"))) continue;
    if (start === 0 || text[start] === "{") {
      try { return JSON.parse(text.slice(start)); } catch { /* try the next start */ }
    }
  }
  return undefined;
}

function lastAssistantText(messages) {
  const last = [...(messages ?? [])].reverse().find((m) => m.role === "assistant");
  if (!last) return "";
  if (typeof last.content === "string") return last.content;
  return (last.content ?? []).filter((c) => c.type === "text").map((c) => c.text).join("\n");
}

function loadCommands() {
  if (!existsSync(COMMANDS_DIR)) return [];
  const found = [];
  for (const file of readdirSync(COMMANDS_DIR)) {
    if (!file.endsWith(".md")) continue;
    found.push({ name: file.slice(0, -3), path: join(COMMANDS_DIR, file) });
  }
  return found;
}

function parseCommand(raw) {
  const text = raw.replace(/\r\n/g, "\n");
  const match = /^---\n([\s\S]*?)\n---\n?([\s\S]*)$/.exec(text);
  if (!match) return { description: "", body: text };
  const desc = /^description:\s*(.*)$/m.exec(match[1]);
  const description = desc ? desc[1].trim().replace(/^(["'])(.*)\1$/, "$2") : "";
  return { description, body: match[2] };
}

export default function (pi: ExtensionAPI) {
  const pendingSession = [];
  const pendingTool = new Map();
  let stopActive = false;
  let stopBlocks = 0;

  pi.on("session_start", async (event, ctx) => {
    stopActive = false;
    stopBlocks = 0;
    pendingTool.clear();
    if (event.reason === "reload") return;
    const source = event.reason === "resume" ? "resume" : event.reason === "new" ? "clear" : "startup";
    const v = dispatch("SessionStart", { source }, ctx);
    pendingSession.length = 0;
    pendingSession.push(...v.context);
    if (v.message) ctx.ui.notify(v.message, "warning");
  });

  pi.on("before_agent_start", async () => {
    if (pendingSession.length === 0) return undefined;
    const content = pendingSession.join("\n");
    pendingSession.length = 0;
    return { message: { customType: "agent-context-hook", content, display: false } };
  });

  pi.on("tool_call", async (event, ctx) => {
    const name = CLAUDE_NAMES[event.toolName] ?? event.toolName;
    const v = dispatch("PreToolUse", { tool_name: name, tool_input: claudeInput(event.toolName, event.input, ctx.cwd), tool_use_id: event.toolCallId }, ctx);
    if (v.message) ctx.ui.notify(v.message, "warning");
    if (v.ask) {
      // A hook asks for confirmation. With no UI the call is refused, the safe default.
      if (ctx.hasUI && await ctx.ui.confirm(`Allow ${event.toolName}?`, v.reason)) return undefined;
      return { block: true, reason: v.reason };
    }
    if (!v.block) {
      if (v.context.length > 0) pendingTool.set(event.toolCallId, v.context);
      return undefined;
    }
    return { block: true, reason: v.reason };
  });

  pi.on("user_bash", async (event, ctx) => {
    const v = dispatch("PreToolUse", { tool_name: "Bash", tool_input: { command: event.command } }, ctx);
    if (v.ask && ctx.hasUI && await ctx.ui.confirm("Allow this command?", v.reason)) return undefined;
    if (!v.block && !v.ask) return undefined;
    return { result: { output: `Blocked by hook: ${v.reason}`, exitCode: 1, cancelled: false, truncated: false } };
  });

  pi.on("tool_result", async (event, ctx) => {
    const name = CLAUDE_NAMES[event.toolName] ?? event.toolName;
    const failed = event.isError === true;
    const text = (event.content ?? []).filter((c) => c.type === "text").map((c) => c.text).join("\n");
    const tail = failed ? { error: text, is_interrupt: false } : { tool_response: { content: event.content, isError: false, stdout: text } };
    const v = dispatch(failed ? "PostToolUseFailure" : "PostToolUse", { tool_name: name, tool_input: claudeInput(event.toolName, event.input, ctx.cwd), tool_use_id: event.toolCallId, ...tail }, ctx);
    const feedback = [...(pendingTool.get(event.toolCallId) ?? []), ...v.context, ...(v.block ? [v.reason] : [])];
    pendingTool.delete(event.toolCallId);
    // The payload's `stdout` is every text part joined, so a replacement of it stands for
    // all of them; parts that are not text (images) are kept after it.
    const shorter = !failed && typeof v.output?.stdout === "string" && v.output.stdout !== text ? v.output.stdout : null;
    if (feedback.length === 0 && shorter === null) return undefined;
    const kept = shorter === null ? (event.content ?? []) : [{ type: "text", text: shorter }, ...(event.content ?? []).filter((c) => c.type !== "text")];
    return { content: [...kept, ...feedback.map((t) => ({ type: "text", text: t }))] };
  });

  pi.on("input", async (event, ctx) => {
    if (event.source === "extension") return { action: "continue" };
    stopBlocks = 0;
    const v = dispatch("UserPromptSubmit", { prompt: event.text }, ctx);
    if (v.block) {
      ctx.ui.notify(v.reason, "error");
      return { action: "handled" };
    }
    if (v.context.length > 0) return { action: "transform", text: `${v.context.join("\n")}\n\n${event.text}` };
    return { action: "continue" };
  });

  pi.on("agent_end", async (event, ctx) => {
    const messages = event.messages ?? [];
    const last = [...messages].reverse().find((m) => m.role === "assistant");
    if (last?.stopReason === "aborted") {
      stopActive = false;
      stopBlocks = 0;
      return;
    }
    const text = lastAssistantText(messages);
    const v = dispatch("Stop", { stop_hook_active: stopActive, ...(text ? { last_assistant_message: text } : {}) }, ctx);
    if (v.message) ctx.ui.notify(v.message, "warning");
    if (!v.block) {
      stopActive = false;
      stopBlocks = 0;
      return;
    }
    stopBlocks += 1;
    if (stopBlocks >= STOP_BLOCK_CAP) {
      stopActive = false;
      stopBlocks = 0;
      ctx.ui.notify(`Stop hook block cap reached (${STOP_BLOCK_CAP} consecutive blocks); ending the turn.`, "warning");
      return;
    }
    stopActive = true;
    pi.sendMessage({ customType: "agent-context-stop-hook", content: v.reason, display: true }, { triggerTurn: true });
  });

  pi.on("session_shutdown", async (event, ctx) => {
    // A reload keeps the session, and its start was skipped above, so skip its end too.
    if (event?.reason === "reload") return;
    dispatch("SessionEnd", { reason: event?.reason ?? "other" }, ctx);
  });

  // A stray or unreadable command file must never take the hooks down with it.
  for (const command of loadCommands()) {
    try {
      const first = parseCommand(readFileSync(command.path, "utf8"));
      pi.registerCommand(command.name, {
        description: first.description || command.name,
        handler: async (args) => {
          const current = parseCommand(readFileSync(command.path, "utf8"));
          pi.sendUserMessage(current.body.split("$ARGUMENTS").join(args ?? ""));
        },
      });
    } catch {
      continue;
    }
  }
}
'''


def render_pi_hooks(manifest, home, commands_dir):
    'TypeScript source of the pi-native hooks extension, from the hooks manifest.\n\n    One entry per dispatched Claude event: kind "dispatcher", harnesses containing\n    "pi". The extension shells to the dispatcher, so the per-script guard list is\n    never duplicated here.'
    dispatchers = {}
    client = hook_client(home)
    for h in (manifest or {}).get("hooks", {}).values():
        if "pi" not in h.get("harnesses", []) or h.get("kind") != "dispatcher":
            continue
        dispatchers[h["claude_event"]] = {
            
            
            "run": agent_python(),
            "launcher": client,
            "script": h["script"].replace("{HOME}", home),
            "timeout": h.get("timeout", 30),
        }
    return (PI_HOOKS_TEMPLATE
            .replace("__DISPATCHERS__", json.dumps(dispatchers, indent=2, sort_keys=True))
            .replace("__COMMANDS_DIR__", json.dumps(commands_dir)))


def materialize_pi_hooks(report):
    'Project the generated hooks extension into ~/.pi/agent/extensions.\n\n    Owned through the same manifest as materialize_pi_extensions. A hand-placed file\n    of the same name is left alone and reported, never overwritten.'
    if not os.path.isdir(os.path.dirname(PI_EXT_DIR)):
        return  
    hooks_manifest = load_json(os.path.join(STORE, "global", "hooks-manifest.json"), {})
    body = render_pi_hooks(hooks_manifest, HOME, os.path.join(STORE, "global", "commands"))
    dst = os.path.join(PI_EXT_DIR, PI_HOOKS_EXT)
    manifest_path = os.path.join(PI_EXT_DIR, PI_EXT_MANIFEST)
    owned = load_json(manifest_path, [])
    owned = [n for n in owned if isinstance(n, str)] if isinstance(owned, list) else []
    current = load_text(dst)
    if current is not None and PI_HOOKS_EXT not in owned and current != body:
        finding("pi extension %s differs from the store and was LEFT ALONE because this "
                "projector did not place it; pi here runs without the store's hooks until "
                "someone adopts or removes it" % PI_HOOKS_EXT)
        report.setdefault("pi", []).append("hooks: LEFT ALONE (not ours): " + PI_HOOKS_EXT)
        return
    changed = current != body
    try:
        os.makedirs(PI_EXT_DIR, exist_ok=True)
        if changed:
            write_text_atomic(dst, body)   
        if PI_HOOKS_EXT not in owned:
            write_json(manifest_path, sorted(set(owned) | {PI_HOOKS_EXT}))
    except OSError as e:
        finding("pi hooks extension could not be written (%s)" % type(e).__name__)
        return
    if changed:
        report.setdefault("pi", []).append("hooks: wrote " + PI_HOOKS_EXT)



def write_json_keep_mode(path, data):
    "write_json, but preserve an existing file's permission bits.\n\n    Xcode's agent config holds an oauthAccount and is mode 600; write_json creates\n    a fresh temp file (644) and renames over it, which would widen it silently."
    mode = None
    try:
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        pass
    write_json(path, data)
    if mode is not None:
        os.chmod(path, mode)


def merge_servers(path, key, entries, writer=write_json):
    'Merge rendered servers into a harness config, preserving everything else.\n    Returns the names that changed.'
    cfg = load_json(path, {})
    if not isinstance(cfg, dict):
        cfg = {}
    servers = cfg.get(key)
    if not isinstance(servers, dict):
        servers = {}
    changed = []
    for name, entry in entries.items():
        if servers.get(name) != entry:
            servers[name] = entry
            changed.append(name)
    cfg[key] = servers
    if changed:
        writer(path, cfg)
    return changed


def project_xcode_commands(notes):
    "Xcode's commands: the store's global/commands, stripped, in shared-commands/.\n\n    Nothing here reads ~/.claude/commands. A store without a commands directory leaves\n    the current Xcode link alone; an empty one empties the projection."
    if not os.path.isdir(STORE_COMMANDS):
        return
    ok, err = project_stripped(STORE_COMMANDS, SHARED_COMMANDS)
    if not ok:
        notes.append(f"commands-FAIL: {err}")
        finding(f"the Xcode commands projection is not being written: rsync failed ({err[:200]})")
        return
    ensure_symlink(os.path.join(XCODE_CLAUDE, "commands"), SHARED_COMMANDS, notes)


def project_xcode_settings(notes):
    "Render ClaudeAgentConfig/settings.json from the store, never from ~/.claude.\n\n    The content is the store's settings-seed.json with the managed values and hooks\n    from home-settings-sync.py asserted on it. Machine-local Claude entries (accumulated\n    permission grants and the like) do not reach Xcode. Hook wiring points at the\n    dispatcher command, as before; the dispatcher's registry stays where it is.\n\n    Deliberately not a symlink. The Xcode agent applies `permissions.additionalDirectories`\n    at session start, and Xcode's agentfileaccesssupervisor turns every entry outside the\n    open workspace into a native approval prompt -- so one stray terminal-session grant\n    prompts in every unrelated Xcode workspace (a grant for one project\n    prompts in another project's session, and the pending prompt wedges the session).\n    Those grants are cwd conveniences with no meaning inside an Xcode workspace, which\n    supplies its own directory scope; everything else -- guard hooks included -- passes\n    through untouched. Xcode's agent may rewrite this file on its own /permissions\n    change; the next run overwrites it, same as the symlinked entries."
    import importlib.util
    dst = os.path.join(XCODE_CLAUDE, "settings.json")
    sync_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home-settings-sync.py")
    spec = importlib.util.spec_from_file_location("home_settings_sync", sync_path)
    if spec is None or spec.loader is None:
        notes.append("settings-FAIL: home-settings-sync.py cannot be loaded")
        return
    sync_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync_mod)
    seed = sync_mod.seed_settings()
    data = sync_mod.sync(seed if seed is not None else {}, None, {})
    perms = data.get("permissions")
    if isinstance(perms, dict) and perms.get("additionalDirectories"):
        data = dict(data, permissions={k: v for k, v in perms.items()
                                       if k != "additionalDirectories"})
    if not os.path.islink(dst) and load_json(dst, None) == data:
        return
    try:
        if os.path.islink(dst):
            os.unlink(dst)
        write_json(dst, data)
        notes.append("settings.json (filtered copy: additionalDirectories dropped)")
    except OSError as e:
        notes.append(f"settings-FAIL {os.path.relpath(dst, HOME)}: {e}")


def materialize_xcode(manifest, report):
    'Xcode 27\'s built-in agents do not read ~/.claude.\n\n    Xcode launches its own bundled claude binary with CLAUDE_CONFIG_DIR pointed at\n    ~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig, so that directory\n    -- not ~/.claude -- is where its global CLAUDE.md, settings.json (hooks, i.e.\n    the git/worktree guards), skills, commands and MCP list come from. Verified by\n    the framework\'s own log strings ("Set CLAUDE_CONFIG_DIR to Claude Agent config\n    directory") and by `CLAUDE_CONFIG_DIR=<dir> claude mcp list` reading that dir\'s\n    .claude.json. Without this function an Xcode agent session runs with no store\n    instructions, no skills and no guardrails.\n\n    Two MCP files are written because Xcode has two readers: the Claude agent\'s own\n    .claude.json, and CodingAssistant/mcp-servers.json -- the "shared config" every\n    Xcode agent (Claude, Codex, custom ACP) merges. The shared file is Xcode-owned\n    and can be rewritten from the Settings > Intelligence MCP UI; this is idempotent\n    and re-adds our entries at the next session start.'
    if not os.path.isdir(XCODE_CA):
        return
    specs = {n: s for n, s in manifest["servers"].items()
             if "xcode" in s.get("harnesses", []) and not s.get("projects")}
    
    

    os.makedirs(XCODE_CLAUDE, exist_ok=True)

    cfg_path = os.path.join(XCODE_CLAUDE, hp.CLAUDE_JSON_NAME)
    existing = load_json(cfg_path, {}).get("mcpServers") or {}
    changed = merge_servers(cfg_path, "mcpServers",
                            {n: render_claude(s, existing.get(n)) for n, s in specs.items()},
                            writer=write_json_keep_mode)
    if changed:
        report.setdefault("xcode", []).append("mcp (claude agent): " + ",".join(changed))

    shared = os.path.join(XCODE_CA, "mcp-servers.json")
    changed = merge_servers(shared, "mcpServers",
                            {n: render_copilot(s, None) for n, s in specs.items()})
    if changed:
        report.setdefault("xcode", []).append("mcp (shared config): " + ",".join(changed))

    notes = []
    
    
    
    
    for link, target in {
        os.path.join(XCODE_CLAUDE, "CLAUDE.md"): AGENTS_MD,
        os.path.join(XCODE_CLAUDE, "skills"): SHARED_SKILLS,
    }.items():
        if os.path.exists(target) or os.path.islink(target):
            ensure_symlink(link, target, notes)
    project_xcode_commands(notes)
    project_xcode_settings(notes)
    if notes:
        report.setdefault("xcode", []).extend(notes)

    
    
    
    
    progress("xcode: checking IDEChatImportUserClaudeSettings...")
    t0 = time.time()
    cur = subprocess.run(["defaults", "read", "com.apple.dt.Xcode",
                          "IDEChatImportUserClaudeSettings"],
                         capture_output=True, text=True).stdout.strip()
    if cur != "1":
        subprocess.run(["defaults", "write", "com.apple.dt.Xcode",
                        "IDEChatImportUserClaudeSettings", "-bool", "true"],
                       capture_output=True)
        note = "pref: IDEChatImportUserClaudeSettings=true"
        if subprocess.run(["pgrep", "-x", "Xcode"], capture_output=True).returncode == 0:
            
            
            note += " (Xcode is running: restart it, and re-run if the pref reverts)"
        report.setdefault("xcode", []).append(note)
    progress("xcode: done in %ds" % (time.time() - t0))



def strip_orca(obj):
    'Recursively drop any hook handler/group that references dead ~/.orca wiring.'
    if isinstance(obj, dict):
        if any(isinstance(v, str) and ORCA_MARK in v for v in obj.values()):
            return None
        out = {}
        for k, v in obj.items():
            sv = strip_orca(v)
            if sv is None or (isinstance(sv, (list, dict)) and len(sv) == 0):
                continue
            out[k] = sv
        
        if ("matcher" in obj or "hooks" in obj) and not out.get("hooks"):
            return None
        return out
    if isinstance(obj, list):
        out = []
        for it in obj:
            sit = strip_orca(it)
            if sit is None or (isinstance(sit, (list, dict)) and len(sit) == 0):
                continue
            out.append(sit)
        return out
    return obj


def prune_orca(path, report, hname):
    cfg = load_json(path, None)
    if cfg is None:
        return
    cleaned = strip_orca(cfg)
    if cleaned != cfg:
        write_json(path, cleaned)
        report.setdefault(hname, []).append("hooks: pruned orca")


OPENCODE_GUARD_PLUGIN = '''// agent-context guards. GENERATED by harness-materialize.py. Do not edit.
//
// opencode 2.x only. A plugin is a default-exported definition with an id and
// setup(ctx); the only pre-tool gate is ctx.tool.hook("execute.before"), and
// throwing from it aborts the tool call, which is how these guards block. The
// scripts are the SAME store copies Claude and pi run, so there is one guard
// body per rule, never a per-harness port.
//
// TWO PAYLOAD SHAPES. The first guards are payload-tolerant and get opencode's own
// tool name and input. The CLAUDE_* guards match Claude's tool names (Bash, Edit,
// Write) and a file_path, so they get a payload translated into that shape.
//
// TWO WAYS A GUARD REFUSES, both honored. The git guards block with a nonzero exit,
// Claude's convention. plain-language-check exits 0 and writes a JSON decision on
// stdout. Reading only the exit code installed that guard as a no-op, which is
// worse than not installing it: the roster would claim coverage nothing provided.
import {{ spawn, spawnSync }} from "node:child_process"
import {{ createHash }} from "node:crypto"
import {{ existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, unlinkSync, watch, writeFileSync }} from "node:fs"
import {{ homedir }} from "node:os"
import {{ isAbsolute, join, resolve }} from "node:path"

const SHELL_GUARDS = {shell}
const EDIT_GUARDS = {edit}
const CLAUDE_SHELL_GUARDS = {claude_shell}
const CLAUDE_EDIT_GUARDS = {claude_edit}
// opencode-approval.py: user's Approve on an approval question, asked with the
// question tool, grants what approval-question grants in Claude Code.
const APPROVAL = {approval}
// locked-test-drift-gate.py. opencode has no hook that can refuse the end of a
// turn, so the gate runs after every shell call, the one route to a write no
// pre-tool guard can see, and its refusal is appended to that call's result.
const DRIFT_GATE = {drift}
// compress-tool-output.py: shortens a large, repetitive result and saves the original.
// It runs only for a result of COMPRESS_MIN characters or more, the size under which
// the hook itself does nothing.
const COMPRESS = {compress}
const COMPRESS_MIN = 2048
const EDIT_TOOLS = new Set(["edit", "write", "patch", "multiedit"])
const LANGUAGE_RULES = {brief}
// hook-client (policy) when this host has a build, else null; the interpreter and dispatcher
// it launches.
const LAUNCHER = {launcher}
const PYTHON = {python}
const DISPATCHER = {dispatcher}

// A guard's refusal text, or "" when it allows.
function refusal(script, payload, event = "PreToolUse") {{
  // Through hook-client the guard runs warm in the hook server under the dispatcher's
  // --guard mode, which turns a JSON deny or block into exit 2 with the reason on stderr.
  const opts = {{ input: JSON.stringify(payload), encoding: "utf8" }}
  const r = LAUNCHER && existsSync(LAUNCHER)
    ? spawnSync(LAUNCHER, [PYTHON, DISPATCHER, event, "--guard", script], opts)
    : spawnSync(PYTHON, [script], opts)
  // Non-zero means the guard blocked (Claude's convention is exit 2, but any
  // non-zero is treated as a block here; a guard that errored must not fail open).
  if (r.status && r.status !== 0) return (r.stderr || "").trim() || `blocked by ${{script}}`
  // Exit 0 with a JSON deny on stdout is the other convention.
  const out = (r.stdout || "").trim()
  if (!out.startsWith("{{")) return ""
  let d
  try {{ d = JSON.parse(out) }} catch {{ return "" }}
  const h = d.hookSpecificOutput || {{}}
  if (h.permissionDecision === "deny") return h.permissionDecisionReason || `blocked by ${{script}}`
  if (d.decision === "block") return d.reason || `blocked by ${{script}}`
  if (d.systemMessage) console.error(d.systemMessage)
  return ""
}}

function run(script, payload) {{
  const why = refusal(script, payload)
  if (why) throw new Error(why)
}}

const isShell = (tool) => tool === "bash" || tool === "shell"
const PATH_KEYS = ["filePath", "file_path", "path"]
const STORE_DIR = (process.env.HOME || "") + "/.agent-context/"
const BOOTSTRAP = "get_session_context"
const QUESTION = "question"

const DENY_UNLOADED = "Refused: this session has not loaded the agent-context store, so it has no instructions, memory or guardrails. Call get_session_context with the working directory through execute, read ALL of the output with no slice, substring or truncation, follow it, then retry."
const DENY_SLICED = "Refused: get_session_context output must be read in full. Do not slice, substring or truncate it. Its tail carries the rules for how the store is edited."
const DENY_STORE = "Refused: this path is inside the agent-context store or is a generated projection of it. Files there are rewritten from the backend at session start, so an edit on disk is lost. Change the entity through the agent-context MCP tools (edit_body, upsert_script, upsert_doc and the rest)."

// A session's load is also marked on disk: opencode loads this file again when a session
// start rewrites it, and a set held only in memory then forgot every running session,
// which was refused until it loaded the store a second time (policy).
const bootstrapped = new Set()
const denials = new Map()

function loadMark(sessionID) {{
  return join(stateDir(), "opencode-bootstrap", String(sessionID).replace(/[^A-Za-z0-9_.-]/g, "_"))
}}

function markLoaded(sessionID) {{
  bootstrapped.add(sessionID)
  try {{
    const file = loadMark(sessionID)
    mkdirSync(join(file, ".."), {{ recursive: true, mode: 0o700 }})
    writeFileSync(file, "")
  }} catch {{}}
}}

// A mark is touched at each load; one untouched for 30 days belongs to a session long gone.
function pruneLoadMarks() {{
  const root = join(stateDir(), "opencode-bootstrap")
  const cutoff = Date.now() - 30 * 86400 * 1000
  for (const name of existsSync(root) ? readdirSync(root) : []) {{
    try {{ if (statSync(join(root, name)).mtimeMs < cutoff) unlinkSync(join(root, name)) }} catch {{}}
  }}
}}

function hasLoaded(sessionID) {{
  if (bootstrapped.has(sessionID)) return true
  if (!sessionID || !existsSync(loadMark(sessionID))) return false
  bootstrapped.add(sessionID)
  return true
}}

function targetPath(args) {{
  for (const k of PATH_KEYS) if (typeof args?.[k] === "string") return args[k]
  return ""
}}

// Every file a patch adds, updates, deletes or moves to.
function patchPaths(args) {{
  const text = typeof args?.patchText === "string" ? args.patchText : ""
  const found = []
  for (const m of text.matchAll(/^\\*\\*\\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm)) {{
    found.push(m[1].trim())
  }}
  return found
}}

function isGenerated(path) {{
  try {{
    if (!existsSync(path)) return false
    // Only the store's own header markers: a repo file that merely says "do not edit" is
    // not a projection (policy).
    return /generated by (agent-context|harness-materialize|project-materialize)/i.test(readFileSync(path, "utf8").slice(0, 600))
  }} catch {{ return false }}
}}

function gateBootstrap(sessionID, tool, args) {{
  const code = tool === "execute" ? String(args?.code ?? "") : ""
  if (tool.includes(BOOTSTRAP) || code.includes(BOOTSTRAP)) {{
    if (code && /[.] *(slice|substring|substr) *[(]/.test(code)) throw new Error(DENY_SLICED)
    markLoaded(sessionID)
    return
  }}
  if (hasLoaded(sessionID)) return
  if (tool === "execute" && !/tools[.[]/.test(code)) return
  const n = (denials.get(sessionID) ?? 0) + 1
  denials.set(sessionID, n)
  if (n > 5) return
  throw new Error(DENY_UNLOADED)
}}

// The store's worker agents (global/agents/worker-*.md), by the agent name opencode puts
// on every tool event.
function isWorker(agent) {{
  return typeof agent === "string" && agent.startsWith("worker-")
}}

function guardsFor(tool) {{
  return isShell(tool) ? SHELL_GUARDS : (EDIT_TOOLS.has(tool) ? EDIT_GUARDS : [])
}}

function guardTool(event, cwd) {{
  const tool = event.tool
  const args = event.input
  const session = event.sessionID || "default"
  // A worker is a child session with its own id and a brief from its lead. It cannot load
  // the store itself: worker-explore has no `execute` tool to make the call with, so the
  // gate refused its every read and a read-only fan-out returned nothing (policy). In
  // Claude Code a subagent runs under its lead's session id and is never gated either.
  if (!isWorker(event.agent)) gateBootstrap(session, tool, args)
  const at = (p) => (isAbsolute(p) ? p : resolve(cwd, p))
  const paths = tool === "patch" ? patchPaths(args) : [targetPath(args)].filter(Boolean)
  if (EDIT_TOOLS.has(tool)) {{
    for (const p of paths) {{
      if (at(p).startsWith(STORE_DIR) || isGenerated(at(p))) throw new Error(DENY_STORE)
    }}
  }}
  const base = {{ session_id: session, cwd }}
  // A shell call runs in its `workdir` when it names one, so every shell guard judges it
  // there, not in the session's directory (policy).
  const workdir = isShell(tool) && typeof args?.workdir === "string" && args.workdir ? at(args.workdir) : cwd
  for (const g of guardsFor(tool)) run(g, {{ ...base, cwd: workdir, tool_name: tool, tool_input: args ?? {{}} }})
  if (isShell(tool)) {{
    const call = {{ ...base, cwd: workdir, tool_name: "Bash", tool_input: {{ command: args?.command ?? "" }} }}
    for (const g of CLAUDE_SHELL_GUARDS) run(g, call)
  }} else if (EDIT_TOOLS.has(tool)) {{
    const name = tool === "write" ? "Write" : "Edit"
    for (const p of paths) {{
      for (const g of CLAUDE_EDIT_GUARDS) run(g, {{ ...base, tool_name: name, tool_input: {{ file_path: at(p) }} }})
    }}
  }} else if (tool === QUESTION && APPROVAL) {{
    const r = spawnSync(PYTHON, [APPROVAL], {{ input: JSON.stringify(approvalCall(event, cwd, "PreToolUse")), encoding: "utf8" }})
    if (r.status && r.status !== 0) throw new Error((r.stderr || "").trim() || "blocked by opencode-approval")
  }}
}}

function approvalCall(event, cwd, name) {{
  return {{ hook_event_name: name, tool_name: QUESTION, session_id: event.sessionID || "default",
    tool_use_id: event.id, cwd, tool_input: event.input ?? {{}} }}
}}

// Add a note to a finished tool call's result, where the model reads it.
function addNote(event, text) {{
  const result = event.result ?? (event.result = {{}})
  if (typeof result.content === "string") result.content += "\\n\\n" + text
  else if (Array.isArray(result.content)) result.content.push({{ type: "text", text }})
  else {{
    const out = result.output
    const shown = out === undefined ? "" : (typeof out === "string" ? out : JSON.stringify(out))
    result.content = shown ? shown + "\\n\\n" + text : text
  }}
}}

// Replace a finished call's text with the shorter one compress-tool-output gives, in the
// result the model reads and in `output`, which a call nested in a script reads. Parts
// that are not text (files, images) are kept. Any fault leaves the result as it was.
function shortenResult(event, cwd) {{
  const result = event.result
  if (!COMPRESS || !result || !Array.isArray(result.content)) return
  const text = result.content.filter((p) => p?.type === "text" && typeof p.text === "string").map((p) => p.text).join("\\n")
  if (text.length < COMPRESS_MIN) return
  const payload = {{ hook_event_name: "PostToolUse", session_id: event.sessionID || "default", cwd,
    tool_name: event.tool, tool_use_id: event.id, tool_input: event.input ?? {{}}, tool_response: {{ stdout: text }} }}
  const opts = {{ input: JSON.stringify(payload), encoding: "utf8", timeout: 10000, maxBuffer: 64 * 1024 * 1024 }}
  const r = LAUNCHER && existsSync(LAUNCHER)
    ? spawnSync(LAUNCHER, [PYTHON, DISPATCHER, "PostToolUse", "--guard", COMPRESS], opts)
    : spawnSync(PYTHON, [COMPRESS], opts)
  if (r.status !== 0) return
  let shorter
  try {{ shorter = JSON.parse((r.stdout || "").trim()).hookSpecificOutput?.updatedToolOutput?.stdout }} catch {{ return }}
  if (typeof shorter !== "string" || !shorter || shorter === text) return
  result.content = [{{ type: "text", text: shorter }}, ...result.content.filter((p) => p?.type !== "text")]
  if (typeof result.output === "string") result.output = shorter
}}

function afterTool(event, cwd) {{
  if (event.status !== "completed") return
  shortenResult(event, cwd)
  if (event.tool === QUESTION && APPROVAL) {{
    // The answers come from the result opencode built out of user's form reply, never
    // from the tool input, which the model writes.
    const call = approvalCall(event, cwd, "PostToolUse")
    call.tool_response = {{ answers: event.result?.output?.answers }}
    const r = spawnSync(PYTHON, [APPROVAL], {{ input: JSON.stringify(call), encoding: "utf8" }})
    const note = r.status === 0
      ? (r.stdout || "").trim()
      : "opencode-approval: the hook failed, so nothing was granted."
    if (note) addNote(event, note)
  }} else if (isShell(event.tool) && DRIFT_GATE) {{
    const why = refusal(DRIFT_GATE, {{ hook_event_name: "Stop", session_id: event.sessionID || "default", cwd }}, "Stop")
    if (why) addNote(event, why)
  }}
}}

// Inter-agent messaging (policy): the wake. opencode runs one service for the user, with one
// plugin instance and one agent-context bridge per project directory, so the bridge has no
// session of its own and cannot reach one. It leaves each peer message as a file in a spool
// named by this service's pid and the directory (server: peer_wake._opencode_spool, which
// must agree with peerSpool), and this plugin admits it to the directory's most recently
// active session, which starts a turn on it. The bridge declares the wake route only while
// the spool directory exists, so making it here is what turns the route on. Until a session
// has been active in the directory, a message waits in the spool.
function stateDir() {{
  if (process.platform === "darwin") return join(homedir(), "Library", "Application Support", "agent-context")
  return join(process.env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "agent-context")
}}

function peerSpool(cwd) {{
  let real = cwd
  try {{ real = realpathSync(cwd) }} catch {{}}
  const name = createHash("sha256").update(real).digest("hex").slice(0, 16)
  return join(stateDir(), "peer-spool", String(process.pid), name)
}}

// A spool left by a service that has exited is litter: nothing will read it.
function prunePeerSpools() {{
  const root = join(stateDir(), "peer-spool")
  for (const name of existsSync(root) ? readdirSync(root) : []) {{
    const pid = Number(name)
    if (!Number.isInteger(pid) || pid === process.pid) continue
    try {{ process.kill(pid, 0) }} catch (e) {{
      if (e?.code === "ESRCH") rmSync(join(root, name), {{ recursive: true, force: true }})
    }}
  }}
}}

// Returns the function that records a session seen in this directory.
function startPeerWake(ctx, cwd) {{
  const dir = peerSpool(cwd)
  let session = null
  let busy = false
  const drain = async () => {{
    if (!session || busy) return
    busy = true
    try {{
      for (const name of readdirSync(dir).sort()) {{
        if (!name.endsWith(".json")) continue
        const file = join(dir, name)
        let text
        // Deliver only a file this call removed: a reloaded plugin leaves a second watcher.
        try {{ text = JSON.parse(readFileSync(file, "utf8")).text; unlinkSync(file) }} catch {{ continue }}
        if (typeof text === "string" && text) await ctx.session.synthetic({{ sessionID: session, text }})
      }}
    }} catch {{}} finally {{ busy = false }}
  }}
  try {{
    prunePeerSpools()
    pruneLoadMarks()
    mkdirSync(dir, {{ recursive: true, mode: 0o700 }})
    watch(dir, () => {{ void drain() }})
  }} catch {{}}
  return (id) => {{
    if (!id || id === "default") return
    session = id
    void drain()
  }}
}}

// opencode raises no Stop event, so terse-output-gate cannot run here and
// nothing can refuse a finished message the way it does on Claude and pi. This
// is the substitute available: state the rules in the system prompt every
// request, so the model does not produce the text in the first place. The text
// is generated from plain-language-words.py at materialize time, so it cannot
// drift from what the write guard blocks.
export default {{
  id: "agent-context-guards",
  async setup(ctx) {{
    // One plugin instance per project location; its directory is the session's cwd.
    const cwd = ctx.location?.directory || process.cwd()
    // Claude Code refreshes a project's .agents files at SessionStart; opencode has no such
    // event, so the refresh starts here, once per project location, without holding up
    // setup. Without it a project's scripts stayed days behind the store (policy).
    try {{
      spawn(PYTHON, [join(DISPATCHER, "..", "project-materialize.py"), cwd],
        {{ cwd, detached: true, stdio: "ignore" }}).on("error", () => {{}}).unref()
    }} catch {{}}
    const seen = startPeerWake(ctx, cwd)
    await ctx.tool.hook("execute.before", (event) => {{ seen(event?.sessionID); return guardTool(event, cwd) }})
    await ctx.tool.hook("execute.after", (event) => {{ seen(event?.sessionID); return afterTool(event, cwd) }})
    await ctx.session.hook("context", (event) => {{
      seen(event?.sessionID)
      if (!event?.system || event.system.some((s) => (s?.text ?? s ?? "").includes("No em dashes"))) return
      event.system.push({{ type: "text", text: LANGUAGE_RULES }})
    }})
  }},
}}
'''


def materialize_hooks(report):
    hm = load_json(HOOK_MANIFEST, None)
    scripts = (hm or {}).get("hooks", {})

    def scr(name):
        return scripts.get(name, {}).get("script", "").replace("{HOME}", HOME)

    
    copilot_orca = os.path.join(HOME, ".copilot", "hooks", "orca.json")
    if os.path.exists(copilot_orca):
        os.remove(copilot_orca)
        report.setdefault("copilot", []).append("hooks: removed orca.json")
    prune_orca(os.path.join(HOME, ".cursor", "hooks.json"), report, "cursor")
    prune_orca(os.path.join(HOME, ".gemini", "settings.json"), report, "gemini")
    prune_orca(os.path.join(HOME, ".gemini", "config", "hooks.json"), report, "antigravity")

    if not scripts:
        return

    
    
    
    
    
    
    
    
    
    
    
    
    
    cop_settings = os.path.join(HOME, ".copilot", "settings.json")
    if os.path.isdir(os.path.join(HOME, ".copilot")):
        mat = scr("harness-materialize")
        guards = [scr("block-git-stash"), scr("guard-git-write"),
                  scr("require-worktree-edit"), scr("plain-language-check"),
                  scr("block-write-outside-home")]
        managed = {
            "SessionStart": [{"type": "command", "command": f"{agent_python()} {mat}",
                              "timeoutSec": 25}],
            "PreToolUse": [{"type": "command", "command": guard_command(g),
                            "timeoutSec": 10} for g in guards if g],
        }
        
        
        
        
        compress = os.path.join(hp.hooks_dir(HOME), "compress-tool-output.py")
        if os.path.isfile(compress):
            managed["PostToolUse"] = [{"type": "command",
                                       "command": f"{agent_python()} {compress}",
                                       "timeoutSec": 10}]
        cfg = load_json(cop_settings, {})
        if not isinstance(cfg, dict):
            cfg = {}
        if cfg.get("hooks") != managed:
            cfg["hooks"] = managed
            write_json(cop_settings, cfg)
            report.setdefault("copilot", []).append("hooks: wired session-start + guards")
        
        stale = os.path.join(HOME, ".copilot", "hooks", "agent-context.json")
        if os.path.exists(stale):
            os.remove(stale)
            report.setdefault("copilot", []).append("hooks: removed unread hooks/agent-context.json")

    
    
    
    
    
    oc_plugins = os.path.join(HOME, ".config", "opencode", "plugin")
    if not os.path.isdir(oc_plugins):
        oc_plugins = os.path.join(HOME, ".config", "opencode", "plugins")
    if os.path.isdir(os.path.dirname(oc_plugins)):
        shell_guards = [g for g in (scr("block-git-stash"), scr("guard-git-write"),
                                    scr("plain-language-check"),
                                    scr("block-write-outside-home")) if g]
        edit_guard = [g for g in (scr("require-worktree-edit"),
                                  scr("plain-language-check"),
                                  scr("block-write-outside-home")) if g]
        
        
        def store_hook(name):
            path = os.path.join(hp.hooks_dir(HOME), name)
            return path if os.path.isfile(path) else None

        claude_guards = [g for g in (store_hook("block-consent-self-grant.py"),
                                     store_hook("block-locked-test-edit.py")) if g]
        
        
        
        progress("hooks: building opencode guard plugin...")
        t0 = time.time()
        try:
            brief = subprocess.run(
                [sys.executable, os.path.join(STORE, "global", "scripts",
                                         "plain-language-words.py"), "--brief"],
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            brief = ""
        progress("hooks: opencode guard plugin done in %ds" % (time.time() - t0))
        body = OPENCODE_GUARD_PLUGIN.format(
            shell=json.dumps(shell_guards),
            edit=json.dumps(edit_guard),
            claude_shell=json.dumps(claude_guards),
            claude_edit=json.dumps(claude_guards),
            approval=json.dumps(store_hook("opencode-approval.py")),
            drift=json.dumps(store_hook("locked-test-drift-gate.py")),
            compress=json.dumps(store_hook("compress-tool-output.py")),
            brief=json.dumps(brief),
            launcher=json.dumps(hook_client()),
            python=json.dumps(agent_python()),
            dispatcher=json.dumps(GUARD_DISPATCHER),
        )
        os.makedirs(oc_plugins, exist_ok=True)
        p = os.path.join(oc_plugins, "agent-context-guards.js")
        if load_text(p) != body:
            write_text_atomic(p, body)   
            report.setdefault("opencode", []).append("hooks: wired guards (plugin)")

    
    ag = os.path.join(HOME, ".gemini", "config", "hooks.json")
    if os.path.isdir(os.path.dirname(ag)):
        cfg = load_json(ag, {})
        if not isinstance(cfg, dict):
            cfg = {}
        shell_guards = [scr("block-git-stash"), scr("guard-git-write"),
                        scr("plain-language-check"), scr("block-write-outside-home")]
        edit_guards = [scr("require-worktree-edit"), scr("plain-language-check"),
                       scr("block-write-outside-home")]
        managed = {"PreToolUse": [
            {"matcher": "run_command",
             "hooks": [{"type": "command", "command": guard_command(g)} for g in shell_guards if g]},
            {"matcher": "edit_file|write_file|replace_file_content|create_file",
             "hooks": [{"type": "command", "command": guard_command(g)} for g in edit_guards if g]},
        ]}
        if cfg.get("agent-context-guards") != managed:
            cfg["agent-context-guards"] = managed
            write_json(ag, cfg)
            report.setdefault("antigravity", []).append("hooks: wired guards (validate blocking)")






CODEX_EVENT_MAP = {
    "Notification": "PermissionRequest",
    "PermissionRequest": "PermissionRequest",
    "PostToolUse": "PostToolUse",
    "PostToolUseFailure": "PostToolUse",
    "PreCompact": "PreCompact",
    "PostCompact": "PostCompact",
    "PreToolUse": "PreToolUse",
    "SessionStart": "SessionStart",
    "SessionEnd": "SessionEnd",
    "UserPromptSubmit": "UserPromptSubmit",
    "SubagentStart": "SubagentStart",
    "SubagentStop": "SubagentStop",
    "Stop": "Stop",
    "Interrupt": "Interrupt",
}





CLAUDE_ONLY_CODEX_EVENTS = {
    "WorktreeCreate": "Claude-created worktrees are placed by the WorktreeCreate hook; "
                      "Codex has no such event",
    "StopFailure": "fires when a Claude turn ends on an API error; Codex has no such event",
    "TeammateIdle": "Claude Code agent-teams concept; Codex has no such event",
}


def render_codex_hooks(claude_hooks, adapter, interpreter, launcher=None):
    "Translate Claude's active hook graph to Codex's native hooks.json shape.\n\n    Every command crosses one adapter. Besides the three lifecycle-name differences,\n    that boundary normalizes Codex tool names to the Claude names the canonical hook\n    bodies already understand. Unknown non-empty events are returned to the caller;\n    silently dropping a future Claude event would claim parity while removing it."
    rendered = {}
    unsupported = []
    if not isinstance(claude_hooks, dict):
        return {"hooks": {}}, unsupported
    for source_event, groups in claude_hooks.items():
        if not groups:
            continue
        target_event = CODEX_EVENT_MAP.get(source_event)
        if target_event is None:
            unsupported.append(source_event)
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            out_group = {k: v for k, v in group.items() if k != "hooks"}
            out_handlers = []
            for handler in group.get("hooks", []):
                if not isinstance(handler, dict):
                    continue
                out = dict(handler)
                if out.get("type") == "command" and out.get("command"):
                    
                    
                    out["command"] = shlex.join(
                        ([launcher] if launcher else [])
                        + [interpreter, adapter, "--event", source_event, "--", out["command"]]
                    )
                out_handlers.append(out)
            if out_handlers:
                out_group["hooks"] = out_handlers
                rendered.setdefault(target_event, []).append(out_group)
    return {"hooks": rendered}, sorted(unsupported)


_HOOK_STATE_HEADER = re.compile(r'^\[hooks\.state\."((?:[^"\\]|\\.)*)"\]\s*$')


def render_codex_trust(config_text, hooks_path, states):
    'Replace trust only for the generated user hook file; keep every other hook.'
    lines = config_text.splitlines(keepends=True)
    kept = []
    i = 0
    prefix = hooks_path + ":"
    while i < len(lines):
        match = _HOOK_STATE_HEADER.match(lines[i].rstrip("\n"))
        if match:
            try:
                key = json.loads('"' + match.group(1) + '"')
            except (TypeError, ValueError):
                key = ""
            if key.startswith(prefix):
                i += 1
                while i < len(lines) and not lines[i].lstrip().startswith("["):
                    i += 1
                continue
        kept.append(lines[i])
        i += 1
    base = "".join(kept).rstrip() + "\n"
    blocks = []
    for state in sorted(states, key=lambda item: item.get("key", "")):
        key = state.get("key")
        current_hash = state.get("currentHash")
        if not isinstance(key, str) or not key.startswith(prefix) or not current_hash:
            continue
        quoted_key = json.dumps(key)
        quoted_hash = json.dumps(current_hash)
        blocks.append(
            f"[hooks.state.{quoted_key}]\n"
            "enabled = true\n"
            f"trusted_hash = {quoted_hash}\n"
        )
    return base + ("\n" + "\n".join(blocks) if blocks else "")


def codex_hook_states(cwd, hooks_path):
    'Ask the installed Codex runtime for its own hook keys and content hashes.'
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex executable not found")
    proc = subprocess.Popen(
        [codex, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    requests = [
        {"method": "initialize", "id": 1,
         "params": {"clientInfo": {"name": "agent-context-materializer",
                                    "title": "agent-context-materializer", "version": "1"},
                    "capabilities": {}}},
        {"method": "initialized", "params": {}},
        {"method": "hooks/list", "id": 2, "params": {"cwds": [cwd]}},
    ]
    try:
        assert proc.stdin is not None and proc.stdout is not None
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        deadline = time.time() + 10
        response = None
        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.time()))
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                continue
            if message.get("id") == 2:
                response = message
                break
        if response is None or "result" not in response:
            raise RuntimeError("Codex hooks/list did not return a result")
        entries = response["result"].get("data", [])
        if not entries:
            return []
        errors = entries[0].get("errors", [])
        if errors:
            raise RuntimeError("; ".join(e.get("message", str(e)) for e in errors))
        return [h for h in entries[0].get("hooks", [])
                if h.get("sourcePath") == hooks_path]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def persist_codex_hook_trust(home, hooks_path):
    states = codex_hook_states(home, hooks_path)
    config = os.path.join(home, ".codex", "config.toml")
    current = load_text(config) or ""
    updated = render_codex_trust(current, hooks_path, states)
    if updated != current:
        write_text_atomic(config, updated)
    return states


def materialize_codex_hooks(report, home=None, claude_settings=None,
                            codex_hooks=None, adapter=None, interpreter=None, launcher=None):
    'Project the canonical Claude hook graph into Codex, and only Codex.'
    home = home or HOME
    claude_settings = claude_settings or hp.settings_file(home)
    codex_hooks = codex_hooks or os.path.join(home, ".codex", "hooks.json")
    adapter = adapter or os.path.join(STORE, "global", "scripts", "codex-hook-adapter.py")
    interpreter = interpreter or agent_python()
    settings = load_json(claude_settings, {})
    source = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    rendered, unsupported = render_codex_hooks(source, adapter, interpreter, launcher)
    if load_json(codex_hooks, None) != rendered:
        write_json(codex_hooks, rendered)
        report.setdefault("codex", []).append("hooks: projected Claude parity graph")
    claude_only = [e for e in unsupported if e in CLAUDE_ONLY_CODEX_EVENTS]
    unsupported = [e for e in unsupported if e not in CLAUDE_ONLY_CODEX_EVENTS]
    if claude_only:
        report.setdefault("codex", []).append(
            "hooks: claude-only events: " + ", ".join(claude_only))
    if unsupported:
        finding("Codex hook parity has unsupported non-empty Claude events: " +
                ", ".join(unsupported))
        report.setdefault("codex", []).append(
            "hooks: unsupported events: " + ", ".join(unsupported))
    try:
        states = persist_codex_hook_trust(home, codex_hooks)
        if any(s.get("trustStatus") != "trusted" for s in states):
            report.setdefault("codex", []).append("hooks: persisted native trust")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        finding("Codex hook trust was not persisted: " + str(exc))
        report.setdefault("codex", []).append("hooks: trust FAILED: " + str(exc))
    return unsupported



def main():
    
    
    
    
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    if args and args[0] not in ("--commands", "--codex-project"):
        print(f"harness-materialize: unknown flag {args[0]}", file=sys.stderr)
        return 2
    unknown = [a for a in args[3:] if a.startswith("-")]
    if unknown:
        print(f"harness-materialize: unknown flag {unknown[0]}", file=sys.stderr)
        return 2

    
    
    
    if len(sys.argv) > 3 and sys.argv[1] == "--commands":
        report = {}
        project_commands(sys.argv[2], sys.argv[3], "project", report)
        for line in report.get("opencode", []):
            print(f"  opencode: {line}")
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--codex-project":
        report = {}
        materialize_codex_content(HOME, STORE, sys.argv[2], report)
        for line in report.get("codex", []):
            print("  codex: " + line)
        return 0

    manifest = load_json(MANIFEST, None)
    if not manifest or "servers" not in manifest:
        print(f"harness-materialize: no manifest at {MANIFEST}", file=sys.stderr)
        return 1
    report = {}
    materialize_mcp(manifest, report)
    materialize_codex_mcp(HOME, manifest, report)
    materialize_opencode_projects(manifest, report)
    materialize_instructions(report)
    materialize_skills(report)
    if os.path.isdir(os.path.join(HOME, ".codex")):
        materialize_codex_content(HOME, STORE, None, report)
    materialize_commands(report)
    materialize_agents(report)
    materialize_pi_extensions(report)
    materialize_pi_hooks(report)
    materialize_xcode(manifest, report)
    materialize_hooks(report)
    if os.path.isdir(os.path.join(HOME, ".codex")):
        materialize_codex_hooks(report, launcher=hook_client(HOME))

    print("harness-materialize: agent-context projected into installed harnesses")
    if report.get("_shared"):
        for line in report.pop("_shared"):
            print(f"  {line}")
    for hname in sorted(report):
        for line in report[hname]:
            print(f"  {hname}: {line}")
    
    
    
    
    if not os.environ.get("AGENT_CONTEXT_OFFLINE_RENDER"):
        record_health()
        write_stamp()
    return 0


if __name__ == "__main__":
    sys.exit(main())
