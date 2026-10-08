#!/usr/bin/env python3
'Project the store\'s agent definitions into opencode and pi, translated for each.\n\nUsage:\n  agents-pin.py opencode <src-agents-dir> [<dst-agent-dir>]\n  agents-pin.py pi <src-agents-dir> <dst-pi-agents-dir>\n\nWhy this exists. The store\'s worker definitions are written for Claude Code: `model:` is\na tier alias (`sonnet`) and `tools:` uses Claude Code\'s names (`Read`,\n`mcp__csharp-lsp__definition`). opencode and pi both read agent markdown, and both get\nboth fields wrong when handed them verbatim.\n\n  `tools:` is a strict allowlist matched by exact name in both. A Claude Code spelling\n  matches nothing, and the worker boots with no language server, silently, while still\n  answering fluently from grep.\n\n  `model:` is an alias neither resolves well. pi matches it by substring against the\n  first catalog id, `claude-sonnet-4`, the oldest of the tier, and falls back to the\n  session model when nothing matches. opencode cannot resolve it at all. Both get a\n  canonical `provider/id` for the newest member of the tier.\n\n  When Claude Code\'s own settings remap an alias (`ANTHROPIC_DEFAULT_SONNET_MODEL`, set\n  fleet-wide by home-settings-sync.py\'s MANAGED_ENV), that id wins over the catalog\'s\n  newest, on the provider that carries the tier. The catalogs can lag a release,\n  and pi sends an unlisted id as a custom id. One remap then moves every harness\'s\n  workers together.\n\nThe definitions stay alias-only: they are shared with Claude Code, and a versioned id\nthere rots on the next model release. Each harness gets a derived copy. This script\nreplaced opencode-agents-pin.py and pi-agents-pin.py, which carried the same\ntranslation twice; test-agents-pin.py holds their output as goldens.\n\nWhat differs by harness, on purpose:\n\n  opencode renders the frontmatter anew into ~/.config/opencode/agent, which it globs as\n  `{agent,agents}/**/*.md`. `mode: subagent` makes the name valid in task(), and\n  `"*": false` comes first or opencode grants its full tool set. MCP tools become\n  `<server>_<tool>` with the server\'s hyphens kept, the spelling of the hand-written\n  `*-local` agents in opencode.json. An agent whose tools all drop is skipped: a worker\n  with no tools is worse than no worker. Only providers opencode holds credentials for\n  can carry a model; with none, no model line is written and the worker inherits\n  opencode\'s default, since a pinned unauthenticated provider is a worker that dies at\n  spawn. A same-named file this script did not write is left alone. The `agent` block in\n  opencode.json is hand-maintained (it carries `lane` and the `*-local` agents) and is\n  never touched.\n\n  pi gets a copy with only the `model:` and `tools:` lines edited, in ~/.pi/agent/agents\n  or <repo>/.pi/agents, which pi loads above the .claude copies. MCP tools become\n  `<server>_<tool>` with every separator an underscore: the language servers are\n  extension-registered tools there, not MCP servers, so `mcp:<server>/<tool>` selects\n  nothing. Providers rank pi\'s defaultProvider first, then the authenticated ones; an id\n  no ranked provider carries binds to its first carrier by name, and an alias nothing\n  carries is kept verbatim, so pi degrades to its old behavior and does not die on an\n  unresolvable --model.\n\nBoth prune only what a previous run wrote (a manifest, not a heuristic), rewrite a file\nonly when its bytes change, and write through a temp file in the same directory: a\nharness reads these when it spawns a worker, and a truncated one is a worker with no\nmodel or tools line. A missing harness or source exits 0 with no output. Other failures\nstill raise (an undecodable definition, a destination that is a regular file); every\ncaller treats a nonzero exit as non-fatal, so this never stops a session.\n\nRuns on Python 3.8, the system Python on the Synology nodes. str.removesuffix once raised\nthere on every run and left s1\'s pi workers on the session model.\n\nObservations guarded: #258.'
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp  

HOME = os.path.expanduser("~")
ALIASES = ("opus", "sonnet", "haiku", "fable")
MCP_TOOL_RE = re.compile(r"^mcp__(.+?)__(.+)$")
USAGE = ("usage: agents-pin.py opencode <src-agents-dir> [<dst-agent-dir>]\n"
         "       agents-pin.py pi <src-agents-dir> <dst-pi-agents-dir>")

OPENCODE_CONFIG = os.path.join(HOME, ".config", "opencode")
OPENCODE_DST = os.path.join(OPENCODE_CONFIG, "agent")
OPENCODE_AUTH = os.path.join(HOME, ".local", "share", "opencode", "auth.json")
OPENCODE_CATALOG = os.path.join(HOME, ".cache", "opencode", "models.json")
OPENCODE_MANIFEST = ".agent-context-agents.json"

