#!/usr/bin/env python3
"Cost of everything a session loads before its first tool call.\n\nOne command prints the whole always-loaded bill: instruction files, `always`\nmemory index rows and MCP tool schemas, in bytes and estimated tokens, and exits\nnon-zero when any line is over its budget. `invariant-check.py` runs `--check`\nas `always-loaded-context-within-budget`, and `block-instruction-budget-overrun`\ncalls `--json --project`/`--workspace` for the instruction figures, so each\nnumber has one home.\n\nUsage:  python3 context_budget.py            # report\n        python3 context_budget.py --check    # exit 1 on overrun\n        python3 context_budget.py --json\n        python3 context_budget.py --json --scope-report                   # global scope,\n        python3 context_budget.py --json --scope-report --project NAME    # or one project/\n        python3 context_budget.py --json --scope-report --workspace NAME  # workspace: the\n                                                        # lightweight report block-instruction-\n                                                        # budget-overrun asks the daemon for\n\nA memory that restates what a hook already enforces is flagged: the hook stops the\naction, so the memory's slot is paid on every session for a rule that is already\nmechanical. Flags are advisory and never fail `--check`."

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task

STORE = Path(os.environ.get("AGENT_CONTEXT_STORE") or Path.home() / ".agent-context")

INSTRUCTION_BUDGET = int(os.environ.get("INSTRUCTION_BUDGET_BYTES") or "12000")
TOOL_BUDGET_TOKENS = 20_000
TOTAL_BUDGET_TOKENS = 29_600  
TOKENS_PER_TOOL = 500
BYTES_PER_TOKEN = 4



KEPT_ALWAYS_DESPITE_HOOK = {"never-proceed-without-lsp"}