PI_AGENT_DIR = os.path.join(HOME, ".pi", "agent")
PI_AUTH = os.path.join(PI_AGENT_DIR, "auth.json")
PI_SETTINGS = os.path.join(PI_AGENT_DIR, "settings.json")
PI_CATALOG = os.path.join(PI_AGENT_DIR, "models-store.json")
PI_MANIFEST = ".pi-agents-pin.manifest"

CLAUDE_SETTINGS = hp.settings_file(HOME)





OPENCODE_TOOLS = {
    "read": "read",
    "grep": "grep",
    "glob": "glob",
    "ls": "list",
    "bash": "bash",
    "edit": "edit",
    "write": "write",
    "multiedit": "patch",
    "notebookedit": "patch",
    "skill": "skill",
    "webfetch": "webfetch",
    "websearch": "websearch",
    "toolsearch": None,
    "task": None,
}
PI_TOOLS = {
    "read": "read",
    "grep": "grep",
    "glob": "find",
    "ls": "ls",
    "bash": "bash",
    "edit": "edit",
    "write": "write",
    "toolsearch": None,
    "notebookedit": None,
    "task": None,
    "websearch": None,
    "webfetch": None,
}


def opencode_mcp_name(server, tool):
    return "%s_%s" % (server, tool)


def pi_mcp_name(server, tool):
    return "%s_%s" % (re.sub(r"[^0-9a-zA-Z]+", "_", server), re.sub(r"[^0-9a-zA-Z]+", "_", tool))




def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def read_text(path):
    "The file's text, or None when it cannot be opened."
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def write_atomic(path, text):
    'Replace path with text through a temp file in the same directory.\n\n    Same directory, or the rename is a copy. The temp file is removed when the write\n    dies, so a failed run leaves nothing for a harness glob to load. Raises OSError.'
    tmp = os.path.join(os.path.dirname(path) or ".", ".pin-tmp-" + os.path.basename(path))
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def source_agents(src):
    '(file name, text) for each readable .md definition in src, sorted by name.'
    for name in sorted(os.listdir(src)):
        if not name.endswith(".md"):
            continue
        text = read_text(os.path.join(src, name))
        if text is not None:
            yield name, text


def translate_tool_list(raw, tool_map, mcp_name):
    '(translated names, dropped names). Order kept, duplicates collapsed.'
    kept, dropped, seen = [], [], set()
    for item in raw.split(","):
        name = item.strip()
        if not name:
            continue
        hit = MCP_TOOL_RE.match(name)
        mapped = mcp_name(hit.group(1), hit.group(2)) if hit else tool_map.get(name.lower())
        if mapped is None:
            dropped.append(name)
        elif mapped not in seen:
            seen.add(mapped)
            kept.append(mapped)
    return kept, dropped


def version_key(model_id):
    'Sort key: the numbers in the id, highest wins, shorter id breaks a tie.\n\n    `claude-sonnet-5` (5,) beats `claude-sonnet-4.6` (4,6) and a dated\n    `claude-sonnet-4-5-20250929` (4,5,20250929): the leading component decides first,\n    which is what "newest member of the tier" means.'
    nums = tuple(int(n) for n in re.findall(r"\d+", model_id))
    return (nums, -len(model_id))


def claude_remaps():
    "{alias: id} from Claude Code's `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` settings env."
    settings = load_json(CLAUDE_SETTINGS, {})
    env = settings.get("env") if isinstance(settings, dict) else None
    if not isinstance(env, dict):
        return {}
    remaps = {}
    for alias in ALIASES:
        value = env.get("ANTHROPIC_DEFAULT_%s_MODEL" % alias.upper())
        if isinstance(value, str) and value.strip():
            remaps[alias] = value.strip()
    return remaps


def resolve(alias, pairs, providers, any_carrier, remap=None):
    'Canonical `provider/id` for a tier alias, or None.\n\n    The newest id containing the alias, on the first ranked provider that carries it.\n    Provider-qualified on purpose: pi rejects a bare id several providers offer, and\n    `claude-sonnet-5` is offered by anthropic, github-copilot and amazon-bedrock alike.\n    When no ranked provider carries the id, `any_carrier` picks the first carrier by name.\n    A `remap` id replaces the newest, on any provider carrying the tier.'
    matches = [(p, i) for p, i in pairs if alias in i.lower()]
    if not matches:
        return None
    if remap:
        matches = [(p, remap) for p, _ in matches]
    best_id = max((i for _, i in matches), key=version_key)
    carriers = [p for p, i in matches if i == best_id]
    for provider in providers:
        if provider in carriers:
            return "%s/%s" % (provider, best_id)
    return "%s/%s" % (sorted(carriers)[0], best_id) if any_carrier else None




def opencode_providers():
    'Providers opencode holds credentials for, in stored order. A restriction, not a\n    preference: a provider opencode cannot authenticate is a worker that dies at spawn.'
    data = load_json(OPENCODE_AUTH, {})
    return [p for p in data if isinstance(p, str)] if isinstance(data, dict) else []


def opencode_catalog(providers):
    'Every (provider, model id) opencode can run, authenticated providers only.'
    data = load_json(OPENCODE_CATALOG, {})
    pairs = []
    if not isinstance(data, dict):
        return pairs
    for provider in providers:
        entry = data.get(provider)
        models = entry.get("models") if isinstance(entry, dict) else None
        if isinstance(models, dict):
            pairs.extend((provider, mid) for mid in models if isinstance(mid, str))
    return pairs


def split_frontmatter(text):
    "(dict of top-level scalar keys, body). No frontmatter -> ({}, text).\n\n    Deliberately not YAML: these files carry one `key: value` per line, and PyYAML is not\n    in every machine's system Python. Indented lines are skipped, unlike pi's line editor\n    below, which takes the first `model:` line at any depth."
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 3)
    if end == -1:
        return {}, text
    fm = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and not key.startswith((" ", "\t", "#")):
            fm[key.strip()] = value.strip().strip('"').strip("'")
    return fm, text[end + 5:]


def render_opencode(text, pairs, providers, notes, remaps):
    '(opencode agent file, None), or (None, why it was skipped).'
    fm, body = split_frontmatter(text)
    name = fm.get("name", "").strip()
    description = fm.get("description", "").strip()
    if not name or not description:
        return None, "no name/description"

    kept, dropped = translate_tool_list(fm.get("tools", ""), OPENCODE_TOOLS, opencode_mcp_name)
    if not kept:
        return None, "no tool name translated"
    if dropped:
        notes.update(dropped)

    out = [
        "---",
        
        
        "description: " + json.dumps(description, ensure_ascii=False),
        "mode: subagent",
    ]
    alias = fm.get("model", "").strip().lower()
    if alias in ALIASES:
        model = resolve(alias, pairs, providers, any_carrier=False, remap=remaps.get(alias))
        if model:
            out.append("model: " + model)
    out.append("tools:")
    out.append('  "*": false')
    out.extend("  %s: true" % tool for tool in kept)
    out += ["---", ""]
    return "\n".join(out) + body.lstrip("\n"), None


def project_opencode(src, dst):
    
    if not os.path.isdir(OPENCODE_CONFIG) or not os.path.isdir(src):
        return 0

    providers = opencode_providers()
    pairs = opencode_catalog(providers)
    remaps = claude_remaps()
    manifest_path = os.path.join(dst, OPENCODE_MANIFEST)
    owned = load_json(manifest_path, [])
    if not isinstance(owned, list):
        owned = []

    wrote, skipped, foreign, unmapped = [], [], [], set()
    for name, text in source_agents(src):
        rendered, why = render_opencode(text, pairs, providers, unmapped, remaps)
        if rendered is None:
            skipped.append("%s (%s)" % (name, why))
            continue
        out_path = os.path.join(dst, name)
        
        if os.path.exists(out_path) and name not in owned:
            foreign.append(name)
            continue
        os.makedirs(dst, exist_ok=True)
        if read_text(out_path) == rendered:
            wrote.append(name)
            continue
        try:
            write_atomic(out_path, rendered)
            wrote.append(name)
        except OSError as exc:
            print("opencode-agents-pin: cannot write %s: %s" % (out_path, exc), file=sys.stderr)

    pruned = []
    for stale in sorted(set(owned) - set(wrote)):
        try:
            os.remove(os.path.join(dst, stale))
            pruned.append(stale)
        except OSError:
            pass
    if wrote or owned:
        try:
            os.makedirs(dst, exist_ok=True)
            write_atomic(manifest_path, json.dumps(sorted(wrote), indent=2))
        except OSError:
            pass

    parts = []
    if wrote:
        parts.append("%d agent(s) -> %s" % (len(wrote), dst))
    if pruned:
        parts.append("pruned " + ", ".join(pruned))
    if foreign:
        parts.append("LEFT ALONE (not ours): " + ", ".join(foreign))
    if skipped:
        parts.append("SKIPPED " + "; ".join(skipped))
    if unmapped:
        parts.append("dropped unmappable tools " + " ".join(sorted(unmapped)))
    
    if parts:
        print("opencode-agents-pin: " + "; ".join(parts))
    return 0




def pi_providers():
    'Providers to prefer, best first: pi\'s defaultProvider, then every provider it holds\n    credentials for. A child spawned against any other dies with "No API key found".'
    order = []
    settings = load_json(PI_SETTINGS, {})
    default = settings.get("defaultProvider") if isinstance(settings, dict) else None
    if isinstance(default, str) and default:
        order.append(default)
    authed = load_json(PI_AUTH, {})
    if isinstance(authed, dict):
        order.extend(p for p in authed if isinstance(p, str) and p not in order)
    return order