def tokens(nbytes):
    return -(-nbytes // BYTES_PER_TOKEN)


def _frontmatter(text):
    "Key/value pairs of a store file's frontmatter, values kept as raw text."
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    out = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    return value


def instruction_files():
    '{label: bytes} for the always-loaded instruction text, missing files skipped.'
    found = {}
    d = STORE / "global" / "instructions"
    if d.is_dir():
        for path in sorted(d.glob("*.md")):
            fm = _frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            if _unquote(fm.get("load_behavior", '"always"')) == "lazy":
                continue
            found[f"global/instructions/{path.name}"] = path.stat().st_size
    
    
    agents = STORE / "AGENTS.md"
    if agents.is_file():
        found["AGENTS.md"] = agents.stat().st_size
    return found


def memory_budget():
    'Per-scope row budget, read from the server so there is one number.'
    src = (STORE / "server" / "src" / "agent_context" / "index.py").read_text(encoding="utf-8")
    m = re.search(r"^_MEM_ROWS_BUDGET\s*=\s*([\d_]+)", src, re.M)
    return int(m.group(1).replace("_", "")) if m else 14_000


def fixed_bytes():
    'Always-loaded instruction text no per-scope write can change: AGENTS.md.'
    agents = STORE / "AGENTS.md"
    return agents.stat().st_size if agents.is_file() else 0


def _sidecar_load_behavior(meta_path):
    '"lazy" or "always" from a .meta.toml sidecar\'s load_behavior line, or None when\n    the sidecar is absent or says nothing -- the caller then reads frontmatter instead.'
    try:
        with open(meta_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip().startswith("load_behavior"):
                    return "lazy" if "lazy" in line else "always"
    except OSError:
        pass
    return None


def _sidecar_title(meta_path):
    'The `title` from a .meta.toml sidecar, or "" when absent.'
    try:
        with open(meta_path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                key, sep, value = line.partition("=")
                if sep and key.strip() == "title":
                    return value.strip().strip('"')
    except OSError:
        pass
    return ""


def scope_dir(project=None, workspace=None):
    'The store directory for a scope: global, or one project/workspace.'
    if workspace:
        return STORE / "workspaces" / workspace
    if project:
        return STORE / "projects" / project
    return STORE / "global"


def scope_instruction_sizes(base):
    '{filename: {"bytes": n, "title": t}} for every ALWAYS-loaded instruction under\n    base/instructions -- the figures block-instruction-budget-overrun needs to judge\n    one write, so it does not scan the directory itself.\n\n    A sidecar\'s load_behavior/title wins when present (older entities, and the budget\n    hook\'s own hook-test-cases.py fixtures, carry it there); otherwise the file\'s own\n    frontmatter is read. A load_behavior neither can answer counts as always-loaded:\n    over-counting risks a false refusal with a clear message, under-counting reopens\n    the hole the hook exists to close.'
    out = {}
    d = base / "instructions"
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.md")):
        meta = Path(str(path) + ".meta.toml")
        behavior = _sidecar_load_behavior(meta)
        title = _sidecar_title(meta)
        if behavior is None or not title:
            fm = _frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            if behavior is None:
                behavior = _unquote(fm.get("load_behavior", "always"))
            if not title:
                title = _unquote(fm.get("title", ""))
        if behavior == "lazy":
            continue
        out[path.name] = {"bytes": path.stat().st_size, "title": title}
    return out


def hook_names():
    d = STORE / "global" / "hooks"
    return {p.stem for p in d.glob("*.py")} if d.is_dir() else set()


def always_memories():
    '[(scope, slug, row_bytes, hook_stems_it_names)] for every `always` memory.'
    abbr = {"feedback": "f", "project": "p", "reference": "r", "user": "u"}
    hooks = hook_names()
    scopes = [("global", STORE / "global" / "memory")]
    projects = STORE / "projects"
    if projects.is_dir():
        scopes += [(p.name, p / "memory") for p in sorted(projects.iterdir())]
    rows = []
    for scope, d in scopes:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            fm = _frontmatter(text)
            if _unquote(fm.get("load_behavior", "")) != "always":
                continue
            slug = _unquote(fm.get("slug", "")) or path.stem
            desc = _unquote(fm.get("description", ""))
            row = f"{abbr.get(_unquote(fm.get('memory_type', '')), '?')} {slug} — {desc}"
            enforced = {m for m in re.findall(r"global/hooks/([\w-]+)\.py", fm.get("enforced_by", ""))
                        if m in hooks}
            rows.append((scope, slug, len(row.encode("utf-8")), sorted(enforced),
                         _unquote(fm.get("memory_type", ""))))
    return rows


def tool_count():
    'Tools the agent-context server registers, counted from its source.'
    src = (STORE / "server" / "src" / "agent_context" / "server.py").read_text(encoding="utf-8")
    return len(re.findall(r"^@mcp\.tool\(", src, re.M))


def measure():
    instr = instruction_files()
    mem = always_memories()
    per_scope = {}
    for scope, _slug, size, _hooks, _type in mem:
        per_scope[scope] = per_scope.get(scope, 0) + size
    n_tools = tool_count()
    instr_bytes = sum(instr.values())
    mem_bytes = sum(per_scope.values())
    tool_tokens = n_tools * TOKENS_PER_TOOL
    total = tokens(instr_bytes) + tokens(mem_bytes) + tool_tokens
    return {
        "instructions": instr, "instruction_bytes": instr_bytes,
        "memories": mem, "memory_bytes_by_scope": per_scope,
        "memory_bytes": mem_bytes, "tools": n_tools, "tool_tokens": tool_tokens,
        "total_tokens": total,
    }


def overruns(m):
    out = []
    if m["instruction_bytes"] > INSTRUCTION_BUDGET:
        out.append(f"instructions {m['instruction_bytes']} bytes, over the "
                   f"{INSTRUCTION_BUDGET} budget by {m['instruction_bytes'] - INSTRUCTION_BUDGET}")
    budget = memory_budget()
    for scope, size in sorted(m["memory_bytes_by_scope"].items()):
        if size > budget:
            out.append(f"`always` memory rows for {scope}: {size} bytes, over the {budget} budget")
    if m["tool_tokens"] > TOOL_BUDGET_TOKENS:
        out.append(f"{m['tools']} tools at {TOKENS_PER_TOOL} tokens = {m['tool_tokens']}, "
                   f"over the {TOOL_BUDGET_TOKENS} token budget")
    if TOTAL_BUDGET_TOKENS and m["total_tokens"] > TOTAL_BUDGET_TOKENS:
        out.append(f"total {m['total_tokens']} tokens, over the {TOTAL_BUDGET_TOKENS} ceiling")
    return out


def demotion_candidates(m):
    '[(scope, slug, why)]: `always` memories a hook enforces, or that are reference.'
    out = []
    for scope, slug, _size, hooks, mtype in m["memories"]:
        if slug in KEPT_ALWAYS_DESPITE_HOOK:
            continue
        if hooks:
            out.append((scope, slug, "enforced by hook " + ", ".join(hooks)))
        elif mtype == "reference":
            out.append((scope, slug, "reference, not a rule"))
    return out


def render(m):
    lines = ["INSTRUCTIONS  (%d bytes, ~%d tokens, budget %d bytes)"
             % (m["instruction_bytes"], tokens(m["instruction_bytes"]), INSTRUCTION_BUDGET)]
    for label, size in m["instructions"].items():
        lines.append(f"  {size:>7}  ~{tokens(size):>5} tok  {label}")
    lines.append("")
    budget = memory_budget()
    lines.append("ALWAYS MEMORY ROWS  (%d bytes, ~%d tokens, budget %d bytes per scope)"
                 % (m["memory_bytes"], tokens(m["memory_bytes"]), budget))
    for scope, slug, size, hooks, _type in sorted(m["memories"], key=lambda r: (r[0], -r[2])):
        flag = ""
        if hooks:
            kept = " (kept always by decision)" if slug in KEPT_ALWAYS_DESPITE_HOOK else ""
            flag = f"  hook: {', '.join(hooks)}{kept}"
        lines.append(f"  {size:>7}  ~{tokens(size):>5} tok  {scope}/{slug}{flag}")
    lines.append("")
    lines.append("MCP TOOLS  (%d tools x %d = ~%d tokens, budget %d)"
                 % (m["tools"], TOKENS_PER_TOOL, m["tool_tokens"], TOOL_BUDGET_TOKENS))
    lines.append("")
    ceiling = f", ceiling {TOTAL_BUDGET_TOKENS}" if TOTAL_BUDGET_TOKENS else ""
    lines.append(f"TOTAL ALWAYS-LOADED  ~{m['total_tokens']} tokens{ceiling}")
    cands = demotion_candidates(m)
    if cands:
        lines.append("")
        lines.append("FLAGGED: `always` memories to consider demoting to lazy")
        for scope, slug, why in cands:
            lines.append(f"  {scope}/{slug}: {why}")
    over = overruns(m)
    if over:
        lines.append("")
        lines.append("OVER BUDGET")
        lines.extend(f"  {o}" for o in over)
    return "\n".join(lines)


def _scope_report(argv):
    '{"scope", "sizes", "fixed_bytes", "budget"}: exactly what\n    block-instruction-budget-overrun needs to judge one scope\'s write, for\n    --project NAME or --workspace NAME.'
    project = argv[argv.index("--project") + 1] if "--project" in argv else None
    workspace = argv[argv.index("--workspace") + 1] if "--workspace" in argv else None
    return {"scope": workspace or project or "global",
            "sizes": scope_instruction_sizes(scope_dir(project, workspace)),
            "fixed_bytes": 0 if (project or workspace) else fixed_bytes(),
            "budget": INSTRUCTION_BUDGET}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--scope-report" in argv:
        print(json.dumps(_scope_report(argv), indent=2))
        return 0
    m = measure()
    over = overruns(m)
    if "--json" in argv:
        print(json.dumps({**m, "memories": [list(r) for r in m["memories"]],
                          "overruns": over}, indent=2))
        return 0
    if "--check" in argv:
        print("\n".join(over))
    else:
        print(render(m))
    return 1 if over else 0


if __name__ == "__main__":
    store_task.main_or_forward("context_budget", main)