def pi_catalog():
    "Every (provider, id) pair in pi's live catalog. Missing or corrupt -> empty, which\n    keeps every alias verbatim."
    data = load_json(PI_CATALOG, {})
    pairs = []
    if not isinstance(data, dict):
        return pairs
    for provider_name, provider in data.items():
        models = provider.get("models", []) if isinstance(provider, dict) else []
        for model in models:
            mid = model.get("id") if isinstance(model, dict) else None
            if isinstance(mid, str):
                pairs.append((provider_name, mid))
    return pairs


def frontmatter_field(lines, key):
    '(index, value without quotes, quote character) of the first `<key>:` line inside\n    the leading `---` block, at any indent, or None.'
    if not lines or lines[0].strip() != "---":
        return None
    for idx in range(1, len(lines)):
        stripped = lines[idx].strip()
        if stripped == "---":
            return None
        if not stripped.startswith(key + ":"):
            continue
        raw = stripped.split(":", 1)[1].strip()
        quote = raw[0] if raw[:1] in ("'", '"') else ""
        return idx, raw.strip("'\""), quote
    return None


def replace_field(lines, idx, key, value, quote):
    'Rewrite one frontmatter line, keeping its indent and quoting style.'
    indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip())]
    lines[idx] = "%s%s: %s%s%s" % (indent, key, quote, value, quote)


def project_pi(src, dst):
    
    if not os.path.isdir(PI_AGENT_DIR) or not os.path.isdir(src):
        return 0

    pairs = pi_catalog()
    providers = pi_providers()
    remaps = claude_remaps()
    generated, pinned, verbatim, retooled, unmapped = [], [], [], [], set()

    for name, text in source_agents(src):
        lines = text.split("\n")
        stem = name[:-3]
        hit = frontmatter_field(lines, "model")
        if hit and hit[1].lower() in ALIASES:
            idx, value, quote = hit
            model = resolve(value.lower(), pairs, providers, any_carrier=True,
                            remap=remaps.get(value.lower()))
            if model:
                replace_field(lines, idx, "model", model, quote)
                pinned.append("%s=%s" % (stem, model))
            else:
                verbatim.append(name)
        tools_hit = frontmatter_field(lines, "tools")
        if tools_hit:
            idx, raw_tools, quote = tools_hit
            translated, dropped = translate_tool_list(raw_tools, PI_TOOLS, pi_mcp_name)
            if translated and ", ".join(translated) != raw_tools:
                replace_field(lines, idx, "tools", ", ".join(translated), quote)
                retooled.append("%s(%d)" % (stem, len(translated)))
            unmapped.update(dropped)
        text = "\n".join(lines)
        os.makedirs(dst, exist_ok=True)
        out = os.path.join(dst, name)
        if read_text(out) == text:
            generated.append(name)
            continue
        try:
            write_atomic(out, text)
            generated.append(name)
        except OSError as exc:
            print("pi-agents-pin: cannot write %s: %s" % (out, exc), file=sys.stderr)

    
    
    manifest_path = os.path.join(dst, PI_MANIFEST)
    previous_text = read_text(manifest_path)
    previous = [] if previous_text is None else \
        [line.strip() for line in previous_text.split("\n") if line.strip()]
    for stale in set(previous) - set(generated):
        try:
            os.remove(os.path.join(dst, stale))
        except OSError:
            pass
    if generated:
        try:
            write_atomic(manifest_path, "\n".join(sorted(generated)) + "\n")
        except OSError:
            pass

    if pinned or verbatim or retooled or unmapped:
        note = "pi-agents-pin: %s <- %d definition(s)" % (dst.replace(HOME, "~"), len(generated))
        if pinned:
            note += "; pinned " + " ".join(sorted(pinned))
        if retooled:
            note += "; tools translated " + " ".join(sorted(retooled))
        if verbatim:
            
            note += "; UNRESOLVED alias kept in " + " ".join(sorted(verbatim))
        if unmapped:
            note += "; DROPPED unmappable tools " + " ".join(sorted(unmapped))
        print(note)
    return 0





PROJECTORS = {
    "opencode": (project_opencode, OPENCODE_DST),
    "pi": (project_pi, None),
}


def main(argv):
    harness = argv[1] if len(argv) > 1 else ""
    if harness not in PROJECTORS or not 3 <= len(argv) <= 4:
        print(USAGE, file=sys.stderr)
        return 2
    project, default_dst = PROJECTORS[harness]
    if len(argv) == 4:
        dst = argv[3]
    elif default_dst is not None:
        dst = default_dst
    else:
        print(USAGE, file=sys.stderr)
        return 2
    return project(argv[2], dst)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
